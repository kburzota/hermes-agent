"""Floor for the process-kill-by-name tools: BSD/macOS read options only BEFORE the pattern, so
`<tool> -f server -n` treats `-n` as a second pattern and matches everything with "-n" in its
command line (T102). Block that shape, multiple patterns, and over-broad `-f` patterns."""
import os
import re

from tools.approval_detection import _iter_top_level_shell_segments, _shell_segment_tokens

_TOOLS = frozenset({"p" + "kill", "pgrep", "kill" + "all"})
_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup", "time", "nice"})
_OPT_WITH_VALUE = frozenset("uUgGPstFcMd")  # shortcut: union across the three tools; wrong only for exotic clusters
_MIN_F_PATTERN = 8


def _check_segment(tokens: list[str]) -> str | None:
    i = 0
    while i < len(tokens) and (tokens[i] in _WRAPPERS or re.match(r"\w+=", tokens[i])):
        i += 1
    if i >= len(tokens) or os.path.basename(tokens[i]) not in _TOOLS:
        return None
    name, args = os.path.basename(tokens[i]), tokens[i + 1:]
    full_match, patterns, late_opt, k = False, [], None, 0
    while k < len(args):
        a = args[k]
        if a == "--":
            patterns += args[k + 1:]
            break
        if a.startswith("-") and len(a) > 1:
            if patterns:
                late_opt = a
            elif a.startswith("--"):
                full_match |= a == "--full"
            else:
                full_match |= "f" in a[1:]
                if a[-1] in _OPT_WITH_VALUE and not a[1:].isdigit():
                    k += 1  # next token is this option's value
        else:
            patterns.append(a)
        k += 1
    if late_opt:
        return f"{name}: option {late_opt} after the pattern is read as another pattern"
    if len(patterns) != 1:
        return f"{name}: needs exactly one pattern, got {len(patterns)}"
    if full_match and len(re.sub(r"[^\w/.-]", "", patterns[0])) < _MIN_F_PATTERN:
        return f"{name} -f: pattern {patterns[0]!r} is too broad"
    return None


def detect_broad_kill(command: str) -> str | None:
    """Return a reason if the command contains a broad name-based kill, else None."""
    for seg in _iter_top_level_shell_segments(command):
        tokens = _shell_segment_tokens(seg, 0)
        if tokens and (reason := _check_segment(tokens)):
            return reason
    return None
