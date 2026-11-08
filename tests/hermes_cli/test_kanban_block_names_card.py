"""A block whose reason names another card on the board becomes a real
parent link (agent-kit stall fix 1 of 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _running(conn, title="waiter"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _last_block_payload(conn, tid):
    kinds = ("blocked", "dependency_wait", "block_loop_detected")
    return [e for e in kb.list_events(conn, tid) if e.kind in kinds][-1].payload


def test_needs_input_naming_open_card_becomes_dependency(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        gordon = kb.create_task(conn, title="gordon", assignee="gordon")
        tid = _running(conn)
        reason = f"⚪ GORDON: commit the fix in {gordon} then wake me"
        assert kb.block_task(conn, tid, link_named=True, reason=reason, kind="needs_input")
        t = kb.get_task(conn, tid)
        assert (t.status, t.block_kind) == ("todo", "dependency")
        assert kb.parent_ids(conn, tid) == [gordon]
        auto = [e for e in kb.list_events(conn, tid) if e.kind == "auto_linked"]
        assert auto and auto[-1].payload["parents"] == [gordon]
        assert _last_block_payload(conn, tid)["reason"] == reason
        assert kb.complete_task(conn, gordon, result="ok")
        assert kb.get_task(conn, tid).status == "ready"


def test_kindless_block_and_self_mention(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="x")
        b = kb.create_task(conn, title="b", assignee="x")
        tid = _running(conn)
        assert kb.block_task(conn, tid, link_named=True, reason=f"{tid} waits on {a} and {b}, {a}")
        assert kb.get_task(conn, tid).status == "todo"
        assert sorted(kb.parent_ids(conn, tid)) == sorted([a, b])


def test_named_card_already_done_does_not_block(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        done = kb.create_task(conn, title="done", assignee="x")
        assert kb.complete_task(conn, done, result="ok")
        tid = _running(conn)
        with pytest.raises(kb.BlockerCleared) as exc:
            kb.block_task(conn, tid, link_named=True, reason=f"waiting on {done}", kind="needs_input")
        assert done in str(exc.value)
        assert kb.get_task(conn, tid).status == "running"
        assert not kb.parent_ids(conn, tid)


def test_unknown_or_other_board_id_stays_needs_input(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        here = kb.create_task(conn, title="here", assignee="x")
        tid = _running(conn)
        # t_deadbeef lives on no board here (another board's card looks the same).
        assert kb.block_task(conn, tid, link_named=True, reason=f"wait on {here} and t_deadbeef", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert (t.status, t.block_kind) == ("blocked", "needs_input")
        assert not kb.parent_ids(conn, tid)
        assert _last_block_payload(conn, tid)["named_cards_not_linked"] == {
            "t_deadbeef": "not on this board",
        }


def test_cycle_falls_back_to_needs_input(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        kid = kb.create_task(conn, title="kid", assignee="x", parents=[tid])
        assert kb.block_task(conn, tid, link_named=True, reason=f"waiting on {kid}", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert (t.status, t.block_kind) == ("blocked", "needs_input")
        assert not kb.parent_ids(conn, tid)
        assert _last_block_payload(conn, tid)["named_cards_not_linked"] == {kid: "would create a cycle"}


def test_capability_block_is_left_alone(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        other = kb.create_task(conn, title="o", assignee="x")
        tid = _running(conn)
        assert kb.block_task(conn, tid, link_named=True, reason=f"no access, see {other}", kind="capability")
        assert kb.get_task(conn, tid).status == "blocked"
        assert not kb.parent_ids(conn, tid)


def test_tool_reports_link_cleared_and_unlinked(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import kanban_tools as kt

    with kbc.connect_closing() as conn:
        open_card = kb.create_task(conn, title="open", assignee="x")
        done = kb.create_task(conn, title="done", assignee="x")
        assert kb.complete_task(conn, done, result="ok")
        tid = _running(conn)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    d = json.loads(kt._handle_block({"reason": f"waiting on {done}", "kind": "needs_input"}))
    assert d["ok"] is True and d["blocked"] is False and done in d["note"]

    d = json.loads(kt._handle_block({"reason": f"waiting on {open_card} and t_00000000", "kind": "needs_input"}))
    assert (d["status"], d["block_kind"]) == ("blocked", "needs_input")
    assert d["named_cards_not_linked"] == {"t_00000000": "not on this board"}


def test_tool_links_open_card(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import kanban_tools as kt

    with kbc.connect_closing() as conn:
        open_card = kb.create_task(conn, title="open", assignee="x")
        tid = _running(conn)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    d = json.loads(kt._handle_block({"reason": f"waiting on {open_card}", "kind": "needs_input"}))
    assert (d["ok"], d["block_kind"], d["auto_linked"]) == (True, "dependency", [open_card])


def test_cli_block_messages(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        open_card = kb.create_task(conn, title="open", assignee="x")
        done = kb.create_task(conn, title="done", assignee="x")
        assert kb.complete_task(conn, done, result="ok")
        a = _running(conn, "a")
        b = _running(conn, "b")
    out = kc.run_slash(f"block {a} waiting on {done}")
    assert "cleared" in out and done in out
    out = kc.run_slash(f"block {b} waiting on {open_card}")
    assert "todo" in out and open_card in out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, a).status == "running"
        assert kb.get_task(conn, b).status == "todo"


def test_system_block_without_flag_is_unchanged(kanban_home: Path) -> None:
    # Goal-loop exhaust and dashboard drags call block_task without link_named.
    with kbc.connect_closing() as conn:
        done = kb.create_task(conn, title="done", assignee="x")
        assert kb.complete_task(conn, done, result="ok")
        other = kb.create_task(conn, title="o", assignee="x")
        tid = _running(conn)
        assert kb.block_task(conn, tid, reason=f"goal budget spent; saw {done} and {other}")
        assert kb.get_task(conn, tid).status == "blocked"
        assert not kb.parent_ids(conn, tid)
