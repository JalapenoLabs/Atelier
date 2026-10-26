"""The scope policy: what a worker may do without asking, and the PreToolUse hook that applies it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from atelierd.catalog import Catalog
from atelierd.project import open_project
from atelierd.workers import ScopePolicy, build_scope_policy, evaluate

ROOT = Path("/projects/Fall")


@pytest.fixture
def policy(tmp_path: Path) -> ScopePolicy:
    return ScopePolicy(
        project_root=ROOT,
        state_dir=ROOT / ".atelier",
        read_folders=(Path("/home/me/.claude"),),
        source="SourceArt/Weapons/Carbine/Carbine.blend",
        write_folders=("SourceArt/Weapons/Carbine",),
        write_files=("SourceArt/Weapons/Shared/Carbine_Export.fbx",),
        shared_globs=("SourceArt/_lib/**", "SourceArt/**/_scripts/*.py", "SourceArt/Weapons/Carbine/palette.png"),
        shared_files=("SourceArt/_lib/run_asset.py",),
    )


ALLOWED = [
    ("Read", {"file_path": f"{ROOT}/SourceArt/Weapons/Sidearm/README.md"}),
    ("Read", {"file_path": f"{ROOT}/.atelier/feedback/SourceArt/Weapons/Carbine/Carbine/1/annotated.png"}),
    ("Read", {"file_path": "/home/me/.claude/docs/python.md"}),
    ("Grep", {"pattern": "magazine"}),
    ("Glob", {"pattern": "**/*.py", "path": "SourceArt"}),
    ("Write", {"file_path": f"{ROOT}/SourceArt/Weapons/Carbine/README.md"}),
    ("Edit", {"file_path": "SourceArt/Weapons/Carbine/build_carbine.py"}),
    ("Write", {"file_path": f"{ROOT}/SourceArt/Weapons/Shared/Carbine_Export.fbx"}),
    ("mcp__blender__execute_blender_code", {"code": "import bpy"}),
    ("mcp__atelier__approve", {}),
    ("TodoWrite", {"todos": []}),
    ("Bash", {"command": "ls SourceArt/Weapons && head -5 SourceArt/Weapons/Carbine/README.md | wc -l"}),
    (
        "Bash",
        {
            "command": "PATH=$(echo \"$PATH\" | tr ':' '\\n' | grep -v mise | paste -sd:) blender -b --factory-startup "
            "--python SourceArt/_lib/run_asset.py -- SourceArt/Weapons/Carbine/Carbine.blend"
        },
    ),
    ("Bash", {"command": "echo note > SourceArt/Weapons/Carbine/notes.txt 2>&1"}),
    ("Bash", {"command": "mkdir -p SourceArt/Weapons/Carbine/Review && rm SourceArt/Weapons/Carbine/old.png"}),
    ("Bash", {"command": "(cd SourceArt && ls) > /dev/null"}),
]

ESCALATED = [
    ("Read", {"file_path": "/etc/passwd"}, "Reads outside the project"),
    ("Read", {"file_path": f"{ROOT}/Content/Weapons/SK_Carbine.uasset"}, "Content/"),
    ("Glob", {"pattern": "/home/me/.ssh/*"}, "Reads outside the project"),
    ("Write", {"file_path": f"{ROOT}/SourceArt/Weapons/Sidearm/README.md"}, "outside the source's scope"),
    ("Edit", {"file_path": f"{ROOT}/SourceArt/_lib/run_asset.py"}, "shared path"),
    ("Edit", {"file_path": f"{ROOT}/SourceArt/Weapons/_scripts/weapon_kit.py"}, "shared path"),
    ("Write", {"file_path": f"{ROOT}/SourceArt/Weapons/Carbine/palette.png"}, "shared path"),
    ("Write", {"file_path": f"{ROOT}/SourceArt/Weapons/Carbine/../Sidearm/x.py"}, "outside the source's scope"),
    ("Write", {"file_path": f"{ROOT}/.atelier/state.db"}, "outside the source's scope"),
    ("Write", {"file_path": f"{ROOT}/Content/Weapons/x.uasset"}, "Content/"),
    ("WebFetch", {"url": "https://example.com"}, "Network access"),
    ("mcp__github__create_issue", {}, "outside the worker's own"),
    ("Bash", {"command": "curl https://example.com"}, "Network access"),
    ("Bash", {"command": "python3 build.py"}, "Runs `python3`"),
    ("Bash", {"command": "blender SourceArt/Weapons/Carbine/Carbine.blend"}, "windowed Blender"),
    ("Bash", {"command": "echo x > /tmp/elsewhere"}, "outside the source's scope"),
    ("Bash", {"command": "rm -rf ../"}, "outside the source's scope"),
    ("Bash", {"command": "find . -name '*.blend1' -delete"}, "`find` that writes"),
    ("Bash", {"command": "for name in a b; do rm $name; done"}, "a loop"),
    ("Bash", {"command": "cd SourceArt/Weapons/Sidearm && rm build.py"}, "changes directory and then writes"),
    ("Bash", {"command": "cat $HOME/.ssh/id_ed25519"}, "computed path"),
    ("Bash", {"command": "echo 'unterminated"}, "cannot read"),
    ("SomethingNew", {}, "not a tool the scope policy knows"),
]


@pytest.mark.parametrize(("tool_name", "tool_input"), ALLOWED)
def test_calls_inside_the_scope_run(policy: ScopePolicy, tool_name: str, tool_input: dict[str, object]) -> None:
    decision = evaluate(policy, tool_name, tool_input)
    assert decision.is_allowed, decision.reason


@pytest.mark.parametrize(("tool_name", "tool_input", "reason"), ESCALATED)
def test_calls_outside_the_scope_escalate(
    policy: ScopePolicy,
    tool_name: str,
    tool_input: dict[str, object],
    reason: str,
) -> None:
    decision = evaluate(policy, tool_name, tool_input)
    assert not decision.is_allowed
    assert reason in decision.reason


def test_a_builder_outside_the_source_folder_is_shared(project_root: Path, tmp_path: Path) -> None:
    project = open_project(project_root, state_dir=tmp_path / "state")
    Catalog(project).scan()
    carbine = next(source for source in Catalog(project).list_sources() if source.path.endswith("Carbine.blend"))
    policy = build_scope_policy(project, carbine)

    assert policy.write_folders == ("SourceArt/Weapons/Carbine",)
    assert carbine.builder in policy.write_files
    assert (
        evaluate(policy, "Write", {"file_path": str(project_root / "SourceArt/_lib/run_asset.py")}).is_allowed is False
    )


class TestHook:
    def _run(self, policy_path: Path, payload: str) -> dict[str, str]:
        completed = subprocess.run(
            [sys.executable, "-m", "atelierd.workers.hook", str(policy_path)],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        answer: dict[str, str] = json.loads(completed.stdout)["hookSpecificOutput"]
        return answer

    def test_the_hook_allows_in_scope_and_asks_otherwise(self, policy: ScopePolicy, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(policy.model_dump_json())
        inside = {"tool_name": "Write", "tool_input": {"file_path": f"{ROOT}/SourceArt/Weapons/Carbine/README.md"}}
        outside = {"tool_name": "WebSearch", "tool_input": {"query": "concept art"}}

        assert self._run(policy_path, json.dumps(inside))["permissionDecision"] == "allow"
        answer = self._run(policy_path, json.dumps(outside))
        assert answer["hookEventName"] == "PreToolUse"
        assert (answer["permissionDecision"], answer["permissionDecisionReason"]) == ("ask", "Network access")

    def test_the_hook_asks_when_it_cannot_check(self, tmp_path: Path) -> None:
        answer = self._run(tmp_path / "missing.json", json.dumps({"tool_name": "Read", "tool_input": {}}))
        assert answer["permissionDecision"] == "ask"
        assert "could not check" in answer["permissionDecisionReason"]
