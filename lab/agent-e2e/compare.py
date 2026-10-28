"""Merge agent-e2e results from several environments into one comparison.

Usage: python compare.py OUT.md DIR [DIR ...]   (each DIR holds a results.json)
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


# The same as run.py's (kept in step with it).
COUNTED = ("passed", "failed", "timed_out")


def outcome_of(rec):
    if rec.get("outcome") == "setup_failed":
        return "setup_failed"
    if rec.get("api_error") or str(rec.get("result_text", "")).startswith("API Error"):
        return "api_error"
    if rec.get("passed"):
        return "passed"
    return "timed_out" if rec.get("timed_out") else "failed"


def main():
    out, dirs = Path(sys.argv[1]), [Path(d) for d in sys.argv[2:]]
    records = []
    for d in dirs:
        f = d / "results.json"
        if f.exists():
            records += json.loads(f.read_text(encoding="utf-8"))
    # Why each run ended (run.py's outcome_of); runs the model's API failed (no credit, an
    # outage) or whose task didn't set up say nothing about the shell and aren't counted.
    for r in records:
        r["outcome"] = outcome_of(r)
    dropped = [r for r in records if r["outcome"] not in COUNTED]
    records = [r for r in records if r["outcome"] in COUNTED]
    envs = sorted({r["env"] for r in records}, key=["sill", "sill-mcp", "gitbash", "powershell"].index)
    tasks = list(dict.fromkeys(r["task"] for r in records))

    by = defaultdict(list)
    for r in records:
        by[(r["task"], r["env"])].append(r)

    def cell(rs):
        if not rs:
            return "–"
        p = sum(r["passed"] for r in rs)
        calls = sum(len(r["shell_calls"]) for r in rs)
        err = sum(c["is_error"] for r in rs for c in r["shell_calls"])
        return f"{'✅' if p == len(rs) else ('❌' if p == 0 else '◐')} {p}/{len(rs)} · {calls} calls, {err} err"

    harness = {"claude": "Claude Code", "opencode": "opencode", "codex": "Codex CLI (via LiteLLM)",
               "gemini": "Gemini CLI (via LiteLLM)"}.get(
        next((r.get("harness") for r in records if r.get("harness")), "claude"), "?")
    md = [f"# Agent end-to-end: {harness} + DeepSeek (cheap model) on Windows", "",
          "Each cell is tasks passed/runs, then shell tool calls and how many errored."
          + (" Not counted: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in
                                           sorted(Counter(r["outcome"] for r in dropped).items())) + "."
             if dropped else ""), "",
          "| task | " + " | ".join(envs) + " |", "|---" * (len(envs) + 1) + "|"]
    for t in tasks:
        md.append(f"| {t} | " + " | ".join(cell(by[(t, e)]) for e in envs) + " |")

    md += ["", "| totals | " + " | ".join(envs) + " |", "|---" * (len(envs) + 1) + "|"]
    rows = {
        "tasks passed": lambda rs: f"{sum(r['passed'] for r in rs)}/{len(rs)}",
        "shell calls": lambda rs: str(sum(len(r['shell_calls']) for r in rs)),
        "errored calls": lambda rs: (lambda c, e: f"{e} ({100 * e / c:.0f}%)" if c else "0")(
            sum(len(r['shell_calls']) for r in rs), sum(c['is_error'] for r in rs for c in r['shell_calls'])),
        "turns": lambda rs: str(sum(r['num_turns'] or 0 for r in rs)),
        "input tokens": lambda rs: f"{sum(r['input_tokens'] for r in rs):,}",
        "output tokens": lambda rs: f"{sum(r['output_tokens'] for r in rs):,}",
        "wall time (s)": lambda rs: f"{sum(r['seconds'] for r in rs):.0f}",
    }
    for label, f in rows.items():
        md.append(f"| {label} | " + " | ".join(f([r for r in records if r['env'] == e]) for e in envs) + " |")

    md += ["", "## Failed tasks", ""]
    for r in records:
        if not r["passed"]:
            md.append(f"- **{r['env']} / {r['task']}** (r{r['rep']}, {r['outcome'].replace('_', ' ')}): {r['detail'][:300]}")

    md += ["", "## Shell calls that errored", ""]
    for e in envs:
        errs = [(r["task"], c) for r in records if r["env"] == e for c in r["shell_calls"] if c["is_error"]]
        md.append(f"### {e} ({len(errs)})")
        md.append("")
        for task, c in errs:
            cmd = c["command"].replace("\n", " ⏎ ")[:220]
            outp = c["output"].replace("\n", " ⏎ ")[:220]
            md.append(f"- `{task}` `{cmd}` → {outp}")
        md.append("")
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:40]))


if __name__ == "__main__":
    main()
