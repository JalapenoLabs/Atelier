"""A session's chat built from Claude Code's stream-json events, and kept on disk."""

from __future__ import annotations

import base64
from pathlib import Path

from atelierd.workers import ChatItemKind
from atelierd.workers.transcript import Transcript, read_transcript

ROOT = Path("/projects/Fall")
_PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _tool_use(tool_use_id: str, name: str, tool_input: dict[str, object]) -> dict[str, object]:
    block = {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"content": [block]}}


def _tool_result(tool_use_id: str, content: object, is_error: bool = False) -> dict[str, object]:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}
    return {"type": "user", "message": {"content": [block]}}


def test_text_and_tool_calls_become_items_and_results_complete_them(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "session", ROOT)
    transcript.apply_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "On it."}]}})
    [tool] = transcript.apply_event(_tool_use("t1", "Read", {"file_path": f"{ROOT}/SourceArt/Weapons/README.md"}))
    assert (tool.tool_name, tool.tool_summary, tool.is_done) == ("Read", "SourceArt/Weapons/README.md", False)

    [done] = transcript.apply_event(_tool_result("t1", [{"type": "text", "text": "# Carbine"}]))
    assert (done.index, done.is_done, done.tool_output) == (tool.index, True, "# Carbine")
    assert [item.kind for item in transcript.items] == [ChatItemKind.ASSISTANT, ChatItemKind.TOOL]


def test_images_a_tool_returns_are_saved_beside_the_transcript(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "session", ROOT)
    transcript.apply_event(_tool_use("t1", "mcp__blender__render_thumbnail_to_path", {"path": "/tmp/x.png"}))
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PIXEL}}
    [done] = transcript.apply_event(_tool_result("t1", [image]))

    [saved] = done.images
    assert saved.parent == tmp_path / "session"
    assert saved.read_bytes() == base64.b64decode(_PIXEL)


def test_a_failed_tool_and_a_one_line_summary(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "session", ROOT)
    code = "import bpy\nfor obj in bpy.data.objects:\n    print(obj.name)"
    [tool] = transcript.apply_event(_tool_use("t1", "mcp__blender__execute_blender_code", {"code": code}))
    assert tool.tool_summary == "import bpy"

    [done] = transcript.apply_event(_tool_result("t1", "Traceback ...", is_error=True))
    assert done.is_error
    assert done.tool_output == "Traceback ..."


def test_the_file_is_folded_back_with_later_lines_winning(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "session", ROOT)
    transcript.add(ChatItemKind.USER, text="Longer magazine")
    transcript.apply_event(_tool_use("t1", "Bash", {"command": "ls"}))
    transcript.apply_event(_tool_result("t1", "Carbine.blend"))
    # A line cut short by a crash is skipped; what came before it survives.
    with (tmp_path / "session.jsonl").open("a") as transcript_file:
        transcript_file.write('{"index": 9, "kind": "assist')

    items = read_transcript(tmp_path / "session.jsonl")
    assert [(item.index, item.kind) for item in items] == [(0, ChatItemKind.USER), (1, ChatItemKind.TOOL)]
    assert items[1].tool_output == "Carbine.blend"

    reopened = Transcript(tmp_path, "session", ROOT)
    assert reopened.add(ChatItemKind.NOTICE, text="again").index == 2
