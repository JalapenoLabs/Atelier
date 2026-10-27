"""The `atelier` command line.

`atelier` on its own opens the desktop app, a client of the daemon (`atelierd`) on `--socket` (default
`$XDG_RUNTIME_DIR/atelier.sock`). `--project` opens a project at start; `--state-root` is the state root a daemon the
window starts keeps every project's state in, outside the projects.

`atelier scan <project>` scans a project into its catalog and lists every source with its exports and renders.
`<project>` is a path to the project's folder or the name of a project scanned before. With `--watch` it then keeps
the catalog live, records a revision of each source whose files change (with standard renders of changed exports),
and prints each settled change until interrupted.

`atelier prune <project> --keep N` deletes all but each source's newest N revisions, and the snapshotted bytes and
standard renders only they needed.

`--state-dir` and `--config` let a scan run against a project without writing anything into it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path, PurePosixPath

from atelierd.catalog import Catalog, CatalogEvent, CatalogWatcher, ScanResult, Source
from atelierd.config import ConfigError
from atelierd.constants import DEFAULT_PRUNE_KEEP
from atelierd.project import open_project
from atelierd.registry import RegistryError, default_registry_path, register_project, resolve_project_root
from atelierd.revisions import RevisionEvent, RevisionEventKind, Revisions, RevisionTracker

logger = logging.getLogger(__name__)

_LOG_LEVEL_BY_VERBOSITY = {
    0: logging.WARNING,
    1: logging.INFO,
    2: logging.DEBUG,
}


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    log_level = _LOG_LEVEL_BY_VERBOSITY[min(arguments.verbose, 2)]
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if arguments.command is None:
        return _run_gui(arguments)

    command_by_name = {"scan": _scan, "prune": _prune}
    try:
        return command_by_name[arguments.command](arguments)
    except (ConfigError, RegistryError, NotADirectoryError, ValueError) as error:
        print(f"atelier: {error}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atelier",
        description="A native review studio for AI-built 3D art. Without a command, opens the desktop app.",
    )
    parser.add_argument("--project", help="Open this project at start: its folder, or the name of a known project.")
    parser.add_argument(
        "--socket",
        type=Path,
        help="The daemon's socket (default $XDG_RUNTIME_DIR/atelier.sock, or ATELIER_SOCKET).",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        help="When the window starts a daemon, have it keep each project's state in <state-root>/<project name>/ "
        "instead of <project>/.atelier/, so nothing is written into the projects.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Log more (-v info, -vv debug).")
    _add_screenshot_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser("scan", help="Scan a project and list its sources.")
    scan_parser.add_argument("project", help="The project's folder, or the name of a project scanned before.")
    scan_parser.add_argument(
        "--config",
        type=Path,
        help="Read this file instead of <project>/atelier.toml.",
    )
    scan_parser.add_argument(
        "--state-dir",
        type=Path,
        help="Keep state here instead of <project>/.atelier/. Outside the project, nothing is written into it.",
    )
    scan_parser.add_argument("--watch", action="store_true", help="Keep the catalog live and print each change.")
    scan_parser.add_argument("-v", "--verbose", action="count", default=0, help="Log more (-v info, -vv debug).")

    prune_parser = subparsers.add_parser("prune", help="Delete all but each source's newest revisions.")
    prune_parser.add_argument("project", help="The project's folder, or the name of a project scanned before.")
    prune_parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_PRUNE_KEEP,
        help=f"Revisions to keep per source (default {DEFAULT_PRUNE_KEEP}).",
    )
    prune_parser.add_argument("--state-dir", type=Path, help="The state folder, when not <project>/.atelier/.")
    prune_parser.add_argument("-v", "--verbose", action="count", default=0, help="Log more (-v info, -vv debug).")
    return parser


def _add_screenshot_arguments(parser: argparse.ArgumentParser) -> None:
    """Development flags, left out of `--help`: render the window once to a PNG without showing it."""
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--screenshot-shown", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", help=argparse.SUPPRESS)
    parser.add_argument("--export", help=argparse.SUPPRESS)
    parser.add_argument("--render", help=argparse.SUPPRESS)
    parser.add_argument("--lighting", choices=("studio", "clay"), help=argparse.SUPPRESS)
    parser.add_argument("--wireframe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--collision", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--direct", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tab", choices=("chat", "refs", "readme"), help=argparse.SUPPRESS)
    parser.add_argument("--frame", choices=("model", "figure"), help=argparse.SUPPRESS)
    # The numpad views (`AxisView` in gui/camera.py, spelled out here so the command line never loads Qt).
    parser.add_argument("--view", choices=("front", "back", "right", "left", "top", "bottom"), help=argparse.SUPPRESS)
    parser.add_argument("--window-size", default="1600x1000", help=argparse.SUPPRESS)
    parser.add_argument("--revision", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--compare", choices=("renders", "models"), help=argparse.SUPPRESS)
    parser.add_argument("--compare-base", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--compare-view", help=argparse.SUPPRESS)
    parser.add_argument("--markup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--markup-file", type=Path, help=argparse.SUPPRESS)


def _run_gui(arguments: argparse.Namespace) -> int:
    # Imported here so that `atelier scan` never loads Qt.
    from atelier.gui.app import GuiOptions, run_gui

    # Only flags actually given, so they never override the settings the window remembers.
    view_options: dict[str, object] = {
        name: value
        for name, value in (
            ("lighting", arguments.lighting),
            ("wireframe", arguments.wireframe),
            ("collision", arguments.collision),
            ("direct", arguments.direct),
            ("tab", arguments.tab),
            ("frame", arguments.frame),
            ("view", arguments.view),
        )
        if value
    }

    width, _, height = arguments.window_size.partition("x")
    options = GuiOptions(
        project=arguments.project,
        socket_path=arguments.socket,
        state_root=arguments.state_root.resolve() if arguments.state_root else None,
        screenshot=arguments.screenshot,
        is_screenshot_shown=arguments.screenshot_shown,
        source=arguments.source,
        export=arguments.export,
        render=arguments.render,
        revision=arguments.revision,
        compare=arguments.compare,
        compare_base=arguments.compare_base,
        compare_view=arguments.compare_view,
        is_markup=arguments.markup,
        markup_file=arguments.markup_file,
        view_options=view_options,
        window_size=(int(width), int(height)),
    )
    return run_gui(options)


def _scan(arguments: argparse.Namespace) -> int:
    registry_path = default_registry_path()
    project_root = resolve_project_root(arguments.project, registry_path)
    project = open_project(project_root, config_path=arguments.config, state_dir=arguments.state_dir)
    register_project(project.root, registry_path)

    catalog = Catalog(project)
    result = catalog.scan()
    _print_result(result, catalog)

    if not arguments.watch:
        return 0

    print(f"\nWatching {project.name} for changes (Ctrl+C to stop)...", flush=True)
    tracker = RevisionTracker(Revisions(project), on_event=_print_revision_event)
    tracker.start()

    def on_catalog_event(event: CatalogEvent) -> None:
        _print_event(event)
        tracker.handle_catalog_event(event)

    stop_event = threading.Event()
    watcher = CatalogWatcher(catalog, on_event=on_catalog_event)
    try:
        watcher.run(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        tracker.stop()
    return 0


def _prune(arguments: argparse.Namespace) -> int:
    project_root = resolve_project_root(arguments.project, default_registry_path())
    project = open_project(project_root, state_dir=arguments.state_dir)
    if not project.database_path.is_file():
        print(f"atelier: {project.name} has no state at {project.state_dir}; nothing to prune", file=sys.stderr)
        return 2

    result = Revisions(project).prune(arguments.keep)
    megabytes = result.bytes_freed / 1_000_000
    print(
        f"{project.name}: removed {result.revisions_removed} revisions, {result.objects_removed} objects "
        f"({megabytes:.1f} MB) and {result.render_folders_removed} render folders"
    )
    return 0


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------


def _print_result(result: ScanResult, catalog: Catalog) -> None:
    project = catalog.project
    for source in result.sources:
        _print_source(source)

    export_count = sum(len(source.exports) for source in result.sources)
    render_count = sum(len(source.renders) for source in result.sources)
    print(
        f"{project.name}: {len(result.sources)} sources, {export_count} exports, {render_count} renders, "
        f"{len(result.references)} references"
    )
    print(f"State: {project.database_path}")
    for override_path in result.unmatched_overrides:
        print(f"warning: atelier.toml overrides '{override_path}', which is not a source", file=sys.stderr)


def _print_source(source: Source) -> None:
    """Print one source, with its files shown relative to its folder where they sit inside it."""
    folder = PurePosixPath(source.path).parent
    print(f"{source.path}  [{source.status.value}]")
    if source.builder:
        print(f"  builder  {source.builder}")
    if source.readme:
        print(f"  readme   {source.readme}")

    for label, paths in (("exports", source.exports), ("renders", source.renders)):
        override_note = " (atelier.toml)" if source.has_output_override else ""
        print(f"  {label}  {len(paths)}{override_note}")
        for path in paths:
            posix_path = PurePosixPath(path)
            shown_path = posix_path.relative_to(folder) if posix_path.is_relative_to(folder) else posix_path
            print(f"    {shown_path}")
    print()


def _print_event(event: CatalogEvent) -> None:
    change = event.change
    for label, paths in (
        ("added", change.added),
        ("removed", change.removed),
        ("changed", change.changed),
        ("touched", event.touched),
    ):
        for path in paths:
            print(f"{label:>8}  {path}", flush=True)
    if change.references_changed:
        print("references changed", flush=True)


def _print_revision_event(event: RevisionEvent) -> None:
    label_by_kind = {
        RevisionEventKind.RECORDED: "revision",
        RevisionEventKind.RENDERED: "rendered",
    }
    print(f"{label_by_kind[event.kind]:>8}  {event.source}  (revision {event.revision_id})", flush=True)
