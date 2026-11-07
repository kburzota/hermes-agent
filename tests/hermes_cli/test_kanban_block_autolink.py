"""Auto-link of same-run helper cards on a dependency block (agent-kit #86)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _last_blocked(conn, tid):
    kinds = ("blocked", "dependency_wait")
    return [e for e in kb.list_events(conn, tid) if e.kind in kinds][-1].payload


def test_auto_links_same_run_helper(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, "waiter")
        helper = kb.create_task(conn, title="helper", assignee="alice", creator_task_id=tid)
        assert kb.block_task(conn, tid, reason="wait for helper", kind="dependency")
        t = kb.get_task(conn, tid)
        assert (t.status, t.block_kind) == ("todo", "dependency")
        assert _last_blocked(conn, tid)["auto_linked_parents"] == [helper]
        assert kb.complete_task(conn, helper, result="ok")
        assert kb.get_task(conn, tid).status == "ready"


def test_skips_descendant_older_run_and_done_then_flags_rekind(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, "waiter")
        # a child of self would make a cycle
        kb.create_task(conn, title="kid", assignee="alice", parents=[tid], creator_task_id=tid)
        old = kb.create_task(conn, title="old", assignee="alice", creator_task_id=tid)
        done = kb.create_task(conn, title="done", assignee="alice", creator_task_id=tid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 3600 "
                "WHERE task_id = ? AND kind = 'created'", (old,),
            )
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (done,))
        fired: list[str] = []
        monkeypatch.setattr(kb, "_fire_task_hook", lambda name, *a, **k: fired.append(name))
        assert kb.block_task(conn, tid, reason="waiting on x", kind="dependency")
        t = kb.get_task(conn, tid)
        assert (t.status, t.block_kind) == ("blocked", "needs_input")
        ev = _last_blocked(conn, tid)
        assert "auto_linked_parents" not in ev and ev["rekind_reason"] == "no_open_parent"
        assert ev["reason"].startswith(kb.STRANDED_DEPENDENCY_HEADER + "\nwaiting on x")
        assert fired == ["kanban_task_blocked"]
