"""The manager MCP end to end: a real MCP client over the daemon's HTTP front door at `/mcp`.

Every call goes through the port the daemon serves, so the mount, the lifespan and the SDK's transport are covered,
not only the tools. Claude Code is the stream-json stub (see `test_workers.py`); Blender's standard renders and export
conversions are replaced by fakes that write what the real scripts would, so images flow through without Blender.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import RunningDaemon, wait_for
from mcp import Client
from mcp_types import CallToolResult, ImageContent, TextContent
from PIL import Image

from atelierd.catalog import Catalog, SourceStatus
from atelierd.exports import cache as cache_module
from atelierd.revisions import STANDARD_VIEWS
from atelierd.revisions import renders as renders_module
from atelierd.rpc import protocol
from atelierd.rpc.client import RpcClient
from atelierd.workers import Approval, ApprovalBroker, FeedbackRequest, Markup, Pin

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
CARBINE_EXPORT = "SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx"

EXPECTED_TOOLS = {
    "list_projects",
    "list_sources",
    "get_source",
    "get_renders",
    "compare_revisions",
    "get_transcript",
    "list_feedback",
    "list_references",
    "pin_references",
    "send_prompt",
    "get_worker",
    "list_workers",
    "interrupt_worker",
    "restore_revision",
}


# ####################### #
#        FIXTURES         #
# ####################### #


def _fake_render(script: Path, script_arguments: list[str]) -> str:
    """Write every standard view as a 1024 px PNG coloured by the export's bytes, as the render script would."""
    export_file, output_folder, name, _ = script_arguments
    digest = hashlib.sha256(Path(export_file).read_bytes()).digest()
    for view in STANDARD_VIEWS:
        Image.new("RGB", (1024, 1024), (digest[0], digest[1], digest[2])).save(
            Path(output_folder) / f"{name}-{view}.png"
        )
    return ""


def _fake_conversion(script: Path, script_arguments: list[str]) -> str:
    """Write a `.glb` and its stats, as the conversion script would."""
    _, glb_path, stats_path, version = script_arguments
    Path(glb_path).write_bytes(b"glTF")
    stats = {
        "converter_version": int(version),
        "triangles": 1234,
        "materials": ["M_Carbine"],
        "meshes": ["SK_Carbine"],
        "collision_hulls": [],
        "is_skinned": True,
        "bounds_min_cm": [-40.0, -5.0, -10.0],
        "bounds_max_cm": [40.0, 5.0, 10.0],
    }
    Path(stats_path).write_text(json.dumps(stats))
    return ""


class Manager:
    """A manager session: every call is a fresh MCP client over HTTP, as a Claude Code tool call is."""

    def __init__(self, daemon: RunningDaemon, project_root: Path) -> None:
        self.daemon = daemon
        self.url = f"http://127.0.0.1:{daemon.daemon.http_port}/mcp"
        self.root = project_root
        self.rpc: RpcClient = daemon.client()
        self.project = self.rpc.call(
            protocol.OPEN_PROJECT, protocol.OpenProjectParams(reference=str(project_root))
        ).name
        # Opening records each source's baseline in the background; the tests start from it.
        wait_for(
            lambda: self.rpc.call(protocol.GET_HISTORY, protocol.SourceParams(project=self.project, source=CARBINE)),
            lambda history: bool(history.revisions),
        )

    def call(self, tool: str, **arguments: Any) -> CallToolResult:
        async def call_tool() -> CallToolResult:
            async with Client(self.url) as client:
                return await client.call_tool(tool, arguments)

        return asyncio.run(call_tool())

    def text(self, tool: str, **arguments: Any) -> str:
        result = self.call(tool, **arguments)
        assert not result.is_error, _texts(result)
        return "\n".join(_texts(result))

    def json(self, tool: str, **arguments: Any) -> Any:
        return json.loads(self.text(tool, **arguments))

    def error(self, tool: str, **arguments: Any) -> str:
        result = self.call(tool, **arguments)
        assert result.is_error, _texts(result)
        return "\n".join(_texts(result))

    def wait_idle(self, source: str = "Carbine") -> dict[str, Any]:
        state: dict[str, Any] = self.json("get_worker", source=source, wait_seconds=30)
        assert state["info"]["status"] in ("idle", "stopped"), state
        return state


def _texts(result: CallToolResult) -> list[str]:
    return [block.text for block in result.content if isinstance(block, TextContent)]


def _images(result: CallToolResult) -> list[Image.Image]:
    return [
        Image.open(io.BytesIO(base64.b64decode(block.data)))
        for block in result.content
        if isinstance(block, ImageContent)
    ]


@pytest.fixture
def manager(daemon: RunningDaemon, project_root: Path, monkeypatch: pytest.MonkeyPatch) -> Manager:
    monkeypatch.setattr(renders_module, "run_blender_script", _fake_render)
    monkeypatch.setattr(cache_module, "run_blender_script", _fake_conversion)
    return Manager(daemon, project_root)


# ####################### #
#       BOUNDARIES        #
# ####################### #


class TestBoundaries:
    def test_the_server_offers_exactly_the_reviewed_tools(self, manager: Manager) -> None:
        async def list_tools() -> list[Any]:
            async with Client(manager.url) as client:
                return (await client.list_tools()).tools

        tools = asyncio.run(list_tools())
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        for tool in tools:
            assert tool.description, tool.name
            properties = set(tool.input_schema.get("properties", {}))
            # Status is a filter on a listing, never something a tool sets; nothing takes an approval.
            assert "status" not in properties or tool.name == "list_sources"
            assert not any("approv" in name for name in properties), tool.name

    def test_a_manager_never_answers_an_approval_or_sets_a_status(
        self, manager: Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forbidden: list[str] = []

        def answer(self: ApprovalBroker, approval_id: str, is_allowed: bool, message: str = "") -> Approval:
            forbidden.append(f"answer {approval_id}")
            raise AssertionError("A manager answered an approval")

        def set_status(self: Catalog, source_path: str, status: SourceStatus) -> None:
            forbidden.append(f"status {status}")

        outside = manager.root / "Content" / "SK_Carbine.uasset"
        manager.text("send_prompt", source="Carbine", prompt=f"WRITE {outside} nope")
        state = manager.json("get_worker", source="Carbine", wait_seconds=30)
        [approval] = state["pending_approvals"]
        assert approval["tool_name"] == "Write"
        assert "Content/" in approval["reason"]
        assert "id" not in approval
        assert "approval" in state["note"]

        monkeypatch.setattr(ApprovalBroker, "answer", answer)
        monkeypatch.setattr(Catalog, "set_status", set_status)
        for tool, arguments in (
            ("list_projects", {}),
            ("list_sources", {}),
            ("get_source", {"source": "Carbine"}),
            ("get_worker", {"source": "Carbine"}),
            ("list_workers", {}),
            ("get_transcript", {"source": "Carbine"}),
            ("list_feedback", {"source": "Carbine"}),
            ("list_references", {}),
        ):
            assert not manager.call(tool, **arguments).is_error, tool
        assert "has a turn running" in manager.error("send_prompt", source="Carbine", prompt="SAY again")
        assert not forbidden
        [still_pending] = manager.rpc.call(protocol.LIST_APPROVALS, protocol.ApprovalsParams(project=manager.project))
        assert still_pending.tool_name == "Write"
        monkeypatch.undo()

        # Only the reviewer answers, through the window's RPC; the manager then sees the turn end.
        manager.rpc.call(
            protocol.ANSWER_APPROVAL,
            protocol.AnswerApprovalParams(approval_id=still_pending.id, is_allowed=False, message="Not in Content"),
        )
        assert not manager.wait_idle()["pending_approvals"]
        assert not outside.exists()


# ####################### #
#         CATALOG         #
# ####################### #


class TestCatalog:
    def test_projects_are_listed_and_the_only_open_one_is_the_default(self, manager: Manager) -> None:
        assert manager.json("list_projects") == [{"name": manager.project, "is_open": True}]
        named = manager.json("list_sources", project=manager.project)
        assert manager.json("list_sources") == named
        assert CARBINE in [source["path"] for source in named]

    def test_sources_are_searched_by_words_of_their_path(self, manager: Manager) -> None:
        [carbine] = manager.json("list_sources", query="weapons CARBINE")
        assert carbine == {"path": CARBINE, "status": "draft", "exports": 1}
        assert manager.json("list_sources", status="approved") == []

    def test_a_source_comes_with_its_export_stats_and_revisions(self, manager: Manager) -> None:
        detail = manager.json("get_source", source="carbine")
        assert detail["source"]["path"] == CARBINE
        [export] = detail["exports"]
        assert export["path"] == CARBINE_EXPORT
        assert export["stats"]["triangles"] == 1234
        assert detail["revisions"][0]["number"] == 1
        assert detail["revisions"][0]["trigger"] == "baseline"
        assert detail["worker"]["status"] == "stopped"

    def test_names_that_match_nothing_or_several_say_so(self, manager: Manager) -> None:
        assert "No source" in manager.error("get_source", source="Nonexistent")
        assert "matches several sources" in manager.error("get_source", source="Weapons")
        assert "has no r9" in manager.error("get_renders", source="Carbine", revision=9)


# ####################### #
#         REVIEW          #
# ####################### #


class TestReview:
    def test_renders_not_made_yet_are_made_and_returned_scaled_down(self, manager: Manager) -> None:
        result = manager.call("get_renders", source="Carbine", views=["hero", "clay"], size=256)
        assert not result.is_error, _texts(result)
        texts = _texts(result)
        assert texts[0].startswith("r1 (baseline")
        assert CARBINE_EXPORT in texts[0]
        assert texts[1:] == ["hero:", "clay:"]
        images = _images(result)
        assert [(image.format, image.size) for image in images] == [("JPEG", (256, 256))] * 2

    def test_two_revisions_compare_side_by_side(self, manager: Manager) -> None:
        export = manager.root / CARBINE_EXPORT
        manager.text("send_prompt", source="Carbine", prompt=f"WRITE {export} a rebuilt carbine")
        assert manager.wait_idle()["last_revision"] == 2

        result = manager.call("compare_revisions", source="Carbine", views=["hero", "side"])
        assert not result.is_error, _texts(result)
        assert _texts(result)[0].startswith("A (left): r1 (baseline")
        assert "B (right): r2 (worker-turn" in _texts(result)[0]
        hero, side = _images(result)
        assert hero.size == side.size == (1024, 512)
        # The fake renders are coloured by the export's bytes, so the halves differ.
        left = hero.convert("RGB").getpixel((256, 400))
        right = hero.convert("RGB").getpixel((768, 400))
        assert left != right

    def test_a_transcript_is_one_short_line_per_item_without_the_brief(self, manager: Manager) -> None:
        manager.text("send_prompt", source="Carbine", prompt="SAY " + "very long words " * 100)
        manager.wait_idle()
        transcript = manager.text("get_transcript", source="Carbine")
        lines = transcript.splitlines()
        assert lines[0].startswith(f"{CARBINE}, session ")
        assert [line.split(" ", 3)[2].rstrip(":") for line in lines[1:]] == ["notice", "user", "assistant", "result"]
        assert all(len(line) < 500 for line in lines)
        assert "Source: SourceArt" not in transcript
        assert lines[-1].endswith("Recorded r1") or "Done" in lines[-1]
        assert manager.text("get_transcript", source="Carbine", last=1).count("\n") == 1

    def test_feedback_lists_its_pins_and_can_show_its_image(self, manager: Manager, tmp_path: Path) -> None:
        capture = tmp_path / "capture.png"
        annotated = tmp_path / "annotated.png"
        Image.new("RGB", (800, 600), (200, 30, 30)).save(capture)
        Image.new("RGB", (800, 600), (30, 200, 30)).save(annotated)
        request = FeedbackRequest(
            source=CARBINE,
            capture_path=capture,
            annotated_path=annotated,
            markup=Markup(pins=(Pin(number=1, x=0.5, y=0.5, note="Longer magazine"),)),
        )
        feedback = manager.rpc.call(
            protocol.CREATE_FEEDBACK, protocol.CreateFeedbackParams(project=manager.project, request=request)
        )

        result = manager.call("list_feedback", source="Carbine", include_images=True, size=400)
        [summary] = [json.loads(text) for text in _texts(result)]
        assert summary["id"] == feedback.id
        assert summary["pins"][0]["note"] == "Longer magazine"
        [image] = _images(result)
        assert image.size == (400, 300)


# ####################### #
#   REFERENCES, WORKERS   #
# ####################### #


class TestWorkers:
    def test_references_are_searched_and_only_real_ones_pinned(self, manager: Manager) -> None:
        [palette] = manager.json("list_references", query="palette")
        assert palette["tag"] == "Palette"
        assert "Not references" in manager.error("pin_references", source="Carbine", references=["nope.png"])
        assert manager.json("pin_references", source="Carbine", references=[palette["path"]]) == [palette["path"]]
        assert manager.json("get_worker", source="Carbine")["info"]["pinned_references"] == [palette["path"]]

    def test_a_prompt_runs_a_turn_the_manager_can_wait_for(self, manager: Manager) -> None:
        readme = manager.root / "SourceArt/Weapons/Carbine/README.md"
        sent = manager.json("send_prompt", source="Carbine", prompt=f"WRITE {readme} A sharper README")
        assert sent["info"]["status"] in ("starting", "running")
        assert "get_worker" in sent["note"]

        state = manager.wait_idle()
        assert readme.read_text() == "A sharper README"
        assert state["last_revision"] == 2
        assert state["last_result"].startswith("Done")
        [worker] = manager.json("list_workers")["workers"]
        assert worker["source"] == CARBINE

    def test_a_new_session_starts_a_fresh_chat(self, manager: Manager) -> None:
        manager.text("send_prompt", source="Carbine", prompt="SAY first")
        first_session = manager.wait_idle()["info"]["session_id"]
        manager.text("send_prompt", source="Carbine", prompt="SAY second", new_session=True)
        assert manager.wait_idle()["info"]["session_id"] != first_session

    def test_interrupt_ends_a_running_turn(self, manager: Manager) -> None:
        manager.text("send_prompt", source="Carbine", prompt="SLEEP 30")
        still_running = manager.json("get_worker", source="Carbine", wait_seconds=1)
        assert still_running["note"] == "The turn is still running."
        manager.text("interrupt_worker", source="Carbine")
        assert manager.wait_idle()["last_result"].startswith("Interrupted")

    def test_restore_writes_an_earlier_revision_back_and_is_refused_during_a_turn(self, manager: Manager) -> None:
        readme = manager.root / "SourceArt/Weapons/Carbine/README.md"
        original = readme.read_text()
        manager.text("send_prompt", source="Carbine", prompt=f"WRITE {readme} Worse")
        manager.wait_idle()

        manager.text("send_prompt", source="Carbine", prompt="SLEEP 30")
        assert "has a worker running" in manager.error("restore_revision", source="Carbine", revision=1)
        manager.text("interrupt_worker", source="Carbine")
        manager.wait_idle()

        restored = manager.json("restore_revision", source="Carbine", revision=1)
        assert restored["restored_to"] == 1
        assert restored["pre_restore"] == 2
        assert "SourceArt/Weapons/Carbine/README.md" in restored["written"]
        assert readme.read_text() == original
