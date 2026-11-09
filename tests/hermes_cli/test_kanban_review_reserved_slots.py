"""kanban.review_reserved_slots: builders get at most cap - N slots; review cards use the reserve."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def kanban(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_review_reserve_test_")
    for prof in ("alpha", "beta", "gamma", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db, kanban_db_dispatch
    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    kanban_db.create_board(slug="default", name="Test")
    return kanban_db


def _spawn(*args, **kwargs):
    return 12345


def _seed(kb, builders_running, ready_titles):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        for i in range(builders_running):
            tid = kb.create_task(conn, title=f"build running {i}", assignee="alpha")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        for title in ready_titles:
            kb.create_task(conn, title=title, assignee="beta")


def _tick(reserve, cap=4):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_spawn, dry_run=True, max_in_progress=cap,
            reconcile_orphans=False, review_reserved_slots=reserve,
        )
        titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM tasks")}
    return [titles[s[0]] for s in res.spawned]


def test_review_card_spawns_while_builders_hold_cap_minus_reserve(kanban):
    _seed(kanban, 2, ["new builder", "pr-chain review: o/r#1", "pr-chain merge: o/r#1"])
    spawned = _tick(reserve=2)
    assert sorted(spawned) == ["pr-chain merge: o/r#1", "pr-chain review: o/r#1"]


def test_no_reserve_is_old_behaviour(kanban):
    _seed(kanban, 2, ["new builder", "pr-chain review: o/r#1"])
    assert len(_tick(reserve=0)) == 2


def test_builders_below_limit_still_spawn_up_to_limit(kanban):
    _seed(kanban, 1, ["b1", "b2", "pr-chain review: o/r#1"])
    spawned = _tick(reserve=2)
    assert spawned[0] == "pr-chain review: o/r#1"  # review first
    assert len(spawned) == 2  # review + one builder (cap 4 - 2 reserved - 1 running = 1)


def test_review_title_match_is_case_insensitive_prefix_only(kanban):
    from hermes_cli import kanban_db_dispatch as kbd
    assert kbd.is_review_title("PR-Chain Review: x")
    assert not kbd.is_review_title("fix: pr-chain review: x")
    assert not kbd.is_review_title(None)


def test_capacity_snapshot_and_setting(kanban):
    from gateway import kanban_watchers_dispatcher as d
    _seed(kanban, 2, ["waiting builder", "pr-chain review: o/r#1"])
    disp = d._KanbanDispatcher(kanban, None)
    assert disp.capacity_snapshot() == (2, 2, 2)
    kb_cfg = {"review_reserved_slots": 2, "max_in_progress": 11}
    assert d._resolve_dispatcher_settings(kb_cfg, kanban).review_reserved_slots == 2
    assert d._resolve_dispatcher_settings({}, kanban).review_reserved_slots == 0
