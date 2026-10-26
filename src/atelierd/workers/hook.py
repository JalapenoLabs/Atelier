"""The scope policy's `PreToolUse` hook: `python -m atelierd.workers.hook <policy.json>`.

Claude Code runs it before every tool call, passing the call as JSON on stdin, including calls the user's own allow
rules would let through without a prompt. It answers `allow` for calls inside the worker's scope and `ask` for the
rest, which sends them on to the permission-prompt tool (`mcp__atelier__approve`), and so to the reviewer as an
approval card. Anything that goes wrong here (an unreadable policy, a malformed call) answers `ask` too: the hook
never lets a call through that it could not check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from atelierd.workers.policy import PolicyDecision, ScopePolicy, evaluate


def decide(policy_path: Path, payload_text: str) -> PolicyDecision:
    """Decide one call from the hook's stdin, escalating whatever cannot be checked."""
    try:
        policy = ScopePolicy.model_validate_json(policy_path.read_text(encoding="utf-8"))
        payload = json.loads(payload_text)
        tool_name = str(payload["tool_name"])
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            raise ValueError(f"tool_input is a {type(tool_input).__name__}, not an object")
    except (OSError, ValidationError, ValueError, KeyError, TypeError) as error:
        return PolicyDecision(is_allowed=False, reason=f"Atelier could not check this call ({error})")
    return evaluate(policy, tool_name, tool_input)


def main() -> int:
    """Answer Claude Code's hook call on stdout."""
    if len(sys.argv) != 2:
        print("usage: python -m atelierd.workers.hook <policy.json>", file=sys.stderr)
        return 2
    decision = decide(Path(sys.argv[1]), sys.stdin.read())
    answer = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if decision.is_allowed else "ask",
            "permissionDecisionReason": decision.reason,
        }
    }
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
