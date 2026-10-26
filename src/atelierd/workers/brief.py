"""The short brief every worker prompt carries ahead of the reviewer's words.

It tells the worker which source it is on and what that source's files are, where it may write freely, about its own
Blender, and whatever this turn adds: references pinned to it, feedback to address, and a restore since its last turn.
It is rebuilt for every turn from the catalog as it stands, so a worker resumed days later still starts from facts.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from atelierd.catalog.models import Source
from atelierd.revisions.models import RestoreMark
from atelierd.workers.models import Feedback


def build_brief(
    source: Source,
    pinned_references: list[str],
    restore_mark: RestoreMark | None,
    feedback: Feedback | None,
) -> str:
    """Return the brief for one turn on `source`."""
    folder = PurePosixPath(source.path).parent.as_posix()
    lines = [
        "[Atelier] You are the worker on one source of this project, reviewed in Atelier.",
        f"Source: {source.path}",
    ]
    if source.builder:
        lines.append(f"Builder: {source.builder}")
    if source.readme:
        lines.append(f"README: {source.readme} (read it before changing anything)")
    if source.exports:
        lines.append(f"Exports: {', '.join(source.exports)}")
    if source.renders:
        lines.append(f"Renders: {', '.join(source.renders)}")
    lines.extend(
        [
            f"Write freely under {folder}/ and to the files above. Other writes (shared scripts, other sources, "
            "Content/) and network access wait for the reviewer's approval, so make them only when the task needs "
            "them.",
            "Your own background Blender is the `blender` MCP server; it opened this .blend when it started, so "
            "reopen the file there after a rebuild changes it on disk.",
            "Atelier records a revision and renders standard views after every turn; you need not render them.",
        ]
    )

    if restore_mark:
        lines.append(
            f"Restored: the reviewer restored this source to r{restore_mark.revision_number} since your last turn. "
            "Its files on disk are that revision's; build on them, not on what you remember."
        )
    if pinned_references:
        lines.append(f"Pinned references for this turn (read them): {', '.join(pinned_references)}")
    if feedback:
        lines.extend(_feedback_lines(feedback))
    return "\n".join(lines)


def _feedback_lines(feedback: Feedback) -> list[str]:
    on_what = f"r{feedback.revision_number}" if feedback.revision_number else "the current files"
    if feedback.export:
        on_what += f", export {feedback.export}"
    lines = [
        f"Feedback {feedback.id} on {on_what}: the reviewer marked up {feedback.annotated_path} (the clean capture "
        f"is {feedback.capture_path}). Read the annotated image first; the numbered pins on it are:",
    ]
    lines.extend(f"{pin.number}: {pin.note or '(no note)'}" for pin in feedback.pins)
    if not feedback.pins:
        lines.append("(no pins; the strokes on the image say it)")
    if feedback.camera:
        camera = feedback.camera
        target_x, target_y, target_z = camera.target_cm
        lines.append(
            f"It is a viewport frame, camera orbiting ({target_x:.1f}, {target_y:.1f}, {target_z:.1f}) cm in Unreal's "
            f"frame at yaw {camera.yaw_degrees:.0f}, pitch {camera.pitch_degrees:.0f}, {camera.distance_cm:.0f} cm out."
        )
    return lines
