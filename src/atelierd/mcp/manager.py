"""The manager MCP: the tools a managing Claude Code session uses to review a project's art and direct its workers.

It is served at `/mcp` on the daemon's HTTP front door (`app.py`) and calls the same core the GUI's RPC calls, so a
manager sees exactly what the window shows. Each tool resolves its project and source, then makes its core calls in a
thread (`asyncio.to_thread`), because every one of them may block on SQLite, hashing or Blender.

What a manager may not do is enforced by what this module calls, and tested:

- It never answers a worker's approval request. It reads `approvals.list_pending` only, and the approvals it returns
  carry no id, so nothing it receives could answer one.
- It never sets a source's status. There is no status tool at all: `approved` is the human reviewer's sign-off, and
  the other statuses are the reviewer's to set too, in the window.

Tools return compact models (`models.py`) and images scaled down by `images.py`, because everything a tool returns
lands in the manager's context. Waiting is built in where a manager would otherwise poll: `get_worker` can wait for a
turn to end, and the render tools wait for renders that are on their way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from functools import partial
from pathlib import PurePosixPath
from typing import Annotated, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from pydantic import Field
from pydantic_core import to_json

from atelierd.catalog import Reference, SourceStatus
from atelierd.config import ConfigError
from atelierd.constants import (
    MANAGER_IMAGE_EDGE,
    MANAGER_IMAGE_EDGE_MAX,
    MANAGER_IMAGE_EDGE_MIN,
    MANAGER_MCP_SERVER_NAME,
    MANAGER_RENDER_WAIT_S,
    MANAGER_TRANSCRIPT_ITEMS,
    MANAGER_TRANSCRIPT_ITEMS_MAX,
    MANAGER_WAIT_MAX_S,
)
from atelierd.daemon.core import AtelierCore, ProjectRuntime
from atelierd.exports import ExportConversionError, convert_export
from atelierd.mcp.images import scaled_jpeg, side_by_side_jpeg
from atelierd.mcp.models import (
    ExportDetail,
    FeedbackSummary,
    PendingApproval,
    ProjectSummary,
    ProjectWorkers,
    RestoreSummary,
    RevisionSummary,
    SourceDetail,
    SourceSummary,
    WorkerState,
)
from atelierd.registry import RegistryError
from atelierd.revisions import (
    STANDARD_VIEWS,
    RenderState,
    Revision,
    SourceBusyError,
    SourceHistory,
    StandardRenderSet,
)
from atelierd.workers import Approval, ChatItem, ChatItemKind, WorkerBusyError

logger = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "Atelier is the review studio for a game project's 3D art. Each source is one .blend under SourceArt/ with its "
    "builder script, README and FBX exports; each source can have a Claude Code worker that edits it. Atelier keeps "
    "a revision (r1, r2, ...) of every source after every change and renders a fixed set of standard views of each "
    "export (hero, front, side, top, clay) for every revision. A review loop: list_sources to find a source, "
    "get_renders to look at it, send_prompt with a concrete critique, get_worker with wait_seconds to wait for the "
    "turn to end, then get_renders or compare_revisions on the revision the turn recorded. A worker's tool calls "
    "outside its source's folder wait for the human reviewer's approval in the Atelier window; you can see them in "
    "get_worker but never answer them, so tell the user when a turn is waiting on one. A source's review status, "
    "and approving a source, are the user's alone. Images cost context: ask only for the views you need."
)

# The views a manager can ask for; the same names the standard renders are written under.
type StandardView = Literal["hero", "front", "side", "top", "clay"]
if set(get_args(StandardView.__value__)) != set(STANDARD_VIEWS):
    raise RuntimeError("StandardView must name exactly the standard render views")

# Failures the core raises on purpose, for a bad name or a state that refuses the call. Their message goes back to
# the manager to act on; anything else is a bug, and the MCP layer keeps its details out of the reply.
_EXPECTED_ERRORS = (
    ConfigError,
    ExportConversionError,
    FileNotFoundError,
    KeyError,
    NotADirectoryError,
    RegistryError,
    SourceBusyError,
    ValueError,
    WorkerBusyError,
)
# How long one chat item's text may be in a transcript read.
_TRANSCRIPT_TEXT_LIMIT = 400
_TRANSCRIPT_TOOL_LIMIT = 160
_WAIT_POLL_S = 1.0

# -----------------------------------------------------------------------------
# Arguments, described for the model that reads them
# -----------------------------------------------------------------------------

ProjectArgument = Annotated[
    str | None,
    Field(
        description="The project, by name (list_projects) or folder path. May be left out while the daemon has "
        "exactly one project open, or the machine knows exactly one.",
    ),
]
SourceArgument = Annotated[
    str,
    Field(
        description="The source: its .blend path relative to the project, as list_sources gives it, or a part of "
        "its name that matches only it, such as 'Carbine'.",
    ),
]
RevisionArgument = Annotated[
    int | None,
    Field(description="A revision number, the N of rN. Defaults to the newest.", ge=1),
]
ExportArgument = Annotated[
    str | None,
    Field(
        description="Which export to show, by path or file name. Defaults to the revision's first export; the "
        "reply names the others.",
    ),
]
ViewsArgument = Annotated[
    tuple[StandardView, ...],
    Field(
        description="Standard views to return: hero (perspective, three-quarter), front, side, top (orthographic), "
        "clay (hero with neutral grey materials, for form). Each view is one image.",
        min_length=1,
    ),
]
SizeArgument = Annotated[
    int,
    Field(
        description=f"Longest edge of each image in pixels; renders are {MANAGER_IMAGE_EDGE_MAX}. Larger costs more "
        "context.",
        ge=MANAGER_IMAGE_EDGE_MIN,
        le=MANAGER_IMAGE_EDGE_MAX,
    ),
]
RenderWaitArgument = Annotated[
    int,
    Field(
        description="How long to wait for renders still being made (a new revision's take a minute or so).",
        ge=0,
        le=MANAGER_WAIT_MAX_S,
    ),
]


def build_manager_mcp(core: AtelierCore) -> MCPServer:
    """Return the manager MCP server over `core`."""
    server: MCPServer = MCPServer(MANAGER_MCP_SERVER_NAME, instructions=_INSTRUCTIONS)

    # ####################### #
    #         CATALOG         #
    # ####################### #

    @server.tool(structured_output=False)
    async def list_projects() -> str:
        """List the projects this machine knows, and which the daemon has open."""

        def _work() -> list[ProjectSummary]:
            open_names = {runtime.project.name for runtime in core.open_projects()}
            names = sorted({*core.list_projects(), *open_names})
            return [ProjectSummary(name=name, is_open=name in open_names) for name in names]

        return await _run_as_json(_work)

    @server.tool(structured_output=False)
    async def list_sources(
        project: ProjectArgument = None,
        query: Annotated[
            str | None,
            Field(description="Only sources whose path holds every word of this, ignoring case, e.g. 'charm duck'."),
        ] = None,
        status: Annotated[SourceStatus | None, Field(description="Only sources with this review status.")] = None,
    ) -> str:
        """List a project's sources with their review status, export count and worker state."""

        def _work() -> list[SourceSummary]:
            runtime = _open_runtime(core, project)
            words = query.lower().split() if query else []
            worker_status_by_source = {info.source: info.status for info in runtime.workers.list_infos()}
            summaries: list[SourceSummary] = []
            for source in runtime.catalog.list_sources():
                path = source.path.lower()
                if status and source.status is not status:
                    continue
                if not all(word in path for word in words):
                    continue
                summaries.append(
                    SourceSummary(
                        path=source.path,
                        status=source.status,
                        exports=len(source.exports),
                        worker=worker_status_by_source.get(source.path),
                    )
                )
            return summaries

        return await _run_as_json(_work)

    @server.tool(structured_output=False)
    async def get_source(source: SourceArgument, project: ProjectArgument = None) -> str:
        """Get one source: its files, export stats, revisions with their render state, and its worker.

        Stats are each export's triangles, materials, meshes and size in cm. Measuring an export runs Blender once
        (cached after), so a large kit's first call can take a while.
        """

        def _work() -> SourceDetail:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            catalog_source = next(item for item in runtime.catalog.list_sources() if item.path == source_path)
            exports: list[ExportDetail] = []
            for export_path in catalog_source.exports:
                try:
                    converted = convert_export(runtime.project, export_path)
                except ExportConversionError as error:
                    logger.info(f"[Manager MCP] No stats for {export_path}: {error}")
                    exports.append(ExportDetail(path=export_path, stats_error=str(error)))
                    continue
                exports.append(ExportDetail(path=export_path, stats=converted.stats))
            history = runtime.revisions.history(source_path)
            return SourceDetail(
                source=catalog_source,
                exports=tuple(exports),
                revisions=_revision_summaries(history),
                restored_to=history.restore_mark.revision_number if history.restore_mark else None,
                worker=runtime.workers.get(source_path).info(),
                feedback=len(runtime.feedback.list_feedback(source_path)),
            )

        return await _run_as_json(_work)

    # ####################### #
    #         REVIEW          #
    # ####################### #

    @server.tool(structured_output=False)
    async def get_renders(
        source: SourceArgument,
        project: ProjectArgument = None,
        revision: RevisionArgument = None,
        export: ExportArgument = None,
        views: ViewsArgument = ("hero",),
        size: SizeArgument = MANAGER_IMAGE_EDGE,
        wait_seconds: RenderWaitArgument = MANAGER_RENDER_WAIT_S,
    ) -> list[str | Image]:
        """Look at the standard renders of one export of a revision, each requested view as an image.

        Renders not made yet are started and waited for, up to wait_seconds.
        """
        runtime, source_path = await _run(partial(_resolve, core, project, source))
        history = await _run(partial(runtime.revisions.history, source_path))
        with _refusals():
            chosen = _find_revision(history, revision)
            export_path, export_note = _choose_export(chosen, export)
        history = await _wait_for_renders(runtime, source_path, [(chosen, export_path)], wait_seconds)
        render_set = _render_set(history, chosen, export_path)

        content: list[str | Image] = [f"{_describe(chosen, history)}, export {export_path}{export_note}"]
        if render_set.state is not RenderState.DONE:
            content.append(_render_state_note(render_set))
            return content
        for view in views:
            image_path = render_set.images.get(view)
            if image_path is None:
                content.append(f"{view}: not rendered")
                continue
            data = await _run(partial(scaled_jpeg, image_path, size))
            content.extend([f"{view}:", Image(data=data, format="jpeg")])
        return content

    @server.tool(structured_output=False)
    async def compare_revisions(
        source: SourceArgument,
        project: ProjectArgument = None,
        revision_a: Annotated[
            int | None,
            Field(description="The earlier revision number, shown on the left. Defaults to the one before B.", ge=1),
        ] = None,
        revision_b: Annotated[
            int | None,
            Field(description="The later revision number, shown on the right. Defaults to the newest.", ge=1),
        ] = None,
        export: ExportArgument = None,
        views: ViewsArgument = ("hero",),
        size: SizeArgument = MANAGER_IMAGE_EDGE,
        wait_seconds: RenderWaitArgument = MANAGER_RENDER_WAIT_S,
    ) -> list[str | Image]:
        """Compare two revisions of a source: per view, one image with revision A on the left and B on the right.

        Each half is labelled and size pixels square. Renders not made yet are started and waited for, up to
        wait_seconds.
        """
        runtime, source_path = await _run(partial(_resolve, core, project, source))
        history = await _run(partial(runtime.revisions.history, source_path))
        with _refusals():
            later = _find_revision(history, revision_b)
            earlier = _find_revision(history, revision_a) if revision_a else _previous_revision(history, later)
            export_path, export_note = _choose_export(later, export)
        history = await _wait_for_renders(
            runtime,
            source_path,
            [(earlier, export_path), (later, export_path)],
            wait_seconds,
        )
        earlier_set = _render_set(history, earlier, export_path)
        later_set = _render_set(history, later, export_path)

        content: list[str | Image] = [
            f"A (left): {_describe(earlier, history)}. B (right): {_describe(later, history)}. "
            f"Export {export_path}{export_note}"
        ]
        for label, render_set in (("A", earlier_set), ("B", later_set)):
            if render_set.state is not RenderState.DONE:
                content.append(f"{label}: {_render_state_note(render_set)}")
        if earlier_set.state is not RenderState.DONE and later_set.state is not RenderState.DONE:
            return content
        for view in views:
            left = earlier_set.images.get(view)
            right = later_set.images.get(view)
            data = await _run(
                partial(side_by_side_jpeg, left, right, size, f"A r{earlier.number}", f"B r{later.number}"),
            )
            content.extend([f"{view}:", Image(data=data, format="jpeg")])
        return content

    @server.tool(structured_output=False)
    async def get_transcript(
        source: SourceArgument,
        project: ProjectArgument = None,
        last: Annotated[
            int,
            Field(description="How many of the newest chat items to return.", ge=1, le=MANAGER_TRANSCRIPT_ITEMS_MAX),
        ] = MANAGER_TRANSCRIPT_ITEMS,
    ) -> str:
        """Read the end of a source's worker chat in its current session, one line per item.

        Items are prompts, the worker's text, tool calls, approvals and how each turn ended. Long text is cut short.
        """

        def _work() -> str:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            snapshot = runtime.workers.snapshot(source_path)
            if not snapshot.items:
                return f"{source_path} has no chat in its current session"
            items = snapshot.items[-last:]
            header = (
                f"{source_path}, session {snapshot.info.session_id}: items {items[0].index} to {items[-1].index} "
                f"of {len(snapshot.items)}"
            )
            return "\n".join([header, *(_transcript_line(item) for item in items)])

        return await _run(_work)

    @server.tool(structured_output=False)
    async def list_feedback(
        source: SourceArgument,
        project: ProjectArgument = None,
        include_images: Annotated[
            bool,
            Field(description="Also return each feedback's marked-up image (the capture with its strokes and pins)."),
        ] = False,
        size: SizeArgument = MANAGER_IMAGE_EDGE,
    ) -> list[str | Image]:
        """List the reviewer's marked-up feedback on a source, newest first, with its numbered pins and notes.

        A feedback id can be sent to the worker with send_prompt.
        """

        def _work() -> list[str | Image]:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            content: list[str | Image] = []
            for feedback in runtime.feedback.list_feedback(source_path):
                summary = FeedbackSummary(
                    id=feedback.id,
                    created_at=feedback.created_at,
                    revision=feedback.revision_number,
                    export=feedback.export,
                    pins=feedback.pins,
                    camera=feedback.camera,
                )
                content.append(summary.model_dump_json(exclude_none=True))
                if include_images and feedback.annotated_path.is_file():
                    content.append(Image(data=scaled_jpeg(feedback.annotated_path, size), format="jpeg"))
            return content or [f"{source_path} has no feedback"]

        return await _run(_work)

    # ####################### #
    #       REFERENCES        #
    # ####################### #

    @server.tool(structured_output=False)
    async def list_references(
        project: ProjectArgument = None,
        query: Annotated[
            str | None,
            Field(description="Only references whose path or tag holds every word of this, ignoring case."),
        ] = None,
    ) -> str:
        """List the project's reference images (concept art, palettes, style targets), each tagged by its folder."""

        def _work() -> list[Reference]:
            runtime = _open_runtime(core, project)
            words = query.lower().split() if query else []
            return [
                reference
                for reference in runtime.catalog.list_references()
                if all(word in f"{reference.path} {reference.tag}".lower() for word in words)
            ]

        return await _run_as_json(_work)

    @server.tool(structured_output=False)
    async def pin_references(
        source: SourceArgument,
        references: Annotated[
            list[str],
            Field(description="Reference paths, as list_references gives them. An empty list unpins all."),
        ],
        project: ProjectArgument = None,
    ) -> str:
        """Pin references to a source's next prompt, replacing those pinned, and return what is pinned now.

        The worker gets their paths with that prompt, and they are unpinned once it has.
        """

        def _work() -> list[str]:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            known = {reference.path for reference in runtime.catalog.list_references()}
            unknown = sorted(set(references) - known)
            if unknown:
                raise ValueError(f"Not references of {runtime.project.name}: {', '.join(unknown)}")
            return list(runtime.workers.get(source_path).pin_references(references))

        return await _run_as_json(_work)

    # ####################### #
    #         WORKERS         #
    # ####################### #

    @server.tool(structured_output=False)
    async def send_prompt(
        source: SourceArgument,
        prompt: Annotated[str, Field(description="What the worker should do: a concrete critique or instruction.")],
        project: ProjectArgument = None,
        feedback_id: Annotated[
            int | None,
            Field(description="A feedback of this source (list_feedback) to send with the prompt."),
        ] = None,
        new_session: Annotated[
            bool,
            Field(description="Start a fresh worker session instead of continuing the current one."),
        ] = False,
    ) -> str:
        """Start a worker turn on a source with a prompt, continuing its session or starting one.

        Returns at once while the turn runs; call get_worker with wait_seconds to wait for it. Refused while a turn is
        running.
        """

        def _work() -> WorkerState:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            if new_session:
                runtime.workers.get(source_path).new_session()
            runtime.workers.send_prompt(source_path, prompt, feedback_id)
            return _worker_state(
                core,
                runtime,
                source_path,
                note="The turn is running. Wait for it with get_worker(wait_seconds=...).",
            )

        return await _run_as_json(_work)

    @server.tool(structured_output=False)
    async def get_worker(
        source: SourceArgument,
        project: ProjectArgument = None,
        wait_seconds: Annotated[
            int,
            Field(
                description="Wait up to this long for a running turn to end. The wait also ends early when the turn "
                "is waiting on the reviewer's approval.",
                ge=0,
                le=MANAGER_WAIT_MAX_S,
            ),
        ] = 0,
    ) -> str:
        """Get a source's worker: its state, the approvals it waits on, and how its last turn ended.

        Also its session, Blender port, pinned references and the revision its last turn recorded. Approvals are read
        only: the reviewer answers them in the Atelier window.
        """
        runtime, source_path = await _run(partial(_resolve, core, project, source))
        read_state = partial(_worker_state, core, runtime, source_path)
        deadline = time.monotonic() + wait_seconds
        state = await _run(read_state)
        while state.info.is_turn_running and not state.pending_approvals and time.monotonic() < deadline:
            await asyncio.sleep(_WAIT_POLL_S)
            state = await _run(read_state)
        if state.pending_approvals:
            state = state.model_copy(
                update={"note": "The turn is waiting on the reviewer's approval in the Atelier window; tell the user."}
            )
        elif state.info.is_turn_running:
            state = state.model_copy(update={"note": "The turn is still running."})
        return to_json(state, exclude_none=True).decode()

    @server.tool(structured_output=False)
    async def list_workers(project: ProjectArgument = None) -> str:
        """List a project's workers that have run since the daemon started, and the approvals waiting on the reviewer.

        Approvals are read only: the reviewer answers them in the Atelier window.
        """

        def _work() -> ProjectWorkers:
            runtime = _open_runtime(core, project)
            pending = core.approvals.list_pending(runtime.project.name)
            return ProjectWorkers(
                workers=tuple(runtime.workers.list_infos()),
                pending_approvals=tuple(_pending_approval(approval) for approval in pending),
            )

        return await _run_as_json(_work)

    @server.tool(structured_output=False)
    async def interrupt_worker(source: SourceArgument, project: ProjectArgument = None) -> str:
        """Interrupt the turn running on a source.

        The turn ends as interrupted and records a revision of whatever it had written; the session continues with
        the next prompt.
        """

        def _work() -> WorkerState:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            runtime.workers.get(source_path).interrupt()
            return _worker_state(core, runtime, source_path)

        return await _run_as_json(_work)

    # ####################### #
    #        REVISIONS        #
    # ####################### #

    @server.tool(structured_output=False)
    async def restore_revision(
        source: SourceArgument,
        revision: Annotated[int, Field(description="The revision number to restore the source to.", ge=1)],
        project: ProjectArgument = None,
    ) -> str:
        """Restore a source's files (blend, builder, README, exports, renders) to an earlier revision.

        The state before is recorded first, so restoring that revision undoes it. Files shared with other sources are
        left alone. Refused while a worker turn runs on the source.
        """

        def _work() -> RestoreSummary:
            runtime = _open_runtime(core, project)
            source_path = _resolve_source(runtime, source)
            target = _find_revision(runtime.revisions.history(source_path), revision)
            result = runtime.revisions.restore(source_path, target.id)
            return RestoreSummary(
                restored_to=result.target.number,
                pre_restore=result.pre_restore.number,
                recorded=result.restored.number,
                written=result.written,
                deleted=result.deleted,
                skipped=result.skipped,
            )

        return await _run_as_json(_work)

    return server


# ####################### #
#        RESOLVING        #
# ####################### #


@contextlib.contextmanager
def _refusals() -> Iterator[None]:
    """Turn the core's deliberate failures into tool errors whose message the manager reads and acts on."""
    try:
        yield
    except _EXPECTED_ERRORS as error:
        # KeyError's str() is its repr; its argument is the message.
        message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        logger.info(f"[Manager MCP] Refused: {type(error).__name__}: {message}")
        raise ToolError(message) from error


async def _run[ResultType](work: Callable[[], ResultType]) -> ResultType:
    """Run blocking core work in a thread, off the daemon's event loop."""
    with _refusals():
        return await asyncio.to_thread(work)


async def _run_as_json(work: Callable[[], object]) -> str:
    """Run blocking core work in a thread and return its result as compact JSON, which costs a model the least."""
    result = await _run(work)
    return to_json(result, exclude_none=True).decode()


def _open_runtime(core: AtelierCore, reference: str | None) -> ProjectRuntime:
    """Return the project a tool names, opening it on first use, or the only one there is when it names none.

    Raises:
        ValueError: no project was named and there is not exactly one to choose.
    """
    if reference:
        return core.open_project(reference)
    open_runtimes = core.open_projects()
    if len(open_runtimes) == 1:
        return open_runtimes[0]
    known = core.list_projects()
    if not open_runtimes and len(known) == 1:
        return core.open_project(known[0])
    if not known:
        raise ValueError("Atelier knows no project yet; pass the project's folder path as project")
    raise ValueError(f"Name the project; this machine knows {', '.join(known)}")


def _resolve_source(runtime: ProjectRuntime, query: str) -> str:
    """Return the path of the source a tool names: by exact path, by its file name, or by a unique part of its path.

    Raises:
        KeyError: nothing matches.
        ValueError: no source was named, or the name matches several.
    """
    if not query.strip():
        raise ValueError("Name a source (list_sources lists them)")
    paths = [source.path for source in runtime.catalog.list_sources()]
    if query in paths:
        return query
    needle = query.strip().lower().removesuffix(".blend")
    by_name = [path for path in paths if PurePosixPath(path).stem.lower() == needle]
    matches = by_name or [path for path in paths if needle in path.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"No source of {runtime.project.name} matches '{query}' (list_sources lists them)")
    raise ValueError(f"'{query}' matches several sources; name one by path: {', '.join(matches[:10])}")


def _resolve(core: AtelierCore, project: str | None, source: str) -> tuple[ProjectRuntime, str]:
    runtime = _open_runtime(core, project)
    return runtime, _resolve_source(runtime, source)


def _find_revision(history: SourceHistory, number: int | None) -> Revision:
    """Return a source's revision by number, or its newest.

    Raises:
        KeyError: the source has no such revision (or none at all yet).
    """
    if not history.revisions:
        raise KeyError(f"{history.source} has no revisions yet; Atelier records its first in the background")
    if number is None:
        return history.revisions[0]
    revision = next((item for item in history.revisions if item.number == number), None)
    if revision is None:
        raise KeyError(f"{history.source} has no r{number}; its revisions run r1 to r{history.revisions[0].number}")
    return revision


def _previous_revision(history: SourceHistory, revision: Revision) -> Revision:
    """Return the revision before `revision`.

    Raises:
        ValueError: it is the source's first.
    """
    earlier = [item for item in history.revisions if item.number < revision.number]
    if not earlier:
        raise ValueError(f"r{revision.number} is the first revision of {history.source}; there is nothing before it")
    return earlier[0]


def _choose_export(revision: Revision, query: str | None) -> tuple[str, str]:
    """Return the export of a revision a tool shows, and a note naming the others when it chose by default.

    Raises:
        KeyError: the revision has no exports, or none matching `query`.
    """
    exports = [file.path for file in revision.exports]
    if not exports:
        raise KeyError(f"r{revision.number} of {revision.source} has no exports, so it has no standard renders")
    if query is None:
        others = exports[1:]
        note = f" (the others: {', '.join(others)})" if others else ""
        return exports[0], note
    needle = query.strip().lower()
    for path in exports:
        name = PurePosixPath(path)
        if needle in (path.lower(), name.name.lower(), name.stem.lower()):
            return path, ""
    raise KeyError(f"r{revision.number} has no export '{query}'; it has {', '.join(exports)}")


# ####################### #
#         RENDERS         #
# ####################### #


async def _wait_for_renders(
    runtime: ProjectRuntime,
    source_path: str,
    wanted: Sequence[tuple[Revision, str]],
    wait_seconds: int,
) -> SourceHistory:
    """Ask for any of the wanted render sets not yet made, then wait while any is pending, up to `wait_seconds`.

    Each missing set is asked for once: a machine with no Blender leaves a set missing after trying, and asking
    again would only try again.
    """

    def _request_missing() -> SourceHistory:
        history = runtime.revisions.history(source_path)
        for revision, export_path in wanted:
            if _render_set(history, revision, export_path).state is RenderState.MISSING:
                runtime.revisions.request_standard_renders(revision.id, export_path)
        return runtime.revisions.history(source_path)

    deadline = time.monotonic() + wait_seconds
    history = await _run(_request_missing)
    while time.monotonic() < deadline and any(
        _render_set(history, revision, export_path).state is RenderState.PENDING for revision, export_path in wanted
    ):
        await asyncio.sleep(_WAIT_POLL_S)
        history = await _run(partial(runtime.revisions.history, source_path))
    return history


def _render_set(history: SourceHistory, revision: Revision, export_path: str) -> StandardRenderSet:
    """Return one export's render set in a revision; one the revision lacks (an export it did not have) is missing."""
    found = next(
        (
            render_set
            for render_set in history.standard_renders
            if render_set.revision_id == revision.id and render_set.export == export_path
        ),
        None,
    )
    if found is None:
        return StandardRenderSet(
            revision_id=revision.id, export=export_path, export_sha256="", state=RenderState.MISSING
        )
    return found


def _render_state_note(render_set: StandardRenderSet) -> str:
    """Say why a render set has no images."""
    match render_set.state:
        case RenderState.PENDING:
            return "Its renders are still being made; ask again shortly."
        case RenderState.FAILED:
            return f"Blender could not render it: {render_set.error}"
        case _:
            return "It has no standard renders (this revision lacks the export, or Blender is not available)."


def _describe(revision: Revision, history: SourceHistory) -> str:
    """Name a revision the way the window's filmstrip does."""
    text = f"r{revision.number} ({revision.trigger.value}, {revision.created_at}"
    restored_from = next((item for item in history.revisions if item.id == revision.restored_from), None)
    if restored_from:
        text += f", restored from r{restored_from.number}"
    return text + ")"


def _revision_summaries(history: SourceHistory) -> tuple[RevisionSummary, ...]:
    number_by_id = {revision.id: revision.number for revision in history.revisions}
    states_by_revision: dict[int, dict[str, RenderState]] = {}
    for render_set in history.standard_renders:
        states_by_revision.setdefault(render_set.revision_id, {})[render_set.export] = render_set.state
    return tuple(
        RevisionSummary(
            number=revision.number,
            trigger=revision.trigger,
            created_at=revision.created_at,
            restored_from=number_by_id.get(revision.restored_from) if revision.restored_from is not None else None,
            renders=states_by_revision.get(revision.id, {}),
        )
        for revision in history.revisions
    )


# ####################### #
#         WORKERS         #
# ####################### #


def _worker_state(core: AtelierCore, runtime: ProjectRuntime, source_path: str, note: str = "") -> WorkerState:
    snapshot = runtime.workers.snapshot(source_path)
    pending = [
        _pending_approval(approval)
        for approval in core.approvals.list_pending(runtime.project.name)
        if approval.source == source_path
    ]
    last_result = next((item for item in reversed(snapshot.items) if item.kind is ChatItemKind.RESULT), None)
    return WorkerState(
        info=snapshot.info,
        pending_approvals=tuple(pending),
        last_result=last_result.text if last_result else None,
        last_revision=last_result.revision_number if last_result else None,
        note=note,
    )


def _pending_approval(approval: Approval) -> PendingApproval:
    return PendingApproval(
        source=approval.source,
        tool_name=approval.tool_name,
        tool_summary=approval.tool_summary,
        reason=approval.reason,
        created_at=approval.created_at,
    )


def _transcript_line(item: ChatItem) -> str:
    """One chat item as one line of a transcript read."""
    prefix = f"[{item.index}] {item.created_at[11:19]} {item.kind.value}"
    match item.kind:
        case ChatItemKind.TOOL:
            outcome = " (error)" if item.is_error else ("" if item.is_done else " (running)")
            text = f"{item.tool_name}: {_clip(item.tool_summary, _TRANSCRIPT_TOOL_LIMIT)}{outcome}"
        case ChatItemKind.APPROVAL:
            status = item.approval_status.value if item.approval_status else "pending"
            text = f"{item.tool_name}: {_clip(item.tool_summary, _TRANSCRIPT_TOOL_LIMIT)} ({status}; {item.text})"
        case ChatItemKind.USER if item.feedback_id is not None:
            text = f"{_clip(item.text, _TRANSCRIPT_TEXT_LIMIT)} (with feedback {item.feedback_id})"
        case _:
            text = _clip(item.text, _TRANSCRIPT_TEXT_LIMIT)
    return f"{prefix}: {text}"


def _clip(text: str, limit: int) -> str:
    """Fold text onto one line and cut it at `limit` characters."""
    folded = " ".join(text.split())
    if len(folded) <= limit:
        return folded
    return folded[: limit - 1] + "…"
