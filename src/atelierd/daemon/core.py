"""The daemon's core: every open project with its catalog, watcher, revisions, workers and feedback.

Both front doors are thin adapters over this module: the GUI's JSON-RPC (`atelierd.rpc`) and the manager's MCP
(`atelierd.mcp`). They call the same objects, so they can never disagree about what a source or a worker is doing:

- `AtelierCore.open_project(reference)` returns the project's `ProjectRuntime`, opening it on first use (a scan, the
  watcher, a revision catch-up). A project stays open until the daemon stops, so its workers outlive every window.
- On a `ProjectRuntime`: `catalog` (`Catalog`: sources, statuses, bookmarks, references), `revisions`
  (`RevisionTracker`: history, standard renders, restore), `workers` (`WorkerPool`: prompts, interrupts, sessions,
  pinned references, transcripts) and `feedback` (`FeedbackService`). Exports convert with
  `atelierd.exports.convert_export(runtime.project, path)`.
- `AtelierCore.approvals` (`ApprovalBroker`) holds every escalated tool call, across projects.

Everything that happens is reported to subscribers as `(project name, event)` on whichever thread it happened, the
way the window's backend reports it: catalog changes, revisions, worker output and approvals. Each subscriber hops the
event to its own thread (the RPC to its event loop).

Every method may block (scans, SQLite, Blender, hashing), so the asyncio front doors call them in a thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from atelierd.catalog import Catalog, CatalogEvent, CatalogWatcher, ScanResult
from atelierd.project import Project, open_project
from atelierd.registry import load_registry, register_project, resolve_project_root
from atelierd.revisions import RevisionEvent, Revisions, RevisionTracker
from atelierd.workers import (
    ApprovalBroker,
    ApprovalEvent,
    BlenderLauncher,
    FeedbackService,
    WorkerEvent,
    WorkerPool,
    WorkerSettings,
)
from atelierd.workers.models import Approval

logger = logging.getLogger(__name__)

# Everything the core reports as it happens.
type CoreEvent = CatalogEvent | RevisionEvent | WorkerEvent | ApprovalEvent
type CoreListener = Callable[[str, CoreEvent], None]


class ProjectRuntime:
    """One open project and everything the daemon runs for it."""

    def __init__(
        self,
        project: Project,
        broker: ApprovalBroker,
        settings: WorkerSettings,
        launch_blender: BlenderLauncher,
        emit: CoreListener,
    ) -> None:
        self.project = project
        self.catalog = Catalog(project)
        self.last_scan: ScanResult = self.catalog.scan()
        self.feedback = FeedbackService(project)
        name = project.name

        def emit_event(event: CoreEvent) -> None:
            emit(name, event)

        # Restore asks the pool whether a turn runs on the source; the pool needs the tracker, so bind it late.
        self.revisions = RevisionTracker(
            Revisions(project, is_source_busy=lambda source_path: self.workers.is_turn_running(source_path)),
            on_event=emit_event,
        )
        self.workers = WorkerPool(project, self.revisions, self.feedback, broker, settings, launch_blender, emit_event)
        self.revisions.start()

        def on_catalog_event(event: CatalogEvent) -> None:
            emit_event(event)
            self.revisions.handle_catalog_event(event)

        self._watch_stop = threading.Event()
        watcher = CatalogWatcher(self.catalog, on_event=on_catalog_event)
        self._watch_thread = threading.Thread(
            target=watcher.run,
            args=(self._watch_stop,),
            name=f"watch-{name}",
            daemon=True,
        )
        self._watch_thread.start()
        logger.info(f"[Core] Opened {name} at {project.root} (state in {project.state_dir})")

    def close(self) -> None:
        """Stop the workers, the watcher and the revision tracker."""
        self.workers.stop_all()
        self._watch_stop.set()
        self._watch_thread.join(timeout=5)
        if self._watch_thread.is_alive():
            logger.warning(f"[Core] The watcher of {self.project.name} did not stop within 5 s")
        self.revisions.stop()


class AtelierCore:
    """Every project the daemon has open, and the approvals across them."""

    def __init__(
        self,
        registry_path: Path,
        settings: WorkerSettings,
        launch_blender: BlenderLauncher,
        notify: Callable[[Approval], None],
        state_root: Path | None = None,
    ) -> None:
        """Create the core.

        Args:
            registry_path: the machine's project registry.
            settings: how workers are run.
            launch_blender: starts a worker's background Blender.
            notify: raises a desktop notification for a new approval.
            state_root: when set, each project's state lives in `<state_root>/<project name>/` instead of its
                `.atelier/` folder, which leaves the projects themselves untouched.
        """
        self.registry_path = registry_path
        self.state_root = state_root
        self._settings = settings
        self._launch_blender = launch_blender
        self._guard = threading.Lock()
        self._runtimes: dict[str, ProjectRuntime] = {}
        self._listeners: list[CoreListener] = []
        self._listeners_guard = threading.Lock()

        def on_approval(event: ApprovalEvent) -> None:
            self._emit(event.approval.project, event)

        self.approvals = ApprovalBroker(on_event=on_approval, notify=notify)

    # ####################### #
    #         EVENTS          #
    # ####################### #

    def subscribe(self, listener: CoreListener) -> Callable[[], None]:
        """Call `listener` with every event from now on, and return the function that stops it."""
        with self._listeners_guard:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._listeners_guard:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, project_name: str, event: CoreEvent) -> None:
        with self._listeners_guard:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(project_name, event)
            except Exception as error:
                # A listener that fails (a client gone mid-send) must not stop the others hearing it.
                logger.exception(f"[Core] A listener failed on a {type(event).__name__}: {error}")

    # ####################### #
    #        PROJECTS         #
    # ####################### #

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        return sorted(load_registry(self.registry_path).projects)

    def open_project(self, reference: str) -> ProjectRuntime:
        """Return a project by name or path, opening it on first use.

        Raises:
            RegistryError: no such project is known.
            NotADirectoryError: the path is not a folder.
            ValueError: another open project already has the same folder name.
        """
        project_root = resolve_project_root(reference, self.registry_path)
        with self._guard:
            runtime = self._runtimes.get(project_root.name)
            if runtime and runtime.project.root == project_root:
                return runtime
            if runtime:
                raise ValueError(f"{runtime.project.root} is open under the name {project_root.name} already")

            state_dir = self.state_root / project_root.name if self.state_root else None
            project = open_project(project_root, state_dir=state_dir)
            register_project(project.root, self.registry_path)
            runtime = ProjectRuntime(project, self.approvals, self._settings, self._launch_blender, self._emit)
            self._runtimes[project.name] = runtime
            return runtime

    def project(self, name: str) -> ProjectRuntime:
        """Return an open project by name.

        Raises:
            KeyError: no open project has that name; open it first.
        """
        with self._guard:
            runtime = self._runtimes.get(name)
        if runtime is None:
            raise KeyError(f"Project {name} is not open in the daemon; open it first")
        return runtime

    def open_projects(self) -> list[ProjectRuntime]:
        """Return every open project."""
        with self._guard:
            return list(self._runtimes.values())

    def shutdown(self) -> None:
        """Stop every project's workers, watcher and tracker."""
        with self._guard:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.close()
        logger.info(f"[Core] Shut down {len(runtimes)} projects")
