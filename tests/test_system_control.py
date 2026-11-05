"""The machine and the servers: real processes, in a temp state folder."""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["LEO_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from tools import dispatch, guard, system_control as _tool  # noqa: E402

control = sys.modules["tools.system_control"]

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_START_SETTLE_S", 0.15)


def _start(name="sleeper", command="sleep 30"):
    return dispatch("system_control", {"action": "start", "name": name, "command": command})


def _cleanup():
    for row in control.entries().values():
        control.stop_group(row, wait_s=0.5)


def test_status_says_the_numbers():
    result = dispatch("system_control", {"action": "status"})
    assert result.ok
    assert "CPU" in result.speech and "memory=" in result.detail


def test_reads_work_during_lockdown():
    """A locked-down assistant that cannot say what is running is just broken."""
    guard.engage_lockdown("test")
    assert dispatch("system_control", {"action": "status"}).ok
    assert dispatch("system_control", {"action": "ports"}).ok


def test_starting_is_refused_during_lockdown():
    guard.engage_lockdown("test")
    assert not _start().ok
    assert control.entries() == {}


def test_asking_for_status_repeatedly_is_not_a_loop():
    for _ in range(6):
        assert dispatch("system_control", {"action": "status"}).ok
    assert not guard.is_locked_down()


def test_a_process_name_never_reaches_speech():
    result = dispatch("system_control", {"action": "processes"})
    assert result.untrusted
    assert "pid" not in result.speech


def test_the_filter_finds_a_process():
    result = dispatch("system_control", {"action": "processes", "name": "python"})
    assert "python" in result.detail.lower()


def test_a_listening_port_is_seen_and_named():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        result = dispatch("system_control", {"action": "ports", "name": str(port)})
    assert "listening" in result.speech
    assert f"pid {os.getpid()}" in result.detail


def test_a_free_port_is_said_to_be_free():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert "Nothing" in dispatch("system_control", {"action": "ports", "name": str(port)}).speech


def test_start_list_logs_stop_round_trip():
    started = _start("printer", "echo hello-from-server; sleep 30")
    assert started.ok, started.detail
    try:
        assert "1 of 1" in dispatch("system_control", {"action": "list"}).speech
        time.sleep(0.1)
        logs = dispatch("system_control", {"action": "logs", "name": "printer"})
        assert "hello-from-server" in logs.detail
        assert "hello-from-server" not in logs.speech
        assert logs.untrusted
        row = control.entries()["printer"]
        assert dispatch("system_control", {"action": "stop", "name": "printer"}).ok
        assert not control.is_alive(row)
        assert "printer" not in control.entries()
    finally:
        _cleanup()


def test_a_server_that_dies_at_once_is_reported_with_its_output():
    result = _start("broken", "echo it-broke; exit 3")
    assert not result.ok
    assert "it-broke" in result.detail and "Exit 3" in result.detail
    assert "broken" not in control.entries()


def test_the_same_name_twice_is_refused():
    assert _start("one").ok
    try:
        assert not _start("one").ok
    finally:
        _cleanup()


def test_only_what_was_started_here_can_be_stopped():
    result = dispatch("system_control", {"action": "stop", "name": "nginx"})
    assert not result.ok
    assert "start" in result.speech


def test_stop_reaches_the_whole_process_group():
    """A dev server is usually a shell with children, and they must go too."""
    assert _start("family", "sleep 31 & sleep 32 & wait").ok
    row = control.entries()["family"]
    children = [
        pid for mib, pid, name in control._read_processes()
        if name == "sleep" and os.getpgid(pid) == row["pid"]
    ]
    assert len(children) == 2
    dispatch("system_control", {"action": "stop", "name": "family"})
    time.sleep(0.2)
    for pid in children:
        assert not Path(f"/proc/{pid}/stat").exists() or "Z" in Path(
            f"/proc/{pid}/stat"
        ).read_text().rpartition(")")[2].split()[0]


def test_a_recycled_pid_is_not_mistaken_for_our_server():
    """After a reboot that number can belong to anything. It must not be killed."""
    row = {"pid": os.getpid(), "started_at": "not-its-start-time", "log": ""}
    assert control.is_alive(row) is False
    assert control.stop_group(row) is False


def test_the_registry_survives_a_restart():
    assert _start("kept").ok
    try:
        control._lock = control.threading.Lock()
        assert control.is_alive(control._load()["kept"])
    finally:
        _cleanup()


def test_a_blocked_command_never_starts():
    result = _start("wipe", "rm -rf /")
    assert not result.ok
    assert "wipe" not in control.entries()


def test_a_risky_command_asks_and_the_yes_runs_exactly_it(tmp_path):
    asked = _start("risky", f"rm -rf {tmp_path}/x; sleep 30")
    assert asked.needs_confirmation
    assert "Confirm?" in asked.speech
    assert control.entries() == {}
    done = dispatch("system_control", {**asked.data, "confirmed": True})
    try:
        assert done.ok, done.detail
        assert asked.data["command"] == control.entries()["risky"]["command"]
    finally:
        _cleanup()


def test_the_model_cannot_supply_the_yes():
    from tools import from_model

    args = from_model({"action": "start", "name": "r", "command": "rm -rf /tmp/x", "confirmed": True})
    assert "confirmed" not in args
    assert dispatch("system_control", args).needs_confirmation


def test_a_strange_name_cannot_escape_the_logs_folder():
    assert _start("../../evil", "sleep 30").ok
    try:
        row = next(iter(control.entries().values()))
        assert Path(row["log"]).parent == control._logs_dir()
    finally:
        _cleanup()


def test_shell_off_means_off(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SHELL", False)
    assert not _start().ok


def test_it_is_offered_only_when_asked_for():
    from tools.schemas import select_tools

    assert "system_control" in select_tools("spin up the dev server")
    assert "system_control" in select_tools("what is using so much cpu")
    assert "system_control" not in select_tools("open notepad")
