"""Summarize swebench_windows.py results: outcomes per repository, and the tasks that work.

Usage: python summarize.py OUT.md RESULTS.jsonl [...]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def main():
    out, files = Path(sys.argv[1]), sys.argv[2:]
    recs = [json.loads(l) for f in files for l in Path(f).read_text(encoding="utf-8").splitlines() if l.strip()]
    by_repo = defaultdict(list)
    for r in recs:
        by_repo[r["repo"]].append(r)
    outcomes = sorted({r["outcome"] for r in recs}, key=lambda o: (o != "works", o))
    md = ["# SWE-bench tasks on native Windows", "",
          f"{len(recs)} tasks tried; {sum(r['outcome'] == 'works' for r in recs)} work.", "",
          "| repo | tried | " + " | ".join(outcomes) + " |", "|---|---|" + "---|" * len(outcomes)]
    for repo, rs in sorted(by_repo.items()):
        c = Counter(r["outcome"] for r in rs)
        md.append(f"| {repo} | {len(rs)} | " + " | ".join(str(c.get(o, "")) for o in outcomes) + " |")
    md += ["", "## Tasks that work", "", "| task | python | setup s | tests s | target tests that flip | regression tests kept |", "|---|---|---|---|---|---|"]
    for r in recs:
        if r["outcome"] == "works":
            md.append(f"| {r['instance_id']} | {r.get('python')} | {r.get('setup_seconds')} | {r.get('test_seconds')} | "
                      f"{len(r.get('f2p_windows', []))}/{len(r.get('f2p', []))} | "
                      f"{len(r.get('p2p_windows', []))}/{r.get('p2p_total')} |")
    md += ["", "## Why the others don't", ""]
    for r in recs:
        if r["outcome"] != "works":
            md.append(f"- **{r['instance_id']}** {r['outcome']}: {str(r.get('detail', ''))[:240]}")
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    works = [r for r in recs if r["outcome"] == "works"]
    Path(out.with_name("works.jsonl")).write_text("".join(json.dumps(r) + "\n" for r in works), encoding="utf-8")


if __name__ == "__main__":
    main()
