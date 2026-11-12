"""A producer (automation) local turn with no responder denies an approval at once; a viewer's own turn waits."""
import threading
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner, _ExecApprovalDeclined
from gateway.session_local import LocalSessionAdapter


def _turn(monkeypatch, subscribers, payload):
    live = SimpleNamespace(event_stream=SimpleNamespace(lock=threading.RLock()), subscribers=subscribers)
    authority = SimpleNamespace(sessions={"s": live}, db=None)
    adapter = SimpleNamespace(authority=authority)
    adapter.no_viewer_can_answer = lambda cid, cap='session:respond': LocalSessionAdapter.no_viewer_can_answer(adapter, cid, cap)
    monkeypatch.setattr("hermes_state_runtime.list_session_admissions",
                        lambda *a, **k: [{"status": "started", "payload": payload}])
    t = object.__new__(TurnRunner)
    t._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, profile="p"), session_key="k")
    t._runner = SimpleNamespace(_adapters_for_profile=lambda _p: {Platform.LOCAL: adapter})
    t._approval_owner = (authority, "s", 1)
    return t


def _stub_render(monkeypatch, t):
    rendered = []
    monkeypatch.setattr(t, "_render_approval_sync", rendered.append)
    for name in ("check_approval_generation", "register_approval"):
        setattr(t._approval_owner[0], name, lambda *a, **k: None)
    monkeypatch.setattr("gateway.local_viewer_events.prompt_timeout_s", lambda *a: 1)
    return rendered


def test_automation_turn_without_responder_denies(monkeypatch):
    with pytest.raises(_ExecApprovalDeclined):
        _turn(monkeypatch, {}, "local_automation_v1")._approval_notify_sync({"command": "echo x"})


def test_viewers_own_turn_without_subscribers_still_waits(monkeypatch):
    t = _turn(monkeypatch, {}, "{}")
    rendered = _stub_render(monkeypatch, t)
    t._approval_notify_sync({"command": "echo x"})
    assert rendered


def test_approver_attached_still_renders(monkeypatch):
    t = _turn(monkeypatch, {"sub": SimpleNamespace(capabilities={"session:approve"})}, "local_automation_v1")
    rendered = _stub_render(monkeypatch, t)
    t._approval_notify_sync({"command": "echo x"})
    assert rendered


def test_respond_only_subscriber_does_not_count_for_approvals(monkeypatch):
    t = _turn(monkeypatch, {"sub": SimpleNamespace(capabilities={"session:respond"})}, "local_automation_v1")
    with pytest.raises(_ExecApprovalDeclined):
        t._approval_notify_sync({"command": "echo x"})
