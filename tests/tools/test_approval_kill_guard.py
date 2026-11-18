"""T102: name-based kill floor."""
import pytest

from tools import approval as mod
from tools.approval_kill_guard import detect_broad_kill

K, KA = "p" + "kill", "kill" + "all"
TONIGHT = f'{K} -f "design-review-server" -n'  # the exact command that took down the Mac


@pytest.mark.parametrize("cmd", [
    TONIGHT,
    f"{K} -f design-review-server -n",
    "pgrep -f design-review-server -n",
    f"{K} server -9",
    f"{K} foo bar",
    f"{KA} foo bar",
    f"{K} -f node",
    f"{K} -f ''",
    K,
    f"echo hi; sudo {K} -f x -n",
])
def test_blocked(cmd):
    assert detect_broad_kill(cmd)


@pytest.mark.parametrize("cmd", [
    f"{K} -f /Users/tuscan/.agent-kit/skills/design-feedback/scripts/design-review-server",
    f"{K} -9 -f /opt/app/bin/server.py",
    f"{K} -u tuscan myproc",
    "pgrep -n myproc",
    "kill 1234",
    "kill -9 1234",
    f"echo {K} -f x -n",
    "ls | grep -n foo",
])
def test_allowed(cmd):
    assert detect_broad_kill(cmd) is None


def test_floor_blocks_even_under_yolo(monkeypatch):
    monkeypatch.setattr(mod, "_yolo_active", lambda: True)
    r = mod.check_dangerous_command(TONIGHT, "local")
    assert r["approved"] is False and r.get("hardline")
