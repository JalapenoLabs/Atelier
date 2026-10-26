#!/usr/bin/env python3
"""A stand-in for `claude --print --input-format stream-json --output-format stream-json`, for the worker tests.

It speaks Claude Code's stream-json closely enough for a worker: a `system/init` event once a session starts,
`assistant` and `user` events for text and tool calls, a `result` to end every turn, and a `control_response` to
an interrupt. Instead of a model, the last paragraph of each prompt is a script, one command per line:

- `SAY <text>`: write text.
- `WRITE <path> <text>`: call the Write tool. The stub runs the PreToolUse hook from `--settings`, and when it
  answers `ask`, calls the permission-prompt tool from `--mcp-config` over HTTP, exactly as Claude Code would; the
  file is written only when allowed.
- `READ <path>`: call the Read tool; a PNG comes back as an image block.
- `SLEEP <seconds>`: work that an interrupt stops.
- `CRASH`: exit at once, mid-turn.

Every argument list it was started with is appended to `$FAKE_CLAUDE_LOG` as a JSON line, so tests can check them.
Standard library only, so it runs under any Python 3.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

type Message = dict[str, Any] | None
type ToolOutcome = tuple[str | list[dict[str, Any]], bool]


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def argument_after(flag: str) -> str | None:
    arguments = sys.argv[1:]
    return arguments[arguments.index(flag) + 1] if flag in arguments else None


def read_stdin(messages: queue.Queue[Message]) -> None:
    for line in sys.stdin:
        if line.strip():
            messages.put(json.loads(line))
    messages.put(None)


class Turn:
    def __init__(self, session_id: str, messages: queue.Queue[Message]) -> None:
        self.session_id = session_id
        self.messages = messages
        self.is_interrupted = False
        mcp_path = argument_after("--mcp-config")
        settings_path = argument_after("--settings")
        self.mcp: dict[str, Any] = {}
        self.settings: dict[str, Any] = {}
        if mcp_path:
            with open(mcp_path) as mcp_file:
                self.mcp = json.load(mcp_file)["mcpServers"]
        if settings_path:
            with open(settings_path) as settings_file:
                self.settings = json.load(settings_file)

    def check_interrupt(self) -> None:
        try:
            message = self.messages.get_nowait()
        except queue.Empty:
            return
        if message and message.get("type") == "control_request":
            self.is_interrupted = True
            response = {"subtype": "success", "request_id": message["request_id"]}
            emit({"type": "control_response", "response": response})

    def tool_call(self, name: str, tool_input: dict[str, Any], run: Callable[[], ToolOutcome]) -> None:
        tool_use_id = "toolu_" + uuid.uuid4().hex[:12]
        tool_use = {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
        emit({"type": "assistant", "message": {"content": [tool_use]}})
        behavior, reason = self.permission(name, tool_input, tool_use_id)
        content, is_error = run() if behavior == "allow" else (reason, True)
        tool_result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}
        emit({"type": "user", "message": {"content": [tool_result]}})

    def permission(self, name: str, tool_input: dict[str, Any], tool_use_id: str) -> tuple[str, str]:
        """Ask the hook, then the permission-prompt tool when the hook says `ask`, as Claude Code does."""
        hook_answer = "ask"
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
                "cwd": os.getcwd(),
            }
        )
        for matcher in self.settings.get("hooks", {}).get("PreToolUse", []):
            for hook in matcher["hooks"]:
                completed = subprocess.run(
                    hook["command"], shell=True, input=payload, capture_output=True, text=True, check=True
                )
                hook_answer = json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"]
        if hook_answer == "allow":
            return "allow", ""

        server = self.mcp["atelier"]
        arguments = {"tool_name": name, "input": tool_input, "tool_use_id": tool_use_id}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "approve", "arguments": arguments},
        }
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        request = urllib.request.Request(
            server["url"],
            data=json.dumps(body).encode(),
            headers={**headers, **server.get("headers", {})},
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read())["result"]
        answer = json.loads(result["content"][0]["text"])
        return str(answer["behavior"]), str(answer.get("message", ""))

    def run(self, script: str) -> None:
        for line in script.splitlines():
            words = line.split(" ", 2)
            command = words[0]
            if command == "SAY":
                emit({"type": "assistant", "message": {"content": [{"type": "text", "text": line[4:]}]}})
            elif command == "WRITE":
                self.write(words[1], words[2] if len(words) > 2 else "")
            elif command == "READ":
                self.read(words[1])
            elif command == "SLEEP":
                deadline = time.monotonic() + float(words[1])
                while time.monotonic() < deadline and not self.is_interrupted:
                    self.check_interrupt()
                    time.sleep(0.05)
            elif command == "CRASH":
                print("fake claude crashed on purpose", file=sys.stderr)
                os._exit(3)
            if self.is_interrupted:
                break
        subtype = "error_during_execution" if self.is_interrupted else "success"
        result = {"type": "result", "subtype": subtype, "is_error": self.is_interrupted, "duration_ms": 10}
        emit({**result, "total_cost_usd": 0, "session_id": self.session_id})

    def write(self, path: str, content: str) -> None:
        def run() -> ToolOutcome:
            with open(path, "w") as target:
                target.write(content)
            return f"File created successfully at: {path}", False

        self.tool_call("Write", {"file_path": path, "content": content}, run)

    def read(self, path: str) -> None:
        def run() -> ToolOutcome:
            with open(path, "rb") as source:
                data = source.read()
            if path.endswith(".png"):
                image = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(data).decode()}
                return [{"type": "image", "source": image}], False
            return data.decode(), False

        self.tool_call("Read", {"file_path": path}, run)


def main() -> int:
    log_path = os.environ.get("FAKE_CLAUDE_LOG")
    if log_path:
        with open(log_path, "a") as log:
            log.write(json.dumps(sys.argv[1:]) + "\n")
    session_id = argument_after("--session-id") or argument_after("--resume") or str(uuid.uuid4())
    messages: queue.Queue[Message] = queue.Queue()
    threading.Thread(target=read_stdin, args=(messages,), daemon=True).start()
    is_started = False
    while True:
        message = messages.get()
        if message is None:
            return 0
        if message.get("type") != "user":
            continue
        if not is_started:
            is_started = True
            emit({"type": "system", "subtype": "init", "session_id": session_id})
        text = message["message"]["content"][0]["text"]
        # The brief comes first; the reviewer's words (the script) are the last paragraph.
        Turn(session_id, messages).run(text.split("\n\n")[-1])


if __name__ == "__main__":
    sys.exit(main())
