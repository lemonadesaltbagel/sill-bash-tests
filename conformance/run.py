"""Runs the conformance cases (cases.py) against Bash shells on Windows.

Each shell runs each case as an agent harness runs commands: `<shell> -c <script>`,
started detached (no console, as Claude Code starts it), stdin from NUL, stdout and
stderr piped, in a fresh directory holding the case's files. The environment is the
runner's own, minus Python's UTF-8 switches (they would hide what the shell itself does).

Usage (on Windows):
  python run.py --shell sill=C:\\...\\bash.exe --shell gitbash=... --echoargs echoargs.exe
                --out results.json --report report.md [--only ID ...]
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cases import CASES, text  # noqa: E402

DETACHED_PROCESS = 0x00000008
CATEGORY_TITLES = {
    "argv": "Arguments to native programs", "paths": "Paths", "encoding": "Encoding",
    "crlf": "Line endings and bytes", "process": "Processes", "limits": "Size limits",
    "scripts": "Running scripts", "tools": "Tools agents use", "errors": "Errors and identity",
}


def shell_env():
    env = dict(os.environ)
    for k in ("PYTHONUTF8", "PYTHONIOENCODING"):
        env.pop(k, None)
    return env


def processes_with(fragment):
    """Command lines of running processes that contain `fragment`."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
          "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True).stdout
    lines = out.decode("utf-8", "replace").splitlines()
    return [l for l in lines if fragment in l and "Get-CimInstance" not in l]


def end(pids):
    for pid in pids:
        subprocess.run(["taskkill", "/T", "/F", "/PID", pid], capture_output=True)


def run_case(case, shell, echoargs, timeout):
    work = Path(tempfile.mkdtemp(prefix="conf-"))
    try:
        for rel, data in (case.get("files") or {}).items():
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        script = case["script"].replace("{echoargs}", echoargs.replace("\\", "/"))
        t0 = time.monotonic()
        try:
            # Past Windows' 32,767-character command line, a harness passes a file.
            if len(script) > 30000:
                (work / "_case.sh").write_bytes(script.encode("utf-8"))
                argv = [shell, "_case.sh"]
            else:
                argv = [shell, "-c", script]
            p = subprocess.run(argv, cwd=work, env=shell_env(), stdin=subprocess.DEVNULL,
                               capture_output=True, creationflags=DETACHED_PROCESS, timeout=timeout)
            out, err, code = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, err, code = e.stdout or b"", (e.stderr or b"") + b"\n[timed out]", -1
        secs = time.monotonic() - t0
        if "expect" in case:
            ok = text(out) == case["expect"]
        else:
            try:
                ok = bool(case["check"](out, err, code, secs))
            except Exception:  # noqa: BLE001
                ok = False
        if case.get("after_no_process"):
            left = processes_with(case["after_no_process"])
            if left:
                ok = False
                err += f"\n[left running: {left[0][:120]}]".encode()
                end([l.split()[0] for l in left])
        return {"pass": ok, "code": code, "secs": round(secs, 2),
                "stdout": text(out)[:600], "stderr": text(err)[:600]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def version(shell):
    try:
        p = subprocess.run([shell, "--version"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        return text(p.stdout).splitlines()[0][:80]
    except Exception:  # noqa: BLE001
        return "?"


def report(results, shells, meta):
    names = [n for n, _ in shells]
    cats = list(dict.fromkeys(c["cat"] for c in CASES))
    md = ["# Bash-on-Windows conformance for coding agents", "",
          f"Machine: {meta['os']}, locale code page {meta['acp']}. Run: {meta['date']}.", "",
          "| shell | version |", "|---|---|"]
    md += [f"| {n} | {meta['versions'][n]} |" for n in names]
    md += ["", "## Score by category", "", "| category | cases | " + " | ".join(names) + " |",
           "|---|---|" + "---|" * len(names)]
    totals = defaultdict(int)
    applicable = 0
    for cat in cats:
        ids = [c["id"] for c in CASES if c["cat"] == cat and c["id"] in results]
        applicable += len(ids)
        row = []
        for n in names:
            k = sum(results[i][n]["pass"] for i in ids)
            totals[n] += k
            row.append(f"{k}/{len(ids)}")
        md.append(f"| {CATEGORY_TITLES.get(cat, cat)} | {len(ids)} | " + " | ".join(row) + " |")
    md.append(f"| **total** | {applicable} | " + " | ".join(f"**{totals[n]}/{applicable}**" for n in names) + " |")
    md += ["", "## Cases", "", "| case | why | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for c in CASES:
        if c["id"] not in results:
            continue
        cells = ["✅" if results[c["id"]][n]["pass"] else "❌" for n in names]
        md.append(f"| `{c['id']}` | {c['why']} | " + " | ".join(cells) + " |")
    md += ["", "## Failures", ""]
    for c in CASES:
        for n in names:
            r = results.get(c["id"], {}).get(n)
            if r and not r["pass"]:
                got = (r["stdout"].strip() or r["stderr"].strip()).replace("\n", " ⏎ ")[:160]
                want = c.get("expect", "(check)").replace("\n", " ⏎ ")[:60]
                md.append(f"- **{n}** `{c['id']}`: expected `{want}`, got `{got}` (exit {r['code']})")
    return "\n".join(md) + "\n"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", action="append", required=True, help="NAME=PATH")
    ap.add_argument("--echoargs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--only", action="append")
    ap.add_argument("--timeout", type=int, default=60)
    a = ap.parse_args()
    shells = [s.split("=", 1) for s in a.shell]
    import ctypes
    meta = {"os": f"Windows {platform.version()}", "acp": ctypes.windll.kernel32.GetACP(),
            "date": time.strftime("%Y-%m-%d %H:%M"), "versions": {n: version(p) for n, p in shells}}
    results = {}
    for case in CASES:
        if a.only and case["id"] not in a.only:
            continue
        if any(shutil.which(t) is None for t in case.get("needs", [])):
            print(f"{case['id']:28} skipped (needs {case['needs']})", flush=True)
            continue
        results[case["id"]] = {}
        for name, path in shells:
            r = run_case(case, path, os.path.abspath(a.echoargs), a.timeout)
            results[case["id"]][name] = r
        print(f"{case['id']:28} " + " ".join(f"{n}={'pass' if results[case['id']][n]['pass'] else 'FAIL'}"
                                              for n, _ in shells), flush=True)
    Path(a.out).write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    Path(a.report).write_text(report(results, shells, meta), encoding="utf-8")


if __name__ == "__main__":
    main()
