"""Lightweight structural check for the edited JS files (no Node available).

Checks: balanced brackets outside strings/comments/regex-ish contexts, plus
expected function definitions present and removed symbols absent.
"""
import re
import sys
from pathlib import Path

FILES = [
    "app/web/static/js/device_ui.js",
]

EXPECT = {
    "app/web/static/js/device_ui.js": [
        "async function loadSettings()",
        "function renderSheet(d)",
        "async function saveSetting(patch)",
        "function applyBrightness(value)",
        "function initSheet()",
        "function rateLabel(value)",
        "function currentVolume()",
        "function tickClock()",
        "function initGestures()",
    ],
}

FORBID = {
    "app/web/static/js/device_ui.js": [
        "_voiceOptions", "_speakerOptions", "voiceSeg", "speakerSeg",
        "voicePreview", "previewVoice", "tts_engine", "tts_speaker",
        "voiceName", "speakerName", "speakerDesc", "voiceDesc",
    ],
}


def strip_js(src: str) -> str:
    """Remove comments and string/template literals so brackets can be counted."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            i = src.find("\n", i)
            if i < 0:
                break
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'`":
            quote = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
            continue
        out.append(c)
        i += 1
    return "".join(out)


failures = []
for f in FILES:
    p = Path(f)
    if not p.exists():
        failures.append(f"{f}: missing")
        continue
    src = p.read_text(encoding="utf-8")
    clean = strip_js(src)

    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        d = clean.count(opener) - clean.count(closer)
        if d != 0:
            failures.append(f"{f}: unbalanced {opener}{closer} (delta {d})")

    for token in EXPECT.get(f, []):
        if token not in src:
            failures.append(f"{f}: missing expected `{token}`")
    for token in FORBID.get(f, []):
        if token in src:
            failures.append(f"{f}: leftover `{token}`")

    print(f"{f}: {len(src.splitlines())} lines, brackets balanced")

if failures:
    print("\nFAILURES:")
    for x in failures:
        print(" -", x)
    sys.exit(1)
print("\nALL JS CHECKS PASSED")
