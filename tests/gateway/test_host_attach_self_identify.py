"""The live host gateway must not dial its own control socket to identify itself.

Production ``list_profiles`` / dashboard polls asked ``host_gateway()`` once per
profile. A cache miss then connected to this process's unix socket, held the
GIL, and often timed out — after which every profile retried. When the host
record names *this* PID, the identify payload is built in-process.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from gateway import host_attach
from gateway import host_rendezvous as hr


@pytest.fixture(autouse=True)
def _no_memo():
    host_attach.invalidate_host_gateway_cache()
    yield
    host_attach.invalidate_host_gateway_cache()


def _self_record(home: Path, profiles: tuple[str, ...] = ("default", "ops")) -> hr.HostRecord:
    return hr.HostRecord(
        role=hr.ROLE_GATEWAY,
        pid=os.getpid(),
        create_time=time.time(),
        host="",
        port=None,
        protocol_version=hr.HOST_PROTOCOL_VERSION,
        token_fingerprint="",
        profiles=profiles,
        updated_at="",
        home=str(home),
    )


def test_self_host_record_does_not_dial_the_control_socket(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "gateway_state.json").write_text(
        json.dumps({
            "pid": os.getpid(),
            "kind": "hermes-gateway",
            "hermes_home": str(home),
            "gateway_state": "running",
            "served_profiles": ["default", "ops"],
        }),
        encoding="utf-8",
    )
    record = _self_record(home)
    monkeypatch.setattr(hr, "read_record", lambda role, **kw: record)
    monkeypatch.setattr(hr, "record_names_live_owner", lambda rec: rec is record)

    def _forbidden(*_a, **_k):
        raise AssertionError("self-identify must not dial identify_gateway")

    monkeypatch.setattr("gateway.control_socket.identify_gateway", _forbidden)

    owner = host_attach.host_gateway()

    assert owner is not None
    assert owner.pid == os.getpid()
    assert owner.served_known is True
    assert owner.serves("ops")
    assert owner.serves("default")


def test_a_foreign_owner_still_uses_the_control_socket(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    record = hr.HostRecord(
        role=hr.ROLE_GATEWAY,
        pid=os.getpid() + 1000,
        create_time=time.time(),
        host="",
        port=None,
        protocol_version=hr.HOST_PROTOCOL_VERSION,
        token_fingerprint="",
        profiles=("default",),
        updated_at="",
        home=str(home),
    )
    monkeypatch.setattr(hr, "read_record", lambda role, **kw: record)
    monkeypatch.setattr(hr, "record_names_live_owner", lambda rec: rec is record)
    dials = []

    def _identify(dialled, **_k):
        dials.append(Path(dialled))
        return {"pid": record.pid, "hermes_home": str(home), "served_profiles": ["default"]}

    monkeypatch.setattr("gateway.control_socket.identify_gateway", _identify)

    owner = host_attach.host_gateway()

    assert dials == [home]
    assert owner is not None and owner.pid == record.pid
    assert owner.serves("default")


def test_concurrent_host_gateway_calls_share_one_probe(tmp_path, monkeypatch):
    """Dashboard polls must not each run _probe_host_gateway — that pile-up is
    the GIL storm the memo + lock exist to prevent."""
    import threading

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    probes = []
    owner = host_attach.HostGateway(os.getpid(), home, ("default",))
    started = threading.Event()
    release = threading.Event()

    def _probe(_wait):
        probes.append(1)
        started.set()
        release.wait()
        return owner

    monkeypatch.setattr(host_attach, "_probe_host_gateway", _probe)
    results = []

    def _call():
        results.append(host_attach.host_gateway())

    first = threading.Thread(target=_call)
    first.start()
    assert started.wait(2.0)
    second = threading.Thread(target=_call)
    second.start()
    release.set()
    first.join(2.0)
    second.join(2.0)

    assert probes == [1]
    assert results == [owner, owner]
