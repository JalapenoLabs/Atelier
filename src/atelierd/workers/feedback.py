"""Feedback: markup on a render or a frozen viewport frame, stored for the worker to read.

The window draws the markup and flattens it onto the capture itself (it has the image and the strokes on screen); it
hands both PNGs here as files. Each feedback gets `<state>/feedback/<source>/<id>/`, where `<source>` is the source's
project-relative path without `.blend`:

- `capture.png`: the clean render or frame.
- `annotated.png`: the capture with the strokes and numbered pins drawn on it. The worker's prompt names this file.
- `markup.json`: the strokes and pins, so the markup can be read back exactly.

`state.db` keeps the row (the revision it critiques, the export, the pins, the camera of a frozen frame) for listing.
Feedback stays in the state folder, out of version control, because it is review state, not source.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path, PurePosixPath

from atelierd.constants import FEEDBACK_DIR_NAME
from atelierd.project import Project
from atelierd.workers.models import Feedback, FeedbackRequest
from atelierd.workers.store import StoredFeedback, WorkerStore

logger = logging.getLogger(__name__)

CAPTURE_FILE_NAME = "capture.png"
ANNOTATED_FILE_NAME = "annotated.png"
MARKUP_FILE_NAME = "markup.json"


class FeedbackService:
    """One project's feedback."""

    def __init__(self, project: Project) -> None:
        self.project = project

    def create(self, request: FeedbackRequest) -> Feedback:
        """Store a marked-up capture as feedback on its source.

        Raises:
            KeyError: the catalog has no present source at `request.source`.
            FileNotFoundError: one of the request's images does not exist.
        """
        for image in (request.capture_path, request.annotated_path):
            if not image.is_file():
                raise FileNotFoundError(f"Feedback image {image} does not exist")

        with WorkerStore(self.project.database_path) as store:
            stored = store.insert_feedback(
                request.source,
                request.markup.pins,
                request.revision_id,
                request.export,
                request.camera,
            )
        feedback = self._with_paths(stored)
        feedback.folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(request.capture_path, feedback.capture_path)
        shutil.copyfile(request.annotated_path, feedback.annotated_path)
        (feedback.folder / MARKUP_FILE_NAME).write_text(request.markup.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"[Feedback] {self.project.name}: feedback {feedback.id} on {request.source} at {feedback.folder}")
        return feedback

    def get(self, feedback_id: int) -> Feedback:
        """Return one feedback.

        Raises:
            KeyError: there is no such feedback.
        """
        with WorkerStore(self.project.database_path) as store:
            return self._with_paths(store.get_feedback(feedback_id))

    def list_feedback(self, source_path: str) -> list[Feedback]:
        """Return a source's feedback, newest first, with its pins."""
        with WorkerStore(self.project.database_path) as store:
            return [self._with_paths(stored) for stored in store.list_feedback(source_path)]

    def _with_paths(self, stored: StoredFeedback) -> Feedback:
        source_key = PurePosixPath(stored.source).with_suffix("")
        folder: Path = self.project.state_dir / FEEDBACK_DIR_NAME / source_key / str(stored.id)
        return Feedback(
            **stored.model_dump(),
            folder=folder,
            capture_path=folder / CAPTURE_FILE_NAME,
            annotated_path=folder / ANNOTATED_FILE_NAME,
        )
