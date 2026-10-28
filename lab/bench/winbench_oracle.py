"""Can each Windows-boundary task (winbench_tasks.py) be done on this machine? Runs each
task's reference bash solution in each shell, in a fresh folder, and applies the task's own
check: a task no shell's reference run passes is broken here, not a measure of anything.

Usage (on Windows): python winbench_oracle.py --shell sill=...\\bash.exe --shell gitbash=... --out oracle.jsonl
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winbench_tasks as wb  # noqa: E402


def end_under(root):
    ps = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{root}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    shells = [tuple(s.split("=", 1)) for s in a.shell]
    only = set(filter(None, a.only.split(",")))
    base = Path(tempfile.mkdtemp(prefix="wb-"))
    with open(a.out, "w", encoding="utf-8") as out:
        for task in wb.TASKS:
            if only and task["id"] not in only:
                continue
            rec = {"task": task["id"], "shells": {}}
            for name, shell in shells:
                root = base / f"{task['id']}-{name}"
                shutil.rmtree(root, ignore_errors=True)
                root.mkdir(parents=True)
                t0 = time.monotonic()
                try:
                    task["setup"](str(root))
                    (root / "_solve.sh").write_text(task["solution"], encoding="utf-8", newline="\n")
                    p = subprocess.run([shell, "_solve.sh"], cwd=root, capture_output=True, timeout=task.get("timeout", 900),
                                       stdin=subprocess.DEVNULL)
                    (root / "_solve.sh").unlink(missing_ok=True)
                    solve_out = (p.stdout + p.stderr).decode("utf-8", "replace")[-800:]
                    ok, detail = task["check"](str(root))
                except Exception as e:  # noqa: BLE001
                    ok, detail, solve_out = False, f"error: {e!r}", ""
                end_under(root)
                rec["shells"][name] = {"passed": ok, "detail": detail[:500], "solve_tail": solve_out,
                                       "seconds": round(time.monotonic() - t0)}
                print(f"{task['id']} [{name}]: {'PASS' if ok else 'FAIL'} {detail[:160]}", flush=True)
            rec["works"] = any(s["passed"] for s in rec["shells"].values())
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()


if __name__ == "__main__":
    sys.exit(main())
