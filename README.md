# sill-bash-tests

The public test suite for **sill**, a native Windows Bash runtime for coding agents, shipped as a drop-in `bash.exe`. sill's source is private. This repository holds the tests and runs them on fresh `windows-2025` runners against sill's released build, side by side with Git Bash.

| | What | Where |
|---|---|---|
| Conformance | What agents rely on from a Bash on Windows: arguments to native programs, paths, encoding, line endings, processes, size limits, `#!` scripts, tools, errors. Each case says why it matters | `conformance/` (cases, runner, README) |
| Distribution checks | The packaged build on a machine with only Windows' own directories on PATH | `scripts/dist-check.sh` |
| Smoke probes | Pipes, here-docs, quoting, native argv, identity | `lab/brush-smoke/smoke.py` |
| Agent end-to-end | Real Claude Code, opencode or Codex (through a local LiteLLM proxy) with DeepSeek's cheap model on 18 tasks, per shell | `lab/agent-e2e/` (manual workflow) |

`echoargs` (`lab/argv-live/`) is the helper that shows what a native program actually received.

## CI

- `conformance`: runs on every push and weekly against the latest release, and on demand with a tag. The tag `nightly` is a prerelease build of sill's current source, published with a run against it. Results are in the run summary and the `results` artifact.
- `agent-e2e`: runs on demand. Choose `claude`, `opencode` or `codex`.

Both download sill's release with the `SILL_DOWNLOAD_TOKEN` secret. Workflows triggered from forks don't receive it, so they can't run against sill.

## Run locally (Windows)

    python conformance\run.py --shell sill=C:\path\to\sill\bash.exe ^
        --shell gitbash="C:\Program Files\Git\bin\bash.exe" ^
        --echoargs echoargs.exe --out results.json --report report.md
