"""The `atelier` command line.

`atelier scan <project>` scans a project into its catalog and lists every source with its exports and renders.
`<project>` is a path to the project's folder or the name of a project scanned before. With `--watch` it then keeps
the catalog live and prints each settled change until interrupted.

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
from atelierd.project import open_project
from atelierd.registry import RegistryError, default_registry_path, register_project, resolve_project_root

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

    try:
        return _scan(arguments)
    except (ConfigError, RegistryError, NotADirectoryError) as error:
        print(f"atelier: {error}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atelier", description="A native review studio for AI-built 3D art.")
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    return parser


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
    stop_event = threading.Event()
    watcher = CatalogWatcher(catalog, on_event=_print_event)
    try:
        watcher.run(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
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
