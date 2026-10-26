"""Where the daemon lives on the machine: its socket, the systemd user unit, and starting it in the background."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from atelierd.daemon import cli, system


def test_the_socket_defaults_to_the_runtime_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ATELIER_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert system.default_socket_path() == tmp_path / "atelier.sock"
    monkeypatch.setenv("ATELIER_SOCKET", str(tmp_path / "elsewhere.sock"))
    assert system.default_socket_path() == tmp_path / "elsewhere.sock"


def test_install_writes_the_unit_without_enabling_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PATH", f"{Path(sys.prefix) / 'bin'}:/home/me/.local/bin:/usr/bin")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))

    assert cli.main(["install", "--state-root", str(tmp_path / "state")]) == 0

    unit = (tmp_path / "config/systemd/user/atelierd.service").read_text()
    assert f'ExecStart="{sys.executable}" "-m" "atelierd" "run" "--state-root" "{tmp_path / "state"}"' in unit
    # The installing shell's PATH, minus this virtual environment, so the service finds claude and blender-mcp.
    assert 'Environment="PATH=/home/me/.local/bin:/usr/bin"' in unit
    assert "WantedBy=default.target" in unit
    assert calls == [["systemctl", "--user", "daemon-reload"]]
    assert "systemctl --user enable --now atelierd.service" in capsys.readouterr().out


def test_without_the_unit_the_window_starts_a_detached_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    started: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: started.append((command, kwargs)))

    how = system.start_in_background(tmp_path / "a.sock", tmp_path / "state")

    [(command, options)] = started
    assert command == [sys.executable, "-m", "atelierd", "run", "--socket", str(tmp_path / "a.sock"),
                       "--state-root", str(tmp_path / "state")]  # fmt: skip
    assert options["start_new_session"] is True
    assert "atelierd.log" in how
