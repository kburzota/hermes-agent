"""Stalled-board WARNING and live settings keys for the embedded dispatcher."""

from __future__ import annotations

import logging

from gateway import kanban_watchers_dispatcher as kwd


def _d():
    return kwd._KanbanDispatcher(None, kwd._DispatcherSettings(60.0, None, 11, 2, 0, True, None, 3, 2))


def test_warns_after_15_min_of_ready_without_spawns(caplog):
    d = _d()
    with caplog.at_level(logging.WARNING):
        assert d.note_board_outcome("bello-lane", True, False, now=0) is False
        assert d.note_board_outcome("bello-lane", True, False, now=899) is False
        assert d.note_board_outcome("bello-lane", True, False, now=900) is True
        assert d.note_board_outcome("bello-lane", True, False, now=1000) is False  # rate limited
        assert d.note_board_outcome("bello-lane", True, False, now=1200) is True
    assert "bello-lane" in caplog.text and "max_in_progress=11" in caplog.text


def test_spawn_or_empty_ready_resets():
    d = _d()
    d.note_board_outcome("b", True, False, now=0)
    d.note_board_outcome("b", True, True, now=800)
    assert d.note_board_outcome("b", True, False, now=1000) is False
    d.note_board_outcome("b", False, False, now=1100)
    assert d.note_board_outcome("b", True, False, now=2000) is False


def test_live_keys_change_on_cap_edit():
    assert kwd._live_keys({"max_in_progress": 11}) != kwd._live_keys({"max_in_progress": 18})
    assert kwd._live_keys({"max_in_progress": 11}) == kwd._live_keys({"max_in_progress": 11, "other": 1})
