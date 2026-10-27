"""Which SWE-bench tasks work on native Windows? (No model involved.)

SWE-bench's tasks are set up and checked in Linux containers. To use them as real-project
tasks for comparing shells on Windows, a task must first work there on its own merits: for
each task this sets up the repository at its base commit in a virtual environment (uv, the
task's Python version, its dependencies as SWE-bench's specs give them), applies the task's
test patch and runs its tests without the reference fix (the FAIL_TO_PASS tests must fail),
then with it (they must pass). The PASS_TO_PASS tests that also pass are recorded: those are
the regression checks that mean something on Windows. A task works when every FAIL_TO_PASS
test passes with the fix and at least one fails without it; those that do are its check.

Setup and test commands run through Git Bash (the specs' commands are bash), the same for
every task, so a task's outcome here doesn't depend on the shell later compared.

Usage (on Windows):
  python swebench_windows.py --repo django/django --count 8 --work D:\\swe --out results.jsonl
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
from pathlib import Path

from datasets import load_dataset

if os.name == "nt":
    # swebench's package imports `resource` (Unix only) for its Docker image builder, which
    # isn't used here; its specs, test directives and log parsers are.
    import types
    _resource = types.ModuleType("resource")
    _resource.RLIMIT_NOFILE = 7
    _resource.getrlimit = lambda _kind: (8192, 8192)
    _resource.setrlimit = lambda _kind, _limits: None
    sys.modules["resource"] = _resource

from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
from swebench.harness.test_spec.python import get_environment_yml, get_requirements, get_test_directives

# (Off Windows, for trying this out: the system's bash and a venv's bin/.)
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else "bash"
BIN, PY = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
START, END = ">>>>> Start Test Output", ">>>>> End Test Output"


def sh(script, cwd, env, timeout, log):
    """Runs a bash script with Git Bash; returns (exit code or None on timeout, output)."""
    path = Path(cwd) / "_swe_step.sh"
    # The task's environment first on PATH, whatever bash's own start-up put there.
    venv = env.get("VIRTUAL_ENV", "")
    first = f'export PATH="$(cygpath -u \'{venv}\')/{BIN}:$PATH"\n' if os.name == "nt" and venv else ""
    path.write_text(first + script, encoding="utf-8", newline="\n")
    p = subprocess.Popen([GIT_BASH, "--noprofile", "--norc", str(path)], cwd=cwd, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        out, _ = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.kill()
        out, _ = p.communicate()
        code = None
    path.unlink(missing_ok=True)
    text = out.decode("utf-8", "replace")
    log.write(f"\n$ {script.strip()[:2000]}\n{text}\n[exit {code}]\n")
    log.flush()
    return code, text


def run(argv, cwd=None, env=None, timeout=1800, log=None):
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    text = (p.stdout + p.stderr).decode("utf-8", "replace")
    if log:
        log.write(f"\n$ {' '.join(map(str, argv))}\n{text}\n[exit {p.returncode}]\n")
        log.flush()
    return p.returncode, text


def conda_to_pip(spec):
    """`numpy=1.19`, `'scipy==1.5.2'` as pip requirements; conda-only names dropped."""
    out = []
    for p in shlex.split(spec):
        if p.startswith("python") or p in ("pip", "setuptools-scm", "conda"):
            continue
        out.append(re.sub(r"(?<![=<>!])=(?!=)", "==", p))
    return out


def yml_deps(yml):
    """The dependencies of an environment.yml (its `dependencies:` section, conda's and
    pip's), as pip requirements. Best effort: conda-only packages fail to install later."""
    deps, section, pip_indent = [], None, None
    for raw in yml.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "-")):
            section = line.split(":", 1)[0].strip()
            continue
        if section != "dependencies":
            continue
        m = re.match(r"^(\s*)-\s+(.+)$", line)
        if not m:
            continue
        indent, item = len(m.group(1)), m.group(2).strip().strip("'\"")
        if item.rstrip(":") == "pip":
            pip_indent = indent
            continue
        if pip_indent is not None and indent > pip_indent:
            deps.append(item)
            continue
        pip_indent = None
        if not item.startswith(("python", "pip")):
            deps += conda_to_pip(item)
    return deps


def has_module(venv, name):
    p = subprocess.run([str(venv / BIN / PY), "-c", f"import {name}"], capture_output=True)
    return p.returncode == 0


def evaluate(log_text, parser):
    body = log_text.split(START, 1)[-1].split(END, 1)[0]
    return parser(body, None)


def passed(status):
    return status in ("PASSED", "XFAIL")


def clone(repo, dest, log):
    """Clones `repo` from GitHub into `dest` (line endings as committed); True on success."""
    if dest.exists():
        return True
    code, _ = run(["git", "clone", "-q", "-c", "core.autocrlf=false", f"https://github.com/{repo}", str(dest)],
                  timeout=1800, log=log)
    return not code


def prepare(inst, src, venv, work, log):
    """Sets up the task in the clone `src`: its base commit, a virtual environment `venv`
    with its dependencies, and the package installed. Returns (record, env): the record has
    an `outcome` when setup failed; env runs things in the environment."""
    repo, version = inst["repo"], inst["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    rec = {"instance_id": inst["instance_id"], "repo": repo, "version": version,
           "python": specs.get("python"), "skipped_steps": []}
    t0 = time.monotonic()
    run(["git", "-C", str(src), "reset", "-q", "--hard"], log=log)
    run(["git", "-C", str(src), "clean", "-qfdx", "-e", venv.name], log=log)
    code, _ = run(["git", "-C", str(src), "checkout", "-q", "-f", inst["base_commit"]], log=log)
    if code:
        return {**rec, "outcome": "checkout_failed"}, None

    shutil.rmtree(venv, ignore_errors=True)
    python = specs["python"]
    code, text = run(["uv", "venv", "--seed", "--python", python, str(venv)], log=log)
    if code and tuple(map(int, python.split(".")[:2])) < (3, 8):
        # uv has no CPython older than 3.8 for Windows; the oldest it has is the nearest.
        python = rec["python_substituted"] = "3.8"
        code, text = run(["uv", "venv", "--seed", "--python", python, str(venv)], log=log)
    if code:
        return {**rec, "outcome": "python_unavailable", "detail": text[-300:]}, None
    env = dict(os.environ, VIRTUAL_ENV=str(venv),
               PATH=str(venv / BIN) + os.pathsep + os.environ["PATH"],
               PYTHONUTF8="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    uv_pip = ["uv", "pip", "install", "--python", str(venv / BIN / PY)]

    # Dependencies, as the specs give them.
    pkgs = specs.get("packages", "")
    reqs = []
    try:
        if pkgs == "requirements.txt":
            req_file = work / f"{inst['instance_id']}-requirements.txt"
            req_file.write_text(get_requirements(inst), encoding="utf-8")
            reqs = ["-r", str(req_file)]
        elif pkgs == "environment.yml":
            reqs = yml_deps(get_environment_yml(inst, "testbed"))
        elif pkgs:
            reqs = conda_to_pip(pkgs)
    except Exception as e:  # noqa: BLE001
        return {**rec, "outcome": "requirements_unavailable", "detail": str(e)[:300]}, None
    for group in (reqs, specs.get("pip_packages", [])):
        if group:
            code, text = run(uv_pip + list(group), env=env, log=log)
            if code:
                return {**rec, "outcome": "deps_failed", "detail": text[-600:]}, None
    for step in specs.get("pre_install", []):
        if re.match(r"\s*(apt-get|apt|wget|QHULL|mkdir -p /|tar )", step):
            rec["skipped_steps"].append(step[:120])
            continue
        sh(step + "\n", src, env, 600, log)
    if "install" in specs:
        code, text = sh(specs["install"] + "\n", src, env, 1800, log)
        if code:
            return {**rec, "outcome": "install_failed", "detail": text[-600:]}, None
    rec["setup_seconds"] = round(time.monotonic() - t0)
    return rec, env


def test_script(inst, venv, env, log):
    """The bash script that runs the task's tests (output between START and END)."""
    specs = MAP_REPO_VERSION_TO_SPECS[inst["repo"]][inst["version"]]
    uv_pip = ["uv", "pip", "install", "--python", str(venv / BIN / PY)]
    base_cmd = specs["test_cmd"]
    if base_cmd.startswith("tox --current-env"):
        # tox only wraps pytest here (sphinx), and picks its own Python on Windows.
        base_cmd = "python -X dev -m pytest -rA"
    if "pytest" in base_cmd and not has_module(venv, "pytest"):
        run(uv_pip + ["pytest"], env=env, log=log)
    test_cmd = " ".join([base_cmd, *get_test_directives(inst)])
    return "\n".join([*specs.get("eval_commands", []), f"echo '{START}'", test_cmd, f"echo '{END}'", ""])


def run_tests(inst, src, env, script, log):
    """Runs the task's tests in `src` with its test patch applied (and the test files put
    back after); returns ({test: status}, timed out, output)."""
    test_files = re.findall(r"diff --git a/.* b/(.*)", inst["test_patch"])
    patch_file = src.parent / f"{inst['instance_id']}-test.patch"
    patch_file.write_text(inst["test_patch"], encoding="utf-8", newline="\n")
    run(["git", "-C", str(src), "checkout", "-q", inst["base_commit"], "--", *test_files], log=log)
    code, text = run(["git", "-C", str(src), "apply", "-v", str(patch_file)], log=log)
    if code:
        return None, False, text
    code, out = sh(script, src, env, 1800, log)
    run(["git", "-C", str(src), "checkout", "-q", inst["base_commit"], "--", *test_files], log=log)
    return evaluate(out, MAP_REPO_TO_PARSER[inst["repo"]]), code is None, out


def one(inst, work, log):
    src = work / "repos" / inst["repo"].replace("/", "__")
    if not clone(inst["repo"], src, log):
        return {"instance_id": inst["instance_id"], "repo": inst["repo"], "outcome": "clone_failed"}
    venv = work / "venvs" / inst["instance_id"]
    rec, env = prepare(inst, src, venv, work, log)
    if env is None:
        return rec
    script = test_script(inst, venv, env, log)
    f2p, p2p = json.loads(inst["FAIL_TO_PASS"]), json.loads(inst["PASS_TO_PASS"])
    fix_file = work / "fix.patch"
    fix_file.write_text(inst["patch"], encoding="utf-8", newline="\n")

    t1 = time.monotonic()
    before, before_timeout, before_text = run_tests(inst, src, env, script, log)
    if before is None:
        return {**rec, "outcome": "test_patch_failed", "detail": before_text[-300:]}
    code, text = run(["git", "-C", str(src), "apply", "-v", str(fix_file)], log=log)
    if code:
        return {**rec, "outcome": "fix_failed", "detail": text[-300:]}
    after, after_timeout, after_text = run_tests(inst, src, env, script, log)
    after = after or {}
    rec["test_seconds"] = round(time.monotonic() - t1)

    rec["f2p_passing_after"] = [t for t in f2p if passed(after.get(t, ""))]
    # The check for a fix on Windows: the FAIL_TO_PASS tests that fail without it here (some
    # pass anyway: SWE-bench's lists include tests that fail in its containers for other
    # reasons) and pass with it.
    rec["f2p_windows"] = [t for t in rec["f2p_passing_after"] if not passed(before.get(t, ""))]
    rec["p2p_windows"] = [t for t in p2p if passed(after.get(t, ""))]
    rec["f2p"], rec["p2p_total"] = f2p, len(p2p)
    rec["timed_out"] = before_timeout or after_timeout
    if rec["timed_out"]:
        rec["outcome"] = "tests_timed_out"
    elif not after:
        rec["outcome"] = "tests_did_not_run"
        rec["detail"] = after_text[-600:]
    elif len(rec["f2p_passing_after"]) < len(f2p):
        rec["outcome"] = "fix_does_not_pass"
        rec["detail"] = "; ".join(f"{t}: {after.get(t, 'missing')}" for t in f2p if not passed(after.get(t, "")))[:600]
    elif not rec["f2p_windows"]:
        rec["outcome"] = "passes_without_fix"
    else:
        rec["outcome"] = "works"
    shutil.rmtree(venv, ignore_errors=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--count", type=int, default=8, help="tasks to try, spread over the repo's tasks")
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    tasks = sorted((t for t in load_dataset(a.dataset, split="test") if t["repo"] == a.repo),
                   key=lambda t: t["instance_id"])
    step = max(1, len(tasks) // a.count) if a.count else 1
    chosen = tasks[::step][: a.count] if a.count else tasks
    print(f"{a.repo}: {len(chosen)} of {len(tasks)} tasks", flush=True)
    with open(a.out, "w", encoding="utf-8") as out, open(Path(a.out).with_suffix(".log"), "w", encoding="utf-8") as log:
        for inst in chosen:
            t0 = time.monotonic()
            try:
                rec = one(inst, work, log)
            except Exception as e:  # noqa: BLE001
                rec = {"instance_id": inst["instance_id"], "repo": a.repo, "outcome": "error", "detail": repr(e)[:600]}
            rec["seconds"] = round(time.monotonic() - t0)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"{rec['instance_id']}: {rec['outcome']} ({rec['seconds']} s)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
