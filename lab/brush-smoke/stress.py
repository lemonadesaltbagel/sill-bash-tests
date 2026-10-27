"""Runs scripts through a shell many times to catch intermittent hangs.

Each script goes to the shell on stdin, as brush's compat harness gives it, with a timeout.
A run that hangs is reported with the shell's process tree at that moment, then killed.

Usage: python stress.py --shell C:\\...\\bash.exe --count 200 [--timeout 20] [--out stress.md]
       (the scripts are SCRIPTS below)
"""
import argparse
import subprocess
import sys
import time

SCRIPTS = {
    "procsub-basic": "shopt -u -o posix\necho <(:) >(:)\necho <(:) <(:)\necho >(:) >(:)\n",
    "procsub-builtins": "source <(echo VAR=100)\necho \"var: ${VAR}\"\n",
    "procsub-input-redirection": "shopt -u -o posix\ncat < <(echo hi)\n",
    "procsub-input": "shopt -u -o posix\nvar=value\ncat <(var=updated; echo ${var})\necho Done.\n",
    "procsub-input-output": "shopt -u -o posix\ncp <(echo hi) >(cat)\n",
    "procsub-diff": "diff <(printf 'a\\nb\\n') <(printf 'a\\nc\\n'); echo rc=$?\n",
}


def tree(pid):
    ps = (f"$all = Get-CimInstance Win32_Process; function t($p, $d) {{ $all | Where-Object "
          f"{{ $_.ParentProcessId -eq $p }} | ForEach-Object {{ ('  ' * $d) + $_.ProcessId + ' ' + "
          f"$_.CommandLine; t $_.ProcessId ($d + 1) }} }}; t {pid} 0")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    return out.stdout.decode("utf-8", "replace").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", required=True)
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--out", default="stress.md")
    a = ap.parse_args()

    md = [f"# Stress: {a.count} runs each, {a.timeout:.0f} s timeout", "",
          "| script | runs | hangs | slowest (s) |", "|---|---|---|---|"]
    details, total = [], 0
    for name, script in SCRIPTS.items():
        hangs, slowest = 0, 0.0
        for i in range(a.count):
            t0 = time.monotonic()
            p = subprocess.Popen([a.shell], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            try:
                p.communicate(script.encode(), timeout=a.timeout)
            except subprocess.TimeoutExpired:
                hangs += 1
                if hangs <= 3:
                    details += [f"### {name}, run {i}", "", "```", f"{p.pid} {a.shell}", tree(p.pid), "```", ""]
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
                p.communicate()
            slowest = max(slowest, time.monotonic() - t0)
        total += hangs
        md.append(f"| {name} | {a.count} | {hangs} | {slowest:.1f} |")
        print(md[-1], flush=True)
    md += ["", *details]
    open(a.out, "w", encoding="utf-8").write("\n".join(md))
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
