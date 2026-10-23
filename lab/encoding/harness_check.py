"""What does the model see? Runs real Claude Code (as lab/agent-e2e does) on sill and asks
it to run given commands verbatim; prints each Bash tool result exactly as Claude Code
handed it to the model.

Usage (on Windows, DEEPSEEK_API_KEY set):
  python harness_check.py --sill-bash dist\\sill\\bash.exe "cmd.exe /d /c nosuchcmd_xyz" ...
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "agent-e2e"))
from run import claude_env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sill-bash", required=True)
    ap.add_argument("--env", default="sill")
    ap.add_argument("--file", help="commands, one per line (no shell quoting to survive)")
    ap.add_argument("--out", help="also write the report here (UTF-8)")
    ap.add_argument("--cwd", help="run Claude Code in this directory (default: a new temp dir)")
    ap.add_argument("--to-files", action="store_true",
                    help="Claude Code's stdout/stderr to files, as lab/agent-e2e/run.py does")
    ap.add_argument("commands", nargs="*")
    a = ap.parse_args()
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            a.commands += [line.rstrip("\n") for line in f if line.strip()]
    env = claude_env(a.env, a.sill_bash)
    claude = shutil.which("claude", path=env.get("PATH"))
    work = a.cwd or tempfile.mkdtemp(prefix="hc-")
    if a.out:
        # Through a pipe, the report would be decoded again by whoever reads it.
        sys.stdout = open(a.out, "w", encoding="utf-8")
    for cmd in a.commands:
        prompt = ("Use the Bash tool exactly once to run this command, unchanged, then stop:\n\n"
                  + cmd + "\n")
        argv = [claude, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "bypassPermissions", "--max-turns", "3", "--model", "sonnet"]
        if a.to_files:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                subprocess.run(argv, input=prompt.encode("utf-8"), stdout=out, stderr=err,
                               cwd=work, env=env, timeout=300)
                out.seek(0)
                p = subprocess.CompletedProcess(argv, 0, out.read(), b"")
        else:
            p = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True, cwd=work,
                               env=env, timeout=300)
        # The key is in the agent's environment; it never goes into a report.
        p = subprocess.CompletedProcess(p.args, p.returncode,
                                        p.stdout.replace(env["ANTHROPIC_API_KEY"].encode(), b"sk-REDACTED"), b"")
        print(f"### {cmd}")
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            msg = ev.get("message") or {}
            for c in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
                if c.get("type") == "tool_use":
                    print("command:", c["input"].get("command"))
                if c.get("type") == "tool_result":
                    content = c.get("content")
                    if isinstance(content, list):
                        content = "".join(x.get("text", "") for x in content)
                    print("result :", ascii(content)[:400])
                    print("display:", str(content)[:300])


if __name__ == "__main__":
    main()
