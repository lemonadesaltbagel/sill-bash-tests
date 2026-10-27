"""Terminal-Bench tasks on native Windows: does a correct bash solution work in each shell?

Terminal-Bench (2.1: github.com/harbor-framework/terminal-bench-2-1) gives each task an
instruction, a Linux container (environment/Dockerfile), a reference solution in bash
(solution/solve.sh) and a verifier (tests/test.sh, which writes /logs/verifier/reward.txt).
This runs them natively on Windows:

- The task's Linux directories (/app, /tests, /logs, /protected, /solution, ...) become one
  Windows folder, written with `/` (`D:/tb/app`), in every text file: the same text works in
  any shell and in Python.
- The environment is rebuilt from the Dockerfile: a uv virtual environment (the image's
  Python, with `python3` and `pip3`) first on PATH, `pip install` into it, COPY and WORKDIR
  mirrored, other RUN lines through Git Bash, apt packages from a map of what Windows has
  (unmapped ones make the task not portable, with the reason).
- The reference solution runs in each shell under test, with its apt lines neutralized (the
  environment already has those packages).
- The verifier runs through Git Bash for every shell, so the check doesn't depend on the
  shell under test; its result is the reward file.

A task's outcome per shell is whether the reference solution, run by that shell, passes.

Usage (on Windows):
  python tbench_windows.py --tasks-dir tb/tasks --shell sill=C:\\...\\bash.exe --shell gitbash="C:\\Program Files\\Git\\bin\\bash.exe"
                           --root D:\\tb --out results.jsonl [--only a,b] [--shard 1/4]
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
# Top-level Linux directories a task uses as its own; each becomes ROOT/<name>.
TASK_DIRS = ("app", "tests", "logs", "protected", "solution", "build", "workspace", "output", "data", "root")
DIR_RE = re.compile(r"(?<![\w.~$}:/-])/(" + "|".join(TASK_DIRS) + r")(?=[/\s\"'`),:;\]]|$)")

# apt packages, by what provides them on a Windows runner. None: present (or only needed by
# Terminal-Bench's own recording tools); a string: a Chocolatey package to install.
APT = {
    **dict.fromkeys(["git", "curl", "ca-certificates", "gnupg", "tmux", "asciinema", "nano", "vim", "less",
                     "procps", "python3", "python3-pip", "python3-venv", "python3-dev", "python-is-python3",
                     "python3-setuptools", "python3-wheel", "nodejs", "npm", "cmake", "openssl", "unzip",
                     "zip", "xz-utils", "tar", "gzip", "bzip2", "file", "jq", "patch", "diffutils",
                     "findutils", "coreutils", "sed", "grep", "gawk", "software-properties-common",
                     "apt-transport-https", "lsb-release", "locales", "tzdata", "golang", "golang-go"], None),
    **dict.fromkeys(["build-essential", "gcc", "g++", "make", "pkg-config", "binutils", "libc6-dev"], "mingw"),
    "wget": "wget",
    "sqlite3": "sqlite",
}


def rewrite(text, root):
    return DIR_RE.sub(lambda m: f"{root}/{m.group(1)}", text)


def is_text(path):
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\0" not in chunk


def copy_tree(src, dst, root):
    """Copies src to dst, rewriting the task's paths in text files."""
    if src.is_dir():
        for p in src.rglob("*"):
            if p.is_file():
                copy_tree(p, dst / p.relative_to(src), root)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_text(src) and src.stat().st_size < 5_000_000:
        dst.write_text(rewrite(src.read_text(encoding="utf-8", errors="surrogateescape"), root),
                       encoding="utf-8", errors="surrogateescape", newline="")
    else:
        shutil.copy2(src, dst)


APT_SEGMENT = re.compile(r"(?:\b(?:DEBIAN_FRONTEND|TZ)=\S+\s+)*\b(?:sudo\s+)?apt(?:-get)?\s+[^&;|\n]*")
PKG_WORD = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")


def apt_packages(script):
    pkgs = set()
    for m in re.finditer(r"\bapt(?:-get)?\s+install\s+([^&;|\n]*)", script):
        for w in m.group(1).split():
            if not w.startswith("-") and PKG_WORD.match(w.split("=")[0]):
                pkgs.add(w.split("=")[0])
    return pkgs


def neutralize(script):
    """A bash script with its apt commands as `:` (the environment has the packages), the uv
    installer and its `source ~/.local/bin/env` too (uv is on PATH), and Linux venv paths in
    their Windows form."""
    script = script.replace("\\\r\n", " ").replace("\\\n", " ")
    script = APT_SEGMENT.sub(":", script)
    script = re.sub(r"curl[^\n|]*astral\.sh/uv[^\n|]*\|\s*(?:ba)?sh", ":", script)
    script = re.sub(r"(?:source|\.)\s+\$HOME/\.local/bin/env", ":", script)
    script = re.sub(r"(\.?\w*venv\w*|\.tb|\.venv)/bin/activate", r"\1/Scripts/activate", script)
    return script


class Task:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.toml = tomllib.loads((self.path / "task.toml").read_text(encoding="utf-8"))
        self.dockerfile = (self.path / "environment" / "Dockerfile").read_text(encoding="utf-8", errors="replace")
        self.solve = (self.path / "solution" / "solve.sh").read_text(encoding="utf-8", errors="replace")
        self.instruction = (self.path / "instruction.md").read_text(encoding="utf-8", errors="replace")

    def timeout(self, section, default):
        return float(self.toml.get(section, {}).get("timeout_sec", default))

    def python_version(self):
        m = re.search(r"^FROM\s+(?:\S+/)?python:(\d+\.\d+)", self.dockerfile, re.M)
        return m.group(1) if m else "3.12"

    def needs(self):
        """apt packages the task needs that Windows has no mapping for."""
        verifier = (self.path / "tests" / "test.sh").read_text(encoding="utf-8", errors="replace")
        pkgs = apt_packages(self.dockerfile.replace("\\\n", " ")) | apt_packages(self.solve) | apt_packages(verifier)
        return sorted(p for p in pkgs if p not in APT), sorted({APT[p] for p in pkgs if APT.get(p)})


def sh(shell, script_path, cwd, env, timeout, log):
    """Runs a bash script with `shell`; returns (exit code, or None on a timeout; seconds; output tail)."""
    t0 = time.monotonic()
    p = subprocess.Popen([shell, str(script_path)], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out = b""
        code = None
    text = out.decode("utf-8", "replace")
    log.write(f"\n$ {shell} {script_path}\n{text[-20000:]}\n[exit {code}]\n")
    log.flush()
    return code, time.monotonic() - t0, text[-1500:]


def provision(choco, log):
    """Installs the Chocolatey packages not yet installed in this job."""
    done = provision.done
    for pkg in sorted(set(choco) - done):
        if pkg == "mingw":
            done.add(pkg)  # The runner has MinGW-w64; its bin is put on PATH (see environment()).
            continue
        p = subprocess.run(["choco", "install", "-y", "--no-progress", pkg], capture_output=True, timeout=900)
        log.write(f"\n$ choco install {pkg}\n{p.stdout.decode(errors='replace')[-2000:]}\n")
        done.add(pkg)


provision.done = set()


def end_processes_under(root):
    """Ends what a run left running from under `root` (a server, a test's child)."""
    ps = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{root}\\*' -or "
          f"$_.CommandLine -like '*{root}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force "
          f"-ErrorAction SilentlyContinue }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=120)


def fresh_root(base):
    """A new, empty folder under `base` for one run (an earlier one may still be locked)."""
    fresh_root.n += 1
    root = base / f"r{fresh_root.n}"
    for old in base.glob("r*"):
        if old != root:
            end_processes_under(old)
            shutil.rmtree(old, ignore_errors=True)
    root.mkdir(parents=True)
    return root


fresh_root.n = 0


def environment(task, root, log):
    """Builds the task's environment in the empty folder `root`; returns (env, workdir) or raises."""
    for d in ("app", "logs/verifier", "logs/agent", "tests"):
        (root / d).mkdir(parents=True, exist_ok=True)
    rootp = root.as_posix()
    copy_tree(task.path / "tests", root / "tests", rootp)
    copy_tree(task.path / "solution", root / "solution", rootp)

    venv = root / "venv"
    p = subprocess.run(["uv", "venv", "--seed", "--python", task.python_version(), str(venv)], capture_output=True)
    if p.returncode:
        raise RuntimeError(f"python {task.python_version()}: {p.stderr.decode(errors='replace')[-300:]}")
    scripts = venv / "Scripts"
    for name, alias in (("python.exe", "python3.exe"), ("pip.exe", "pip3.exe")):
        if (scripts / name).exists() and not (scripts / alias).exists():
            shutil.copy2(scripts / name, scripts / alias)
    path = [str(scripts)]
    if Path(r"C:\mingw64\bin").is_dir():
        path.append(r"C:\mingw64\bin")
    env = dict(os.environ, VIRTUAL_ENV=str(venv), PATH=os.pathsep.join(path + [os.environ["PATH"]]),
               PYTHONUTF8="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               # git as in the task's Linux container: files checked out as committed (Git for
               # Windows converts to CRLF by default, which changes checksums and diffs).
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="false")

    workdir = root / "app"
    lines = task.dockerfile.replace("\\\r\n", " ").replace("\\\n", " ").splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        word, _, rest = line.partition(" ")
        word = word.upper()
        rest = rest.strip()
        if word == "WORKDIR":
            target = rewrite(rest, rootp)
            workdir = Path(target) if re.match(r"^[A-Za-z]:", target) else workdir / target
            workdir.mkdir(parents=True, exist_ok=True)
        elif word == "ENV":
            for k, v in re.findall(r"(\w+)=(\"[^\"]*\"|\S+)", rest) or [tuple(rest.split(None, 1))]:
                env[k] = rewrite(v.strip('"'), rootp)
        elif word in ("COPY", "ADD"):
            args = [a for a in shlex.split(rest) if not a.startswith("--")]
            if args and args[0].startswith("["):
                args = json.loads(rest)
            *srcs, dst = args
            dst = rewrite(dst, rootp)
            dstp = Path(dst) if re.match(r"^[A-Za-z]:", dst) else workdir / dst
            for s in srcs:
                src = task.path / "environment" / s
                if not src.exists():
                    continue
                if src.is_dir():
                    copy_tree(src, dstp, rootp)
                else:
                    target = dstp / src.name if (dst.endswith("/") or dstp.is_dir()) else dstp
                    copy_tree(src, target, rootp)
        elif word == "RUN":
            cmd = neutralize(rewrite(rest, rootp))
            if re.fullmatch(r"[:\s&;]*", cmd):
                continue
            step = root / "_env_step.sh"
            step.write_text(cmd + "\n", encoding="utf-8", newline="\n")
            code, _, tail = sh(GIT_BASH, step, workdir, env, 1800, log)
            step.unlink(missing_ok=True)
            if code:
                raise RuntimeError(f"RUN failed ({code}): {cmd[:200]} :: {tail[-300:]}")
    return env, workdir


def verify(task, root, workdir, env, log):
    test = root / "tests" / "test.sh"
    script = root / "_verify.sh"
    script.write_text(neutralize(test.read_text(encoding="utf-8", errors="replace")), encoding="utf-8", newline="\n")
    reward = root / "logs" / "verifier" / "reward.txt"
    reward.unlink(missing_ok=True)
    code, secs, tail = sh(GIT_BASH, script, workdir, env, task.timeout("verifier", 900), log)
    try:
        value = float(reward.read_text().strip() or 0)
    except (OSError, ValueError):
        value = None
    return value, tail


def one(task, shells, root, log):
    rec = {"task": task.name, "category": task.toml.get("metadata", {}).get("category"),
           "difficulty": task.toml.get("metadata", {}).get("difficulty")}
    missing, choco = task.needs()
    if missing:
        return {**rec, "outcome": "needs", "detail": ", ".join(missing)}
    provision(choco, log)
    rec["shells"] = {}
    for name, shell in shells:
        run_root = fresh_root(root)
        try:
            env, workdir = environment(task, run_root, log)
        except Exception as e:  # noqa: BLE001
            return {**rec, "outcome": "environment_failed", "detail": str(e)[:600]}
        solve = run_root / "solution" / "solve.sh"
        body = neutralize(solve.read_text(encoding="utf-8", errors="replace"))
        if one.trace:
            # Where a run stops: each command, as it runs (the last lines are in solve_tail).
            body = "set -x\n" + body
        solve.write_text(body, encoding="utf-8", newline="\n")
        limit = one.solve_timeout or task.timeout("agent", 900) * 2
        code, secs, tail = sh(shell, solve, workdir, env, limit, log)
        reward, vtail = verify(task, run_root, workdir, env, log)
        end_processes_under(run_root)
        rec["shells"][name] = {"reward": reward, "solve_exit": code, "solve_seconds": round(secs),
                               "solve_tail": tail[-500:], "verify_tail": vtail[-800:]}
        print(f"  {task.name} [{name}]: reward={reward} solve_exit={code} ({round(secs)} s)", flush=True)
    rewards = [s["reward"] for s in rec["shells"].values()]
    rec["outcome"] = "works" if any(r == 1 for r in rewards) else ("verifier_failed" if all(r is None for r in rewards) else "fails")
    return rec


one.trace = False
one.solve_timeout = 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--shell", action="append", required=True, help="NAME=PATH to a bash")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--shard", default="")
    ap.add_argument("--trace", action="store_true", help="run reference solutions with set -x")
    ap.add_argument("--solve-timeout", type=float, default=0, help="seconds (default: twice the task's)")
    a = ap.parse_args()
    one.trace, one.solve_timeout = a.trace, a.solve_timeout
    shells = [tuple(s.split("=", 1)) for s in a.shell]
    names = sorted(p.name for p in Path(a.tasks_dir).iterdir() if (p / "task.toml").exists())
    if a.only:
        names = [n for n in names if n in set(a.only.split(","))]
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        names = names[i - 1::n]
    root = Path(a.root)
    with open(a.out, "w", encoding="utf-8") as out, open(Path(a.out).with_suffix(".log"), "w", encoding="utf-8") as log:
        for name in names:
            t0 = time.monotonic()
            try:
                rec = one(Task(Path(a.tasks_dir) / name), shells, root, log)
            except Exception as e:  # noqa: BLE001
                rec = {"task": name, "outcome": "error", "detail": repr(e)[:600]}
            rec["seconds"] = round(time.monotonic() - t0)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"{name}: {rec['outcome']} ({rec['seconds']} s) {rec.get('detail', '')[:150]}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
