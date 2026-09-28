"""Re-verify that a pytest run cannot touch the real settings files."""
import hashlib
import subprocess
import sys
from pathlib import Path

PY = str(Path(".venv/Scripts/python.exe").resolve())
TARGETS = ["data_local/agent_settings.json", "data_local/ai_config.json"]


def digests():
    out = {}
    for t in TARGETS:
        p = Path(t)
        out[t] = (hashlib.sha256(p.read_bytes()).hexdigest()[:12],
                  p.stat().st_mtime) if p.exists() else ("MISSING", 0)
    return out


before = digests()
for t, (h, m) in before.items():
    print(f"before {t}: {h}")

r = subprocess.run([PY, "-m", "pytest", "tests", "-q", "--no-header",
                    "-p", "no:cacheprovider"], capture_output=True, text=True)
line = [l for l in r.stdout.splitlines() if "passed" in l or "failed" in l]
print("pytest:", line[-1] if line else "(none)")

after = digests()
bad = []
for t in TARGETS:
    same = before[t][0] == after[t][0]
    print(f"  {'UNCHANGED' if same else 'CHANGED  '} {t}: {before[t][0]} -> {after[t][0]}")
    if not same:
        bad.append(t)
print("\nRESULT:", "USER DATA SAFE" if not bad else f"CLOBBERED: {bad}")
sys.exit(1 if bad else 0)
