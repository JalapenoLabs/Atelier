"""A project's workers: one per source that has been prompted, created on first use and kept until the daemon stops.

The pool is what the front doors call: it finds (or makes) a source's worker, sends prompts with their feedback, and
answers the worker-side MCP's question of which worker a call comes from. Everything else happens on the worker
itself (`pool.get(source).interrupt()`, `.new_session()`, `.stop()`, `.pin_references(...)`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from atelierd.catalog.store import CatalogStore
from atelierd.project import Project
from atelierd.revisions import RevisionTracker
from atelierd.workers.approvals import ApprovalBroker
from atelierd.workers.feedback import FeedbackService
from atelierd.workers.models import ChatItem, WorkerEvent, WorkerInfo, WorkerSnapshot
from atelierd.workers.process import BlenderLauncher
from atelierd.workers.worker import Worker, WorkerSettings

logger = logging.getLogger(__name__)


class WorkerPool:
    """Every worker of one project."""

    def __init__(
        self,
        project: Project,
        tracker: RevisionTracker,
        feedback: FeedbackService,
        broker: ApprovalBroker,
        settings: WorkerSettings,
        launch_blender: BlenderLauncher,
        emit: Callable[[WorkerEvent], None],
    ) -> None:
        self.project = project
        self._tracker = tracker
        self._feedback = feedback
        self._broker = broker
        self._settings = settings
        self._launch_blender = launch_blender
        self._emit = emit
        self._guard = threading.Lock()
        self._workers: dict[str, Worker] = {}

    def get(self, source_path: str) -> Worker:
        """Return a source's worker, creating it (with no processes yet) on first use.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        with self._guard:
            worker = self._workers.get(source_path)
            if worker:
                return worker
            with CatalogStore(self.project.database_path) as store:
                if store.get_source(source_path) is None:
                    raise KeyError(f"No present source '{source_path}'")
            worker = Worker(
                self.project,
                source_path,
                self._tracker,
                self._broker,
                self._settings,
                self._launch_blender,
                self._emit,
            )
            self._workers[source_path] = worker
            return worker

    def snapshot(self, source_path: str) -> WorkerSnapshot:
        """Return a source's worker state and current chat.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        return self.get(source_path).snapshot()

    def send_prompt(self, source_path: str, prompt: str, feedback_id: int | None = None) -> ChatItem:
        """Start a turn on a source, optionally carrying a feedback, and return the prompt's chat item.

        Raises:
            KeyError: the source or the feedback does not exist.
            ValueError: the feedback belongs to another source, or there is nothing to send.
            WorkerBusyError: a turn is already running on the source.
        """
        feedback = self._feedback.get(feedback_id) if feedback_id is not None else None
        if feedback and feedback.source != source_path:
            raise ValueError(f"Feedback {feedback.id} is on {feedback.source}, not {source_path}")
        return self.get(source_path).send_prompt(prompt, feedback)

    def find_by_token(self, token: str) -> Worker | None:
        """Return the worker a worker-side MCP call comes from, by the token it sends, or None for a stranger."""
        with self._guard:
            return next((worker for worker in self._workers.values() if worker.token == token), None)

    def is_turn_running(self, source_path: str) -> bool:
        """Whether a turn is running on a source: what restore checks before writing it."""
        with self._guard:
            worker = self._workers.get(source_path)
        return bool(worker and worker.is_turn_running)

    def list_infos(self) -> list[WorkerInfo]:
        """Return the state of every worker created since the daemon started, by source."""
        with self._guard:
            workers = sorted(self._workers.values(), key=lambda worker: worker.source_path)
        return [worker.info() for worker in workers]

    def stop_all(self) -> None:
        """Stop every worker's processes, for the daemon shutting down."""
        with self._guard:
            workers = list(self._workers.values())
        for worker in workers:
            try:
                worker.stop()
            except OSError as error:
                logger.error(f"[Workers] Could not stop the worker on {worker.source_path}: {error}")
