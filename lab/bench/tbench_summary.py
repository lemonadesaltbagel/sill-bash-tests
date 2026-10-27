"""Summarize tbench_windows.py results: what ports to Windows, and each shell's reference
solution results.

Usage: python tbench_summary.py OUT.md RESULTS.jsonl [...]
"""
import json
import sys
from collections import Counter
from pathlib import Path


def main():
    out, files = Path(sys.argv[1]), sys.argv[2:]
    recs = [json.loads(l) for f in files for l in Path(f).read_text(encoding="utf-8").splitlines() if l.strip()]
    recs.sort(key=lambda r: r["task"])
    shells = sorted({s for r in recs for s in r.get("shells", {})}, key=lambda s: (s != "sill", s))
    c = Counter(r["outcome"] for r in recs)
    ran = [r for r in recs if r.get("shells")]
    md = ["# Terminal-Bench 2.1 on native Windows (reference solutions)", "",
          f"{len(recs)} tasks: " + ", ".join(f"{v} {k}" for k, v in c.most_common()) + ".", "",
          "| reference solution passes | " + " | ".join(shells) + " |", "|---|" + "---|" * len(shells),
          "| of the tasks whose environment builds (" + str(len(ran)) + ") | " +
          " | ".join(str(sum(r["shells"].get(s, {}).get("reward") == 1 for r in ran)) for s in shells) + " |", "",
          "| task | category | " + " | ".join(shells) + " |", "|---|---|" + "---|" * len(shells)]
    for r in ran:
        cells = []
        for s in shells:
            x = r["shells"].get(s, {})
            cells.append("✅" if x.get("reward") == 1 else ("–" if x.get("reward") is None else "❌"))
        md.append(f"| {r['task']} | {r.get('category')} | " + " | ".join(cells) + " |")
    md += ["", "## Where only some shells pass", ""]
    for r in ran:
        got = {s: r["shells"].get(s, {}).get("reward") == 1 for s in shells}
        if any(got.values()) and not all(got.values()):
            for s in shells:
                if not got[s]:
                    x = r["shells"][s]
                    md.append(f"- **{r['task']}** fails in {s} (solve exit {x.get('solve_exit')}): "
                              f"`{x.get('solve_tail', '')[-300:].strip()}`".replace("\n", " ⏎ "))
    md += ["", "## Not portable or not run", ""]
    for r in recs:
        if not r.get("shells"):
            md.append(f"- **{r['task']}** {r['outcome']}: {str(r.get('detail', ''))[:200]}")
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    works = [r for r in ran if any(x.get("reward") == 1 for x in r["shells"].values())]
    out.with_name("tbench-works.jsonl").write_text("".join(json.dumps(r) + "\n" for r in works), encoding="utf-8")


if __name__ == "__main__":
    main()
