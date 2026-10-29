"""A claim this machine issued under an earlier hostname must not hold its card forever.

Oct 28 2026: a reboot flipped ``socket.gethostname()`` from ``BIG-IRON.local`` to ``Mac`` (no
pinned HostName, so DHCP's name won). Four cards claimed as ``BIG-IRON.local:2496`` had dead
workers, but every reclaim path compares the lock against ``_host_prefix()`` only, so the claims
read as another host's forever: no reclaim, and no ``stale_running`` diagnostic either, for 2.5 h.
Kanban is single-host by design (docs: kanban.md), so a lock on a board whose DB sits on this
machine's own disk came from this machine whatever name it carries.
"""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli.test_kanban_owner_receipt_reclaim import _Board

REASON = "expired_claim_dead_worker_host_alias"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_claimer_id", lambda: f"NEWHOST:{os.getpid()}")
    monkeypatch.setattr(kb, "_macos_host_names", lambda: (), raising=False)
    scratch = _Board(tmp_path)
    yield scratch
    scratch.close()


def _kinds(board, tid):
    return [(e["kind"], json.loads(e["payload"] or "{}").get("reason")) for e in board.events(tid)]


def _stale_running(board, tid):
    return [d for d in board.diagnostics(tid) if d.kind == "stale_running"]


def test_expired_claim_under_an_old_hostname_with_dead_workers_is_reclaimed(board):
    """The incident: bound run, workers dead, lock ``OLDHOST.local:<deadpid>``, prefix ``NEWHOST:``."""
    assert kb._host_prefix() == "NEWHOST:"
    board.dead_attempt("t_ghost", lock_host="OLDHOST.local", worker_bound=True, expires_age_seconds=30 * 60,
                       heartbeat_age=31 * 60)
    assert _stale_running(board, "t_ghost")                 # named whatever the host, 10 min past expiry
    assert kbd.detect_crashed_workers(board.conn) == ["t_ghost"]
    row = board.state("t_ghost")
    assert (row["status"], row["claim_lock"], row["worker_pid"]) == ("ready", None, None)
    assert ("reclaimed", REASON) in _kinds(board, "t_ghost")


def test_an_unbound_run_under_an_old_hostname_is_released_by_the_ttl_path(board):
    board.dead_attempt("t_ghost", lock_host="OLDHOST.local")
    assert kb.release_stale_claims(board.conn) == 1
    assert board.state("t_ghost")["status"] == "ready"
    assert ("reclaimed", REASON) in _kinds(board, "t_ghost")


def test_a_macos_alias_of_this_machine_counts_as_local_at_once(board, monkeypatch):
    """LocalHostName ``OLDHOST`` makes ``OLDHOST.local`` ours: no expiry grace needed."""
    monkeypatch.setattr(kb, "_macos_host_names", lambda: ("OLDHOST",))
    assert kb._lock_is_local("OLDHOST.local:1") and kb._lock_is_local("OLDHOST:1")
    assert not kb._lock_is_local("elsewhere:1")
    board.dead_attempt("t_ghost", lock_host="OLDHOST.local", worker_bound=True, expires_age_seconds=-3600)
    assert kbd.detect_crashed_workers(board.conn) == ["t_ghost"]
    assert ("crashed", None) in _kinds(board, "t_ghost")


@pytest.mark.parametrize("shape", [
    dict(worker_pid=os.getpid(), worker_fingerprint=kbd._process_fingerprint(os.getpid())),  # live worker
    dict(expires_age_seconds=60),                                                           # inside the grace
], ids=["live-worker", "inside-grace"])
def test_an_old_hostname_claim_that_may_be_alive_keeps_its_card(board, shape):
    board.dead_attempt("t_ghost", lock_host="OLDHOST.local", worker_bound=True, **shape)
    assert kbd.detect_crashed_workers(board.conn) == []
    assert kb.release_stale_claims(board.conn) == 0
    assert board.state("t_ghost")["status"] == "running"


def test_a_board_db_off_this_machine_never_trusts_a_foreign_lock(board, monkeypatch):
    monkeypatch.setattr(kb, "_db_on_local_disk", lambda conn: False)
    board.dead_attempt("t_ghost", lock_host="OLDHOST.local", worker_bound=True)
    assert kbd.detect_crashed_workers(board.conn) == []
    assert kb.release_stale_claims(board.conn) == 0
    assert board.state("t_ghost")["status"] == "running"


def test_the_board_db_under_test_is_on_local_disk(board):
    assert kb._db_on_local_disk(board.conn) is True
