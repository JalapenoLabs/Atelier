"""The daemon's JSON-RPC socket: who can connect, how errors cross it, and which events a client hears."""

from __future__ import annotations

import json
import socket
import stat
import threading
from pathlib import Path

import pytest
from conftest import RunningDaemon

from atelierd.catalog import CatalogEvent
from atelierd.rpc import protocol
from atelierd.rpc.client import DaemonUnavailableError, RpcClient
from atelierd.rpc.server import SocketInUseError, _claim_socket_path


def test_the_socket_is_the_owners_alone(daemon: RunningDaemon) -> None:
    assert stat.S_IMODE(daemon.socket_path.stat().st_mode) == 0o600


def test_a_live_daemon_keeps_its_socket_and_a_dead_ones_is_replaced(daemon: RunningDaemon, tmp_path: Path) -> None:
    with pytest.raises(SocketInUseError):
        _claim_socket_path(daemon.socket_path)

    stale = tmp_path / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()
    _claim_socket_path(stale)
    assert not stale.exists()


def test_the_cores_own_errors_cross_as_themselves(daemon: RunningDaemon) -> None:
    client = daemon.client()
    with pytest.raises(KeyError, match="not open in the daemon"):
        client.call(protocol.LIST_SOURCES, protocol.ProjectParams(project="Nowhere"))


def test_malformed_and_unknown_requests_get_json_rpc_errors(daemon: RunningDaemon) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(daemon.socket_path))
        reader = connection.makefile("rb")
        connection.sendall(b"not json\n")
        assert json.loads(reader.readline())["error"]["code"] == protocol.PARSE_ERROR
        connection.sendall(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "nope"}).encode() + b"\n")
        assert json.loads(reader.readline()) == {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {"code": protocol.METHOD_NOT_FOUND, "message": "No method nope"},
        }
        bad_params = {"jsonrpc": "2.0", "id": 8, "method": "catalog.list_sources", "params": {"wrong": 1}}
        connection.sendall(json.dumps(bad_params).encode() + b"\n")
        assert json.loads(reader.readline())["error"]["code"] == protocol.INVALID_PARAMS


def test_a_subscriber_hears_its_projects_events_only(daemon: RunningDaemon, project_root: Path) -> None:
    heard: list[tuple[str, object]] = []
    arrived = threading.Event()

    def on_event(project: str, event: object) -> None:
        heard.append((project, event))
        if isinstance(event, CatalogEvent):
            arrived.set()

    client = daemon.client(on_event)
    opened = client.call(protocol.OPEN_PROJECT, protocol.OpenProjectParams(reference=str(project_root)))
    client.call(protocol.SUBSCRIBE, protocol.SubscribeParams(project="SomeOtherProject"))
    readme = project_root / "SourceArt/Weapons/Carbine/README.md"
    readme.write_text("changed")
    assert not arrived.wait(4)

    client.call(protocol.SUBSCRIBE, protocol.SubscribeParams(project=opened.name))
    readme.write_text("changed again")
    assert arrived.wait(15)
    assert {project for project, _ in heard} == {opened.name}


def test_a_client_hears_when_the_daemon_goes_away(daemon: RunningDaemon) -> None:
    gone = threading.Event()
    client = RpcClient(daemon.socket_path, on_event=lambda project, event: None, on_disconnect=gone.set)
    client.connect()
    # As the window does right after connecting; the daemon serves a client from its first request.
    client.call(protocol.DAEMON_INFO, protocol.NoParams())
    daemon.stop()
    assert gone.wait(10)
    with pytest.raises(DaemonUnavailableError):
        client.call(protocol.LIST_PROJECTS, protocol.NoParams())


def test_no_daemon_is_a_clear_error(tmp_path: Path) -> None:
    client = RpcClient(tmp_path / "none.sock", on_event=lambda project, event: None, on_disconnect=lambda: None)
    with pytest.raises(DaemonUnavailableError, match="No Atelier daemon"):
        client.connect()
