"""What encoding do programs write when their output is a pipe, as under an agent harness?

Claude Code (and most harnesses) start bash.exe with a hidden console (Node's windowsHide)
and decode what comes back through the pipe as UTF-8. Windows programs pick their output
encoding in different ways:
- the console's output code page (chcp; the OEM code page by default);
- the ANSI code page (system-wide, needs a reboot to change);
- their own choice: UTF-8 for Node, Go and Rust; UTF-16 for `cmd /u`.
This probe runs a set of programs through a shell, in a fresh hidden console like the
harness's, with the console output code page left alone or set with chcp. It classifies
the raw bytes each one writes.

Usage (on Windows):
  python probe.py --shell "C:\\...\\bash.exe" --out probe.json [--cp default --cp 65001 ...]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

# Text in several scripts; each probe reports which of these came through intact.
SAMPLES = {
    "zh": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
    "ar": "العربية",
    "latin": "café",
    "emoji": "😀",
}
ALL = " ".join(SAMPLES.values())

VCVARS = (
    'vs=$("/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe" -latest '
    "-products '*' -property installationPath | tr -d '\\r'); "
)


def cases():
    return [
        # name, bash command; output should contain the samples it names (or None: info)
        ("sill_echo", f"echo '{ALL}'", True),
        ("sill_ls", "ls", True),
        ("cmd_echo", f"cmd.exe /d /c 'echo {ALL}'", True),
        ("cmd_dir_names", "cmd.exe /d /c 'dir /b'", True),
        ("cmd_error_message", "cmd.exe /d /c nosuchcommand_xyz", None),
        ("cmd_unicode_switch", f"cmd.exe /d /u /c 'echo {ALL}'", True),
        ("where_error_message", "where.exe nosuchcommand_xyz", None),
        ("findstr_utf8_file", "findstr.exe . samples.txt", True),
        ("type_utf8_file", "cmd.exe /d /c 'type samples.txt'", True),
        ("powershell51_names", "powershell.exe -NoProfile -Command 'Get-ChildItem -Name'", True),
        ("powershell51_write", f"powershell.exe -NoProfile -Command \"Write-Output '{ALL}'\"", True),
        ("powershell51_error", "powershell.exe -NoProfile -Command 'Get-Item nosuchfile_xyz'", None),
        ("python_default_sill", f"python -c \"print('{ALL}')\"", True),
        ("python_no_utf8_mode", f"PYTHONUTF8=0 PYTHONIOENCODING= python -c \"print('{ALL}')\"", True),
        ("node", f"node -e \"console.log('{ALL}')\"", True),
        ("git_ls_files", "git ls-files", True),
        ("git_quotepath_off", "git -c core.quotepath=off ls-files", True),
        ("git_error_message", "git nosuchsubcommand_xyz", None),
        ("msvc_cl_error", VCVARS + 'cmd.exe /d /c "call \\"$vs\\VC\\Auxiliary\\Build\\vcvars64.bat\\" >nul && cl /nologo nosuchfile_xyz.c"', None),
        ("ipconfig_head", "ipconfig | head -3", None),
        ("whoami_groups_head", "whoami /groups | head -4", None),
        ("tasklist_head", "tasklist | head -3", None),
        ("robocopy_names", "robocopy . ../rc-out /L /NJH /NJS /NDL /NC /NS /NP", True),
    ]


def classify(data: bytes):
    if not data:
        return "empty"
    if data[:2] in (b"\xff\xfe",) or (len(data) > 3 and data[1:2] == b"\x00" and data[3:4] == b"\x00"):
        return "utf-16le"
    if all(b < 0x80 for b in data):
        return "ascii"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("gbk", "cp932", "cp949", "cp1251", "cp1252"):
        try:
            data.decode(enc)
            return enc + "?"
        except UnicodeDecodeError:
            continue
    return "binary"


def intact(data: bytes, kind: str):
    """Which samples survive when the bytes are read as UTF-8 (what the harness does)."""
    text = data.decode("utf-16le", "replace") if kind == "utf-16le" else data.decode("utf-8", "replace")
    return sorted(k for k, v in SAMPLES.items() if v in text)


def setup(work):
    for v in SAMPLES.values():
        with open(os.path.join(work, f"{v}.txt"), "w", encoding="utf-8") as f:
            f.write(v + "\n")
    with open(os.path.join(work, "samples.txt"), "w", encoding="utf-8") as f:
        f.write(ALL + "\n")
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)


def run(shell, cp, script, work):
    # A fresh hidden console, as Node's spawn(windowsHide) gives bash.exe under a harness.
    pre = "" if cp == "default" else f"chcp.com {cp} >/dev/null; "
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run([shell, "-c", pre + script], cwd=work, capture_output=True,
                       creationflags=flags, timeout=120)
    return p.stdout + p.stderr, p.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", action="append", required=True, help="NAME=PATH")
    ap.add_argument("--cp", action="append", help="console output code page: default, 65001, 932, ...")
    ap.add_argument("--only", action="append")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cps = a.cp or ["default", "65001"]
    shells = [s.split("=", 1) for s in a.shell]
    work = tempfile.mkdtemp(prefix="enc-")
    setup(work)
    results = []
    for name, script, expect in cases():
        if a.only and name not in a.only:
            continue
        for sname, spath in shells:
            for cp in cps:
                data, code = run(spath, cp, script, work)
                kind = classify(data)
                row = {"case": name, "shell": sname, "cp": cp, "exit": code, "kind": kind,
                       "intact": intact(data, kind) if expect else None,
                       "bytes_hex": data[:400].hex(), "len": len(data)}
                results.append(row)
                print(f"{name:22} {sname:8} cp={cp:7} {kind:9} intact={row['intact']}", flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"acp": acp_info(), "results": results}, f, ensure_ascii=False, indent=1)


def acp_info():
    import ctypes
    k = ctypes.windll.kernel32
    return {"acp": k.GetACP(), "oemcp": k.GetOEMCP()}


if __name__ == "__main__":
    sys.exit(main())
