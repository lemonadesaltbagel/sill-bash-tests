"""SWE-bench tasks that work on native Windows, as agent-e2e tasks (lab/agent-e2e/run.py
--task-source swebench).

The tasks are those swebench_windows.py found working (works.jsonl). For each: the agent
gets the issue in a clone of the repository at its base commit, with the Python environment
the tests need in `.venv` (first on PATH). The check applies the task's test patch and runs
its tests: the FAIL_TO_PASS tests that flip on Windows, and the PASS_TO_PASS tests that pass
there, must all pass.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swebench_windows as sw  # noqa: E402

PROMPT = """{problem}

---
This directory is a clone of {repo} at the commit this issue was reported against. Its Python
environment, with the dependencies installed, is in .venv, which is already first on PATH
(`python`, `pytest`). Change the code so that the issue is resolved. Don't change the existing
tests."""


def tasks(works_file, dataset, cache, limit=0):
    """agent-e2e tasks for the instances in `works_file` (swebench_windows.py's works.jsonl)."""
    from datasets import load_dataset

    works = [json.loads(line) for line in Path(works_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        works = works[:limit]
    wanted = {w["instance_id"]: w for w in works}
    insts = {t["instance_id"]: t for t in load_dataset(dataset, split="test") if t["instance_id"] in wanted}
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    out = []
    for iid, w in wanted.items():
        if iid in insts:
            out.append(task(insts[iid], w, cache))
    return out


def task(inst, works_rec, cache):
    log_path = cache / f"{inst['instance_id']}.log"
    state = {}

    def setup(work):
        work = Path(work)
        with open(log_path, "a", encoding="utf-8") as log:
            mirror = cache / inst["repo"].replace("/", "__")
            if not sw.clone(inst["repo"], mirror, log):
                raise RuntimeError(f"couldn't clone {inst['repo']}")
            # The work directory is the clone, its environment inside it (ignored by git).
            shutil.rmtree(work, ignore_errors=True)
            sw.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(mirror), str(work)], log=log)
            (work / ".git" / "info").mkdir(parents=True, exist_ok=True)
            with open(work / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
                f.write("\n.venv/\n")
            rec, env = sw.prepare(inst, work, work / ".venv", cache, log)
            if env is None:
                raise RuntimeError(f"setup failed: {rec.get('outcome')} {rec.get('detail', '')[:200]}")
            # As SWE-bench does: what setup changed is committed, so the agent's `git diff`
            # shows only its own changes.
            sw.run(["git", "-C", str(work), "-c", "user.email=setup@swebench", "-c", "user.name=setup",
                    "commit", "-q", "--allow-empty", "-am", "setup"], log=log)
            # And what it left untracked (build/, *.egg-info) is ignored.
            left = subprocess.run(["git", "-C", str(work), "ls-files", "--others", "--exclude-standard", "--directory"],
                                  capture_output=True, text=True).stdout
            with open(work / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
                f.write("".join(f"/{p}\n" for p in left.splitlines()))
            state["env"] = env
            state["script"] = sw.test_script(inst, work / ".venv", env, log)

    def env_for(work, base):
        """The harness's environment `base`, with the task's .venv first on PATH."""
        venv = Path(work) / ".venv"
        return {"VIRTUAL_ENV": str(venv), "PATH": str(venv / sw.BIN) + os.pathsep + base.get("PATH", "")}

    def check(work):
        work = Path(work)
        with open(log_path, "a", encoding="utf-8") as log:
            results, timed_out, out = sw.run_tests(inst, work, state["env"], state["script"], log)
        if results is None:
            return False, "the test patch didn't apply (the agent changed the tests?)"
        if timed_out:
            return False, "the tests timed out"
        need = works_rec["f2p_windows"] + works_rec["p2p_windows"]
        failing = [t for t in need if not sw.passed(results.get(t, ""))]
        f2p_failing = [t for t in works_rec["f2p_windows"] if t in failing]
        if not failing:
            return True, f"{len(need)} tests pass"
        return False, (f"{len(f2p_failing)}/{len(works_rec['f2p_windows'])} target tests and "
                       f"{len(failing) - len(f2p_failing)} regression tests fail: {', '.join(failing[:3])}")

    return {"id": inst["instance_id"], "prompt": PROMPT.format(problem=inst["problem_statement"].strip(), repo=inst["repo"]),
            "setup": setup, "check": check, "env": env_for}
