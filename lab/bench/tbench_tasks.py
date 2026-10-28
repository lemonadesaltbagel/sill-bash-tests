"""Terminal-Bench tasks that work on native Windows, as agent-e2e tasks (lab/agent-e2e/run.py
--task-source tbench).

The tasks are those tbench_windows.py found working (a reference solution passes in at least
one shell: tbench-works.jsonl). For each: the environment is built as tbench_windows.py
builds it, in the run's own folder; the agent starts in the task's working directory with
the task's instruction (its paths in their Windows form); the task's own verifier decides.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tbench_windows as tw  # noqa: E402

NOTE = ("\n\n---\nThis is a Windows machine; the task's directories are under {root} "
        "(you're in {workdir}).")


def tasks(works_file, tasks_dir):
    names = [json.loads(l)["task"] for l in Path(works_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    return [task(tw.Task(Path(tasks_dir) / n)) for n in names]


def task(t):
    state = {}

    def setup(work):
        root = Path(work)
        with open(root.parent / f"{t.name}-env.log", "a", encoding="utf-8") as log:
            _, choco = t.needs()
            tw.provision(choco, log)
            env, workdir = tw.environment(t, root, log)
        state.update(env=env, workdir=workdir, root=root)
        # What the agent mustn't see: the reference solution and the tests. Neither stays on
        # disk; the check copies the tests in afresh, as Terminal-Bench does after the agent.
        import shutil
        shutil.rmtree(root / "solution", ignore_errors=True)
        shutil.rmtree(root / "tests", ignore_errors=True)

    def cwd(work):
        return str(state["workdir"])

    def env_for(work, base):
        env = state["env"]
        keep = ("VIRTUAL_ENV", "PYTHONUTF8", "PIP_DISABLE_PIP_VERSION_CHECK",
                "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")
        extra = {k: env[k] for k in keep if k in env}
        # The environment's PATH entries (the venv, MinGW) before the harness's own.
        added = [p for p in env["PATH"].split(";") if p and p not in base.get("PATH", "")]
        extra["PATH"] = ";".join(added + [base.get("PATH", "")])
        return extra

    def check(work):
        import shutil
        root = state["root"]
        shutil.rmtree(root / "tests", ignore_errors=True)
        tw.copy_tree(t.path / "tests", root / "tests", root.as_posix())
        with open(root.parent / f"{t.name}-env.log", "a", encoding="utf-8") as log:
            reward, tail = tw.verify(t, root, state["workdir"], state["env"], log)
        tw.end_processes_under(root)
        return reward == 1, f"reward {reward}: {tail[-300:]}"

    instruction = tw.rewrite(t.instruction, "{root}")
    return {"id": t.name, "prompt_fn": lambda work: (
                tw.rewrite(t.instruction, Path(work).as_posix())
                + NOTE.format(root=Path(work).as_posix(), workdir=Path(state["workdir"]).as_posix())),
            "prompt": instruction, "setup": setup, "check": check, "env": env_for, "cwd": cwd,
            "timeout": t.timeout("agent", 900)}
