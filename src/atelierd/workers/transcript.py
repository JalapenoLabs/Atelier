"""One session's chat: the worker's stream-json events turned into `ChatItem`s and kept on disk.

Claude Code keeps its own record of a session, but in its own format and folder. Atelier keeps what the chat panel
draws, as JSON lines in `<state>/transcripts/<session id>.jsonl`, so the panel (and a manager, through MCP) can read a
worker's history with the daemon restarted and no Claude Code process running. Each line is a whole item; a later line
with the same index replaces an earlier one (a tool call gaining its result, an approval being answered), so the file
is only ever appended to.

Images a tool returns (a render from the worker's Blender, an image it read) arrive as base64 in the stream. They are
written to `<state>/transcripts/<session id>/`, and the item names the file: images never travel over the front
doors.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atelierd.workers.models import ChatItem, ChatItemKind

logger = logging.getLogger(__name__)

# How much of a tool's result the chat keeps: enough to read an error or a listing, not a whole file.
_TOOL_OUTPUT_LIMIT = 4000
_SUMMARY_LIMIT = 240
# The argument that says best what each tool call does.
_SUMMARY_ARGUMENT_BY_TOOL = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Task": "description",
    "Agent": "description",
    "mcp__blender__execute_blender_code": "code",
}
_IMAGE_SUFFIX_BY_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class Transcript:
    """The chat of one session, safe to use from the worker's threads."""

    def __init__(self, transcripts_dir: Path, session_id: str, project_root: Path) -> None:
        self.session_id = session_id
        self._path = transcripts_dir / f"{session_id}.jsonl"
        self._images_dir = transcripts_dir / session_id
        self._project_root = project_root
        self._guard = threading.Lock()
        # In index order: indices only grow, and an item keeps its place when it is updated.
        self._item_by_index = {item.index: item for item in read_transcript(self._path)}
        self._next_index = max(self._item_by_index, default=-1) + 1
        self._tool_index_by_use_id = {
            item.tool_use_id: item.index for item in self._item_by_index.values() if item.tool_use_id
        }

    @property
    def items(self) -> tuple[ChatItem, ...]:
        """Every item, in order."""
        with self._guard:
            return tuple(self._item_by_index.values())

    def add(self, kind: ChatItemKind, **fields: Any) -> ChatItem:
        """Append a new item and return it."""
        with self._guard:
            item = ChatItem(index=self._next_index, kind=kind, created_at=datetime.now(UTC).isoformat(), **fields)
            self._next_index += 1
            self._item_by_index[item.index] = item
            if item.tool_use_id:
                self._tool_index_by_use_id[item.tool_use_id] = item.index
            self._append_line(item)
            return item

    def update(self, index: int, **fields: Any) -> ChatItem:
        """Change fields of an earlier item and return it."""
        with self._guard:
            item = self._item_by_index[index].model_copy(update=fields)
            self._item_by_index[index] = item
            self._append_line(item)
            return item

    def apply_event(self, event: dict[str, Any]) -> list[ChatItem]:
        """Turn one stream-json event into the items it adds or changes.

        Only the worker's messages are read here: text and tool calls from `assistant` events, tool results from
        `user` events. Session starts and turn ends carry state the worker itself tracks, so it records those.
        """
        event_type = event.get("type")
        message = event.get("message") or {}
        content = message.get("content")
        if event_type not in ("assistant", "user") or not isinstance(content, list):
            return []

        changed: list[ChatItem] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if event_type == "assistant" and block_type == "text" and str(block.get("text", "")).strip():
                changed.append(self.add(ChatItemKind.ASSISTANT, text=str(block["text"])))
            elif event_type == "assistant" and block_type == "tool_use":
                tool_name = str(block.get("name", ""))
                raw_input = block.get("input")
                tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
                changed.append(
                    self.add(
                        ChatItemKind.TOOL,
                        tool_name=tool_name,
                        tool_use_id=str(block.get("id", "")),
                        tool_summary=self._summarise(tool_name, tool_input),
                    )
                )
            elif event_type == "user" and block_type == "tool_result":
                result_item = self._apply_tool_result(block)
                if result_item:
                    changed.append(result_item)
        return changed

    def _apply_tool_result(self, block: dict[str, Any]) -> ChatItem | None:
        tool_use_id = str(block.get("tool_use_id", ""))
        with self._guard:
            index = self._tool_index_by_use_id.get(tool_use_id)
        if index is None:
            logger.debug(f"[Transcript] A tool result for unknown call {tool_use_id}, leaving it out")
            return None

        texts: list[str] = []
        images: list[Path] = []
        content = block.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        for part in blocks:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "image":
                image_path = self._save_image(index, len(images), part.get("source") or {})
                if image_path:
                    images.append(image_path)
        output = "\n".join(text for text in texts if text)
        return self.update(
            index,
            tool_output=output[:_TOOL_OUTPUT_LIMIT],
            is_error=bool(block.get("is_error")),
            is_done=True,
            images=tuple(images),
        )

    def _save_image(self, index: int, position: int, source: dict[str, Any]) -> Path | None:
        media_type = str(source.get("media_type", ""))
        suffix = _IMAGE_SUFFIX_BY_MEDIA_TYPE.get(media_type)
        if source.get("type") != "base64" or suffix is None:
            logger.debug(
                f"[Transcript] An image of type {media_type or 'unknown'} that cannot be saved, leaving it out"
            )
            return None
        try:
            image_bytes = base64.b64decode(str(source.get("data", "")), validate=True)
        except binascii.Error as error:
            logger.warning(f"[Transcript] An image in session {self.session_id} is not valid base64: {error}")
            return None
        self._images_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._images_dir / f"{index}-{position}{suffix}"
        image_path.write_bytes(image_bytes)
        return image_path

    def _summarise(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """One line saying what a call does: its command, file or pattern, with paths shown project-relative."""
        argument = _SUMMARY_ARGUMENT_BY_TOOL.get(tool_name)
        value = tool_input.get(argument) if argument else None
        if value is None:
            value = next((item for item in tool_input.values() if isinstance(item, str)), "")
        text = str(value).strip()
        if text.startswith(str(self._project_root) + "/"):
            text = text[len(str(self._project_root)) + 1 :]
        first_line = text.splitlines()[0] if text else ""
        if len(first_line) > _SUMMARY_LIMIT:
            return first_line[: _SUMMARY_LIMIT - 1] + "…"
        return first_line

    def _append_line(self, item: ChatItem) -> None:
        """Write one item at the end of the file. Called holding the guard, so lines never interleave."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as transcript_file:
            transcript_file.write(item.model_dump_json() + "\n")


def read_transcript(path: Path) -> list[ChatItem]:
    """Read a transcript file, folding each item's later lines over its earlier ones. A missing file is empty."""
    if not path.is_file():
        return []
    item_by_index: dict[int, ChatItem] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = ChatItem.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as error:
            # A line cut short by a crash mid-write; everything before it is still whole.
            logger.warning(f"[Transcript] Skipping unreadable line {line_number} of {path.name}: {error}")
            continue
        item_by_index[item.index] = item
    return [item_by_index[index] for index in sorted(item_by_index)]
