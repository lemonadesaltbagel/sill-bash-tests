"""Shell smoke probes for Windows: brush vs Git Bash (and any other shell).

Each probe runs as `<shell> -c <script>`, through Python's subprocess, which
quotes with MSVCRT rules, as Node/Bun/Rust harnesses do. That deliberately
exercises the harness -> bash.exe boundary where Git Bash halves backslashes
and truncates long commands. Probes target known failure areas: brush issue
#1359 (pipes > 4 KB hang), large here-docs, non-UTF-8-safe command
substitution, MSYS path conversion of `/p:` and `/c` arguments, and exit codes.

Informational: always exits 0. Writes JSON and a markdown table.

Usage: python smoke.py --shell NAME=PATH [--shell ...] --echoargs EXE --out JSON --summary MD
"""
import argparse
import json
import os
import subprocess
import sys
import time

TIMEOUT_S = 30
# CreateProcess caps a command line at 32,767 UTF-16 units; longer probes run from a script file.
MAX_DASH_C = 30_000


def probes(echoargs):
    ea = echoargs.replace("\\", "/")
    big_heredoc_body = ("x" * 99 + "\n") * 2000
    mid_heredoc_body = ("x" * 99 + "\n") * 200
    cmdsub_expected = len("\n".join(str(i) for i in range(1, 20001)))
    return [
        # name, script, expected stdout (after stripping one trailing "\n"), expected exit code
        ("echo", "echo hello", "hello", 0),
        ("pipe_builtin_to_ext_200KB", "printf '%s\\n' {1..40000} | wc -l", "40000", 0),
        ("pipe_ext_to_ext_300KB", "seq 1 50000 | wc -l", "50000", 0),
        ("pipe_ext_to_builtin", "seq 1 30000 | { n=0; while read -r l; do n=$((n+1)); done; echo $n; }", "30000", 0),
        ("heredoc_20KB", "wc -c <<'EOF'\n" + mid_heredoc_body + "EOF", "20000", 0),
        ("heredoc_200KB_file", "wc -c <<'EOF'\n" + big_heredoc_body + "EOF", "200000", 0),
        ("herestring_100KB", "x=$(printf 'y%.0s' {1..100000}); wc -c <<<\"$x\"", "100001", 0),
        ("cmdsub_large", "x=$(seq 1 20000); echo ${#x}", str(cmdsub_expected), 0),
        ("long_command_20K", "x=" + "a" * 20000 + "; echo ${#x}", "20000", 0),
        ("backslashes_literal", "printf '%s\\n' 'a\\\\b' 'C:\\Users\\x'", "a\\\\b\nC:\\Users\\x", 0),
        ("double_quotes", 'printf "%s|" "a b" "c\\"d"; echo', 'a b|c"d|', 0),
        ("pipestatus", "false | true; echo ${PIPESTATUS[@]}", "1 0", 0),
        ("set_e", "set -e; false; echo unreachable", "", 1),
        ("subshell_state", "d=$PWD; x=1; (cd ..; x=2); [ \"$PWD\" = \"$d\" ] && echo $x", "1", 0),
        ("arrays", "a=(x 'y z'); a+=(w); echo ${#a[@]} \"${a[1]}\"", "3 y z", 0),
        ("assoc_array", "declare -A m=([k]=v); echo ${m[k]}", "v", 0),
        ("dbl_bracket", "[[ abc == a* && 5 -gt 3 ]] && echo ok", "ok", 0),
        ("arith", "echo $(( (3+4)*5 % 6 ))", "5", 0),
        ("native_exit_code", "cmd.exe /d /c exit 3; echo $?", "3", 0),
        ("native_argv_slash_opts", f'"{ea}" /p:Configuration=Release /EHsc /c', '["/p:Configuration=Release","/EHsc","/c"]', 0),
        ("native_argv_quotes", f'"{ea}" "a b" \'c"d\' \'e\\\' ""', '["a b","c\\"d","e\\\\",""]', 0),
        ("native_argv_unix_paths", f'"{ea}" --prefix=/usr /tmp/x', '["--prefix=/usr","/tmp/x"]', 0),
        ("unicode", "echo 日本語 | wc -c", "10", 0),
        ("identity", 'echo "$OSTYPE|$(uname -s)"', None, 0),
        ("ls_hides_dotfiles", "d=ls-dot-$$; mkdir $d && touch $d/.hidden $d/visible && ls $d; rm -r $d", "visible", 0),
        ("path_probe", 'case "$PATH" in *";"*) echo windows-form;; /*) echo posix;; *) echo other;; esac', "posix", 0),
        ("dev_null", "echo hi > /dev/null; [ -e nul ] && echo created-nul-file || echo ok", "ok", 0),
        # Is `vcvars64.bat >nul` failing a shell problem or Visual Studio's? Same command, both shells.
        ("vcvars_with_nul", 'vs=$("/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe" -latest -products \'*\' -property installationPath | tr -d \'\\r\'); MSYS2_ARG_CONV_EXCL=\'*\' cmd.exe /d /c "call \\"$vs\\VC\\Auxiliary\\Build\\vcvars64.bat\\" >nul && echo VCOK" 2>&1 | tail -1', None, 0),
        ("vcvars_without_nul", 'vs=$("/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe" -latest -products \'*\' -property installationPath | tr -d \'\\r\'); MSYS2_ARG_CONV_EXCL=\'*\' cmd.exe /d /c "call \\"$vs\\VC\\Auxiliary\\Build\\vcvars64.bat\\" && echo VCOK" 2>&1 | tail -1', None, 0),
        # Timing only: a deep tree walk (Visual Studio is ~100k files) through each shell's find.
        # The first walk pays for a cold file-system cache (54 s vs 1 s warm, measured with the
        # diag workflow), and whichever shell ran first paid it; so warm up, then time.
        ("find_vs_tree", 'vs=$("/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe" -latest -products \'*\' -property installationPath | tr -d \'\\r\'); vs=${vs//\\\\//}; '
         'find "$vs" -name cl.exe >/dev/null 2>&1; '
         's=$(date +%s%N); n=$(find "$vs" -name cl.exe 2>/dev/null | wc -l); e=$(date +%s%N); '
         'echo "$n found, warm $(( (e - s) / 1000000 )) ms"', None, 0),
    ]


def run(shell, script, cwd):
    if len(script) > MAX_DASH_C:
        path = os.path.join(cwd, "probe-script.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script + "\n")
        argv = [shell, path]
    else:
        argv = [shell, "-c", script]
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            timeout=TIMEOUT_S,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        return {
            "exit": p.returncode,
            "stdout": p.stdout.decode("utf-8", "replace"),
            "stderr": p.stderr.decode("utf-8", "replace")[-2000:],
            "timed_out": False,
            "ms": round((time.monotonic() - t0) * 1000),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit": None,
            "stdout": (e.stdout or b"").decode("utf-8", "replace")[-2000:],
            "stderr": (e.stderr or b"").decode("utf-8", "replace")[-2000:],
            "timed_out": True,
            "ms": TIMEOUT_S * 1000,
        }
    except OSError as e:
        return {"exit": None, "stdout": "", "stderr": str(e), "timed_out": False, "ms": 0}


def verdict(r, want_out, want_exit):
    if r["timed_out"]:
        return "HANG"
    if r["exit"] is None:
        return "ERROR"
    out = r["stdout"][:-1] if r["stdout"].endswith("\n") else r["stdout"]
    if want_out is None:
        return "info"
    if "\r" in out and "\r" not in want_out:
        return "CRLF"
    # Whole-output strip: BSD wc pads counts; leading/trailing blanks aren't what these probes test.
    if out.strip() != want_out.strip():
        return "FAIL"
    if r["exit"] != want_exit:
        return f"EXIT {r['exit']}"
    return "pass"


def main():
    # The runner's console code page is cp1252; this report is UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", action="append", required=True, help="NAME=PATH")
    ap.add_argument("--echoargs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--only", action="append", help="run just this probe (repeatable)")
    a = ap.parse_args()
    shells = [s.split("=", 1) for s in a.shell]
    results = []
    for name, script, want_out, want_exit in probes(os.path.abspath(a.echoargs)):
        if a.only and name not in a.only:
            continue
        row = {"probe": name, "script": script if len(script) < 300 else script[:300] + "...", "expected": want_out}
        for sname, spath in shells:
            r = run(spath, script, a.cwd)
            r["verdict"] = verdict(r, want_out, want_exit)
            row[sname] = r
        results.append(row)
        print(name, " ".join(f"{s}={row[s]['verdict']}" for s, _ in shells), flush=True)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    names = [s for s, _ in shells]
    md = ["## Shell smoke probes", "", "| probe | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for row in results:
        cells = []
        for s in names:
            r = row[s]
            cell = r["verdict"] if r["verdict"] != "info" else "`" + r["stdout"].strip().replace("|", "\\|") + "`"
            if r["verdict"] not in ("pass", "info"):
                got = r["stdout"].strip().replace("|", "\\|").replace("\n", "⏎")[:60]
                cell += f" (got `{got}`)" if got else ""
            cells.append(f"{cell} · {r['ms']}ms")
        md.append(f"| {row['probe']} | " + " | ".join(cells) + " |")
    totals = {s: sum(row[s]["verdict"] == "pass" for row in results) for s in names}
    scored = sum(row["expected"] is not None for row in results)
    md += ["", "Pass counts: " + ", ".join(f"{s} {totals[s]}/{scored}" for s in names), ""]
    with open(a.summary, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
