"""Bitrot guard: each gateway bench workload runs one short iteration."""

from __future__ import annotations


def test_fanout_stream_reports_cpu_time():
    from bench.gateway.workloads import fanout_stream

    stats = fanout_stream(subscribers=2, events=8)
    assert stats["iterations"] == 8
    assert stats["cpu_s"] >= 0
    assert stats["events"] == 8


def test_redact_corpus_covers_the_sample_kinds():
    from bench.gateway.workloads import redact_corpus

    stats = redact_corpus(repeats=2)
    assert stats["redact.prose_10kib.cpu_s"] >= 0
    assert stats["redact.secret_bearing.chars"] > stats["redact.status_line.chars"]


def test_kanban_tick_drives_a_board(tmp_path, monkeypatch):
    from bench.gateway.workloads import kanban_tick

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    stats = kanban_tick(tasks=3, ticks=2)
    assert stats["tasks"] == 3
    assert stats["connects"] >= 2
    assert stats["cpu_s"] >= 0
