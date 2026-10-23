# Conformance: what coding agents rely on from a Bash on Windows

`cases.py` lists 56 cases in nine categories:
- arguments to native programs;
- paths;
- encoding;
- line endings;
- processes;
- size limits;
- running scripts;
- tools;
- errors and identity.

Each case is a bash script with an expectation. That is what bash does on Linux (the behaviour agents are trained on) or, where Windows forces a choice, what lets the agent's next command work. Every case says why it matters.

`run.py` runs each case in a fresh directory, under each shell, the way an agent harness runs commands:
- `<shell> -c <script>`;
- started detached, with no console, as Claude Code starts its shell;
- stdin from NUL;
- output piped.

## Run

On Windows, with `echoargs.exe` (`lab/argv-live/build-echoargs.cmd`):

    python conformance\run.py --shell sill=C:\...\sill\bash.exe ^
        --shell gitbash="C:\Program Files\Git\bin\bash.exe" ^
        --echoargs echoargs.exe --out results.json --report report.md

Any other `bash.exe` can be added as another `--shell NAME=PATH`.

On the lab, `scripts/lab-compat.ps1 -Shell <bash.exe> -Name <name>` adds the Bash-language measure: the brush compat suite with Git Bash as the reference.

## Results

Dated runs are in `results/`: the machine, its code page and each shell's version are in each report.

## Rules

- **Expectations come from Linux bash or the agent's next step,** never from what sill does. A case where shells reasonably differ is left out (for example, the form `pwd` prints).
- **One root cause, one case.** Cases outside `argv` that start cmd.exe set `MSYS_NO_PATHCONV=1`, so Git Bash's conversion of `/d /c` into paths is counted once (`cmd-c-string`), not in every category.
- **Results depend on the machine.**
  - The system language changes the encoding cases. On a code page 1252 machine, Git Bash passes more of them.
  - Installed programs (python, git) gate the cases that need them; a case without its program is skipped.
- **The cases grew out of failures sill was fixed for, so sill's boundary score is partly by construction.** The Bash-language measure is the counterweight, and there sill is not perfect. Cases written by others are welcome.
