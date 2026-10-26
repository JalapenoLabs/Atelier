"""Shared fixtures: a disposable copy of the fixture project for every test."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURE_PROJECT = Path(__file__).parent / "fixtures" / "project"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Copy the fixture project into a temporary folder, so tests can scan and change it freely.

    `__pycache__` is created here rather than committed, because the repository ignores those folders.
    """
    root = tmp_path / "FixtureProject"
    shutil.copytree(FIXTURE_PROJECT, root)
    cache_folder = root / "SourceArt" / "Weapons" / "Carbine" / "__pycache__"
    cache_folder.mkdir()
    (cache_folder / "Cached.blend").touch()
    return root
