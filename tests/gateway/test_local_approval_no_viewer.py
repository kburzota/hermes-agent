"""A local (Bot Chat) turn with no viewer attached denies an approval at once instead of parking it."""
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner, _ExecApprovalDeclined


def _turn(subscribers):
    t = object.__new__(TurnRunner)
    t._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL), session_key="k")
    t._approval_owner = (SimpleNamespace(sessions={"s": SimpleNamespace(subscribers=subscribers)}), "s", 1)
    return t


def test_no_viewer_denies_instead_of_waiting():
    with pytest.raises(_ExecApprovalDeclined):
        _turn({})._approval_notify_sync({"command": "echo x"})


def test_viewer_attached_still_renders(monkeypatch):
    t = _turn({"sub": object()})
    rendered = []
    monkeypatch.setattr(t, "_render_approval_sync", rendered.append)
    for name in ("check_approval_generation", "register_approval"):
        monkeypatch.setattr(t._approval_owner[0], name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr("gateway.local_viewer_events.prompt_timeout_s", lambda *a: 1)
    t._approval_notify_sync({"command": "echo x"})
    assert rendered
