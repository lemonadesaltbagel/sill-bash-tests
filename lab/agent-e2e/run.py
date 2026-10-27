"""Run real Claude Code (headless) on lab/agent-e2e tasks in one shell environment.

Environments:
  sill        Claude Code's Bash tool on sill's bash.exe (CLAUDE_CODE_GIT_BASH_PATH)
  gitbash     Claude Code's Bash tool on Git for Windows' bash.exe
  powershell  Claude Code's PowerShell tool only (Bash disallowed)

The model is reached through DeepSeek's Anthropic-compatible API: claude-sonnet*/haiku*
names map to its cheap model. Needs DEEPSEEK_API_KEY.

For each task: fresh directory, fixture, `claude -p`, check. Writes the transcript, a
per-task record (pass/fail, every shell command with its error flag, turns, tokens,
time) to OUT/results.json, and OUT/summary.md.

Usage: python run.py --env sill --sill-bash dist/sill/bash.exe --out out/sill [--tasks a,b] [--reps 1]
       [--harness claude|opencode|codex|gemini]   (opencode: its shell via SHELL; DeepSeek via
       DEEPSEEK_API_KEY; codex and gemini through the local LiteLLM proxy)
  sill-mcp    (codex, gemini) the harness's own shell (PowerShell) plus sill's MCP server
              (`bash.exe --mcp`), with no instructions beyond the server's own
"""
import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks import TASKS  # noqa: E402

GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
SHELL_TOOLS = {"Bash", "PowerShell"}


def claude_env(env_name, sill_bash):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("DEEPSEEK_API_KEY is not set")
    env = dict(os.environ)
    for k in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_USE_POWERSHELL_TOOL"):
        env.pop(k, None)
    env.update({
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_API_KEY": key,
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # The runner has no git identity; that cost every environment a failed commit.
        "GIT_AUTHOR_NAME": "sill e2e", "GIT_AUTHOR_EMAIL": "e2e@sill.invalid",
        "GIT_COMMITTER_NAME": "sill e2e", "GIT_COMMITTER_EMAIL": "e2e@sill.invalid",
    })
    if env_name == "sill":
        env["CLAUDE_CODE_GIT_BASH_PATH"] = str(Path(sill_bash).resolve())
        env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] = "0"
    elif env_name == "gitbash":
        env["CLAUDE_CODE_GIT_BASH_PATH"] = GIT_BASH
        env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] = "0"
    elif env_name == "powershell":
        env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] = "1"
    else:
        sys.exit(f"unknown env {env_name}")
    return env


def opencode_env(env_name, sill_bash):
    """opencode picks its shell from `SHELL` (else pwsh, powershell, Git Bash, ComSpec) and
    runs `<shell> -c <command>`; DeepSeek is one of its built-in providers."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("DEEPSEEK_API_KEY is not set")
    env = dict(os.environ)
    env.pop("SHELL", None)
    env.update({
        "GIT_AUTHOR_NAME": "sill e2e", "GIT_AUTHOR_EMAIL": "e2e@sill.invalid",
        "GIT_COMMITTER_NAME": "sill e2e", "GIT_COMMITTER_EMAIL": "e2e@sill.invalid",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    })
    if env_name == "sill":
        env["SHELL"] = str(Path(sill_bash).resolve())
    elif env_name == "gitbash":
        env["SHELL"] = GIT_BASH
    return env


CODEX_CONFIG = """model = "{model}"
model_provider = "litellm"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.litellm]
name = "LiteLLM (DeepSeek)"
base_url = "{base_url}"
env_key = "LITELLM_KEY"
wire_api = "responses"
"""
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "sill e2e", "GIT_AUTHOR_EMAIL": "e2e@sill.invalid",
                "GIT_COMMITTER_NAME": "sill e2e", "GIT_COMMITTER_EMAIL": "e2e@sill.invalid"}
CODEX_MCP = """
[mcp_servers.sill]
command = "{exe}"
args = ["--mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 660
default_tools_approval_mode = "approve"
# Codex starts MCP servers with a few variables only; the tasks' git identity is passed on.
env = { GIT_AUTHOR_NAME = "sill e2e", GIT_AUTHOR_EMAIL = "e2e@sill.invalid", GIT_COMMITTER_NAME = "sill e2e", GIT_COMMITTER_EMAIL = "e2e@sill.invalid" }
"""
CODEX_BASH_AGENTS = ("# Shell\n\nThis machine's shell is bash. Run every shell command with bash: pass "
                     "`shell: \"bash\"` to the command tool. Write bash, not PowerShell.\n")


def codex_env(env_name, sill_bash, out_dir, model):
    """Codex speaks only OpenAI's Responses API; a local LiteLLM proxy (CODEX_PROXY_URL,
    default http://127.0.0.1:4000/v1) translates for DeepSeek. Codex has no shell setting
    on Windows (always PowerShell) but runs bash when the model passes `shell: "bash"`,
    resolved on PATH; a global AGENTS.md in an isolated CODEX_HOME asks for that."""
    env = dict(os.environ)
    home = Path(out_dir).resolve() / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(CODEX_CONFIG.format(
        model=OPENCODE_MODELS.get(model, model).split("/")[-1],
        base_url=os.environ.get("CODEX_PROXY_URL", "http://127.0.0.1:4000/v1")), encoding="utf-8")
    if env_name == "sill-mcp":
        # Codex as it comes (PowerShell), plus sill's MCP server: no AGENTS.md, so whether
        # the model reaches for sill's bash tool is up to the server's own instructions.
        with open(home / "config.toml", "a", encoding="utf-8") as f:
            f.write(CODEX_MCP.format(exe=str(Path(sill_bash).resolve()).replace("\\", "\\\\")))
    agents = home / "AGENTS.md"
    if env_name in ("sill", "gitbash"):
        agents.write_text(CODEX_BASH_AGENTS, encoding="utf-8")
        first = str(Path(sill_bash).resolve().parent) if env_name == "sill" else str(Path(GIT_BASH).parent)
        env["PATH"] = first + os.pathsep + env.get("PATH", "")
    elif agents.exists():
        agents.unlink()
    env.update({
        "CODEX_HOME": str(home),
        "LITELLM_KEY": os.environ.get("LITELLM_KEY", "sk-local-sill-proxy"),
        # A Windows system proxy can answer for the local LiteLLM (502s); localhost bypasses it.
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "GIT_AUTHOR_NAME": "sill e2e", "GIT_AUTHOR_EMAIL": "e2e@sill.invalid",
        "GIT_COMMITTER_NAME": "sill e2e", "GIT_COMMITTER_EMAIL": "e2e@sill.invalid",
    })
    return env


def run_codex(exe, prompt, cwd, env, timeout, transcript, stderr_path):
    argv = [exe, "exec", "--json", "--skip-git-repo-check", "-"]
    t0 = time.monotonic()
    with open(transcript, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            timed_out = True
    return proc.returncode, timed_out, time.monotonic() - t0


def parse_codex(path):
    tools_used, shell, items, tin, tout, final_text = {}, [], 0, 0, 0, ""
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "item.completed":
            item = ev.get("item") or {}
            kind = item.get("type", "?")
            if kind == "error":
                continue
            items += 1
            tools_used[kind] = tools_used.get(kind, 0) + 1
            if kind == "command_execution":
                shell.append({"tool": "shell", "command": item.get("command", ""),
                              "is_error": item.get("exit_code") not in (0, None),
                              "output": str(item.get("aggregated_output", ""))[:2000]})
            elif kind == "mcp_tool_call":
                # sill's MCP tools: `bash` results end with "--- exit N ..." or "--- stopped".
                result = json.dumps(item.get("result") or "", ensure_ascii=False)
                exit_code = re.search(r"--- exit (-?\d+)", result)
                args = item.get("arguments") or {}
                failed = (item.get("status") == "failed" or item.get("error") is not None
                          or (exit_code is not None and exit_code.group(1) != "0")
                          or "--- stopped after" in result)
                shell.append({"tool": f"mcp:{item.get('tool', '?')}",
                              "command": args.get("command", json.dumps(args)) if isinstance(args, dict) else str(args),
                              "is_error": failed, "output": result[:2000]})
            elif kind == "agent_message":
                final_text = item.get("text", "")
        elif ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
    return {"tools_used": tools_used, "shell_calls": shell, "num_turns": items, "result_subtype": None,
            "result_is_error": None, "result_text": final_text[:1500], "input_tokens": tin,
            "output_tokens": tout, "cost_usd_reported": None}


def gemini_env(env_name, sill_bash, out_dir):
    """Gemini CLI against the local LiteLLM proxy (GOOGLE_GEMINI_BASE_URL), which routes its
    Gemini model names to DeepSeek. Its settings come from a profile of its own (USERPROFILE
    and HOME point there, for every environment alike: Gemini CLI ignores a system settings
    file in a user-writable folder), with folder trust off and sill's MCP server added for
    sill-mcp. Its shell tool on Windows is PowerShell."""
    env = dict(os.environ)
    home = Path(out_dir).resolve() / "gemini-home"
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    settings = {
        "security": {"auth": {"selectedType": "gemini-api-key"}, "folderTrust": {"enabled": False}},
        "general": {"disableAutoUpdate": True, "disableUpdateNag": True},
        "privacy": {"usageStatisticsEnabled": False},
    }
    if env_name == "sill-mcp":
        settings["mcpServers"] = {"sill": {"command": str(Path(sill_bash).resolve()), "args": ["--mcp"],
                                           "timeout": 660000, "trust": True,
                                           "env": dict(GIT_IDENTITY)}}
    (home / ".gemini" / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    env.update({
        "USERPROFILE": str(home), "HOME": str(home),
        "GOOGLE_GEMINI_BASE_URL": os.environ.get("GEMINI_PROXY_URL", "http://127.0.0.1:4000"),
        "GEMINI_API_KEY": os.environ.get("LITELLM_KEY", "sk-local-sill-proxy"),
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "GIT_AUTHOR_NAME": "sill e2e", "GIT_AUTHOR_EMAIL": "e2e@sill.invalid",
        "GIT_COMMITTER_NAME": "sill e2e", "GIT_COMMITTER_EMAIL": "e2e@sill.invalid",
    })
    return env


def find_gemini():
    shim = shutil.which("gemini")
    if not shim:
        return None
    # Node with the bundle, not the npm .cmd shim (cmd.exe would mangle arguments).
    js = Path(shim).parent / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
    return [shutil.which("node") or "node", str(js)] if js.is_file() else [shim]


def run_gemini(argv0, prompt, cwd, env, timeout, transcript, stderr_path):
    # The prompt over stdin; -p (empty) makes it headless and is appended to it. A model name
    # Gemini CLI sends no thinking settings for: LiteLLM would turn them into DeepSeek's
    # thinking mode, whose reasoning the Gemini format can't carry back (400 on turn two).
    argv = [*argv0, "-p", "", "--yolo", "-o", "stream-json", "-m", "gemini-2.0-flash"]
    t0 = time.monotonic()
    with open(transcript, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            timed_out = True
    return proc.returncode, timed_out, time.monotonic() - t0


def parse_gemini(path):
    """Gemini CLI's stream-json: tool_use and tool_result events, assistant messages, and a
    final result with token stats."""
    tools_used, shell, calls, turns, tin, tout, final_text = {}, [], {}, 0, 0, 0, ""
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "tool_use":
            name = ev.get("tool_name", "?")
            tools_used[name] = tools_used.get(name, 0) + 1
            calls[ev.get("tool_id")] = (name, ev.get("parameters") or {})
            turns += 1
        elif kind == "tool_result":
            name, params = calls.get(ev.get("tool_id"), ("?", {}))
            if "shell" in name or "bash" in name:
                out = json.dumps(ev.get("output") or ev.get("error") or "", ensure_ascii=False)
                exit_code = re.search(r"--- exit (-?\d+)|Exit Code: (-?\d+)", out)
                code = next((g for g in exit_code.groups() if g), "0") if exit_code else "0"
                shell.append({"tool": name, "command": params.get("command", json.dumps(params)),
                              "is_error": ev.get("status") != "success" or code != "0" or "--- stopped after" in out,
                              "output": out[:2000]})
        elif kind == "message" and ev.get("role") == "assistant":
            final_text = (final_text + ev.get("content", "")) if ev.get("delta") else ev.get("content", "")
        elif kind == "result":
            stats = ev.get("stats") or {}
            tin = stats.get("input_tokens", 0)
            tout = stats.get("output_tokens", 0)
    return {"tools_used": tools_used, "shell_calls": shell, "num_turns": turns, "result_subtype": None,
            "result_is_error": None, "result_text": final_text[:1500], "input_tokens": tin,
            "output_tokens": tout, "cost_usd_reported": None}


OPENCODE_MODELS = {"sonnet": "deepseek/deepseek-flash", "haiku": "deepseek/deepseek-flash",
                   "opus": "deepseek/deepseek-v4-pro"}


def find_opencode():
    shim = shutil.which("opencode")
    if not shim:
        return None
    # Run the native binary, not the npm .cmd shim (cmd.exe would mangle the prompt).
    exe = Path(shim).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    return str(exe) if exe.is_file() else shim


def run_opencode(exe, prompt, cwd, env, model, timeout, transcript, stderr_path):
    # The prompt goes over stdin. In argv, opencode.exe (Bun) decodes `\\\"` as `\\\\"`: the
    # quote-stress task arrived with every `\"` doubled. (An inherited stdin fails with
    # "EUNKNOWN ... read"; a pipe of our own works.)
    argv = [exe, "run", "--format", "json", "--auto", "-m", OPENCODE_MODELS.get(model, model)]
    t0 = time.monotonic()
    with open(transcript, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            timed_out = True
    return proc.returncode, timed_out, time.monotonic() - t0


def parse_opencode(path):
    tools_used, shell, turns, tin, tout, final_text = {}, [], 0, 0, 0, ""
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") or {}
        if ev.get("type") == "step_start":
            turns += 1
        elif ev.get("type") == "tool_use":
            name = part.get("tool", "?")
            tools_used[name] = tools_used.get(name, 0) + 1
            if name == "bash":
                state = part.get("state") or {}
                meta = state.get("metadata") or {}
                is_error = state.get("status") == "error" or meta.get("exit", 0) not in (0, None)
                shell.append({"tool": "bash", "command": (state.get("input") or {}).get("command", ""),
                              "is_error": bool(is_error), "output": str(state.get("output", ""))[:2000]})
        elif ev.get("type") == "step_finish":
            tok = part.get("tokens") or {}
            tin += tok.get("input", 0) + (tok.get("cache") or {}).get("read", 0)
            tout += tok.get("output", 0)
        elif ev.get("type") == "text":
            final_text = part.get("text", "")
    return {"tools_used": tools_used, "shell_calls": shell, "num_turns": turns, "result_subtype": None,
            "result_is_error": None, "result_text": final_text[:1500], "input_tokens": tin,
            "output_tokens": tout, "cost_usd_reported": None}


def redact_key(paths, key):
    """The key is in every agent's environment (that's how Claude Code reaches DeepSeek), so
    a model that runs `env` puts it in the transcript. Transcripts get uploaded and read;
    the key never stays in one."""
    if not key:
        return
    for path in paths:
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if key.encode() in data:
            Path(path).write_bytes(data.replace(key.encode(), b"sk-REDACTED"))


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def run_claude(claude, prompt, cwd, env, env_name, model, max_turns, timeout, transcript, stderr_path):
    # The prompt goes over stdin, not argv: `claude` is claude.cmd, and cmd.exe cut a
    # prompt containing newlines, quotes, & and % (the quote-stress task) at the first
    # newline, dropping the flags after it too.
    argv = [claude, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions", "--max-turns", str(max_turns), "--model", model]
    if env_name == "powershell":
        argv += ["--disallowedTools", "Bash"]
    t0 = time.monotonic()
    with open(transcript, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            timed_out = True
    return proc.returncode, timed_out, time.monotonic() - t0


def parse_transcript(path):
    calls, results, final = {}, {}, None
    order = []
    tools_used = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        if ev.get("type") == "assistant":
            for item in msg.get("content") or []:
                if item.get("type") == "tool_use":
                    name = item.get("name")
                    tools_used[name] = tools_used.get(name, 0) + 1
                    calls[item["id"]] = {"tool": name, "input": item.get("input")}
                    order.append(item["id"])
        elif ev.get("type") == "user":
            content = msg.get("content")
            for item in content if isinstance(content, list) else []:
                if item.get("type") == "tool_result":
                    c = item.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    results[item.get("tool_use_id")] = {"is_error": bool(item.get("is_error")), "output": str(c)[:2000]}
        elif ev.get("type") == "result":
            final = ev
    shell = []
    for cid in order:
        call = calls[cid]
        if call["tool"] in SHELL_TOOLS:
            inp = call["input"] or {}
            res = results.get(cid, {})
            shell.append({"tool": call["tool"], "command": inp.get("command", ""),
                          "is_error": res.get("is_error", False), "output": res.get("output", "")})
    usage = (final or {}).get("usage") or {}
    return {
        "tools_used": tools_used,
        "shell_calls": shell,
        "num_turns": (final or {}).get("num_turns"),
        "result_subtype": (final or {}).get("subtype"),
        "result_is_error": (final or {}).get("is_error"),
        "result_text": str((final or {}).get("result", ""))[:1500],
        "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd_reported": (final or {}).get("total_cost_usd"),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["sill", "gitbash", "powershell", "sill-mcp"])
    ap.add_argument("--harness", default="claude", choices=["claude", "opencode", "codex", "gemini"])
    ap.add_argument("--sill-bash", default="")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--task-source", default="builtin", choices=["builtin", "swebench", "tbench"],
                    help="swebench, tbench: the SWE-bench or Terminal-Bench tasks that work on Windows (lab/bench)")
    ap.add_argument("--tbench-works", default="lab/bench/tbench-works.jsonl")
    ap.add_argument("--tbench-tasks", default="tb/tasks", help="a Terminal-Bench 2.1 checkout's tasks folder")
    ap.add_argument("--swebench-works", default="lab/bench/works-verified.jsonl")
    ap.add_argument("--swebench-dataset", default="princeton-nlp/SWE-bench_Verified")
    ap.add_argument("--swebench-cache", default="", help="where repositories are cloned once")
    ap.add_argument("--shard", default="", help="I/N: run every N-th task, starting with the I-th (1-based)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()

    if a.harness == "claude":
        claude = shutil.which("claude")
        if not claude:
            sys.exit("claude not found on PATH (npm install -g @anthropic-ai/claude-code)")
        env = claude_env(a.env, a.sill_bash)
        key = env["ANTHROPIC_API_KEY"]
    elif a.harness == "opencode":
        opencode = find_opencode()
        if not opencode:
            sys.exit("opencode not found on PATH (npm install -g opencode-ai)")
        env = opencode_env(a.env, a.sill_bash)
        key = env["DEEPSEEK_API_KEY"]
    elif a.harness == "gemini":
        gemini = find_gemini()
        if not gemini:
            sys.exit("gemini not found on PATH (npm install -g @google/gemini-cli)")
        env = gemini_env(a.env, a.sill_bash, a.out)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
    else:
        codex = shutil.which("codex")
        if not codex:
            sys.exit("codex not found on PATH (npm install -g @openai/codex)")
        env = codex_env(a.env, a.sill_bash, a.out, a.model)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
    wanted = set(filter(None, a.tasks.split(",")))
    if a.task_source == "swebench":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
        import swebench_tasks
        cache = a.swebench_cache or str(a.out.resolve().parent / "swebench-cache")
        source = swebench_tasks.tasks(a.swebench_works, a.swebench_dataset, cache)
    elif a.task_source == "tbench":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
        import tbench_tasks
        source = tbench_tasks.tasks(a.tbench_works, a.tbench_tasks)
    else:
        source = TASKS
    tasks = [t for t in source if not wanted or t["id"] in wanted]
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        tasks = tasks[i - 1::n]

    a.out.mkdir(parents=True, exist_ok=True)
    records = []
    for rep in range(a.reps):
        for task in tasks:
            name = f"{task['id']}-r{rep}"
            work = (a.out / "work" / name).resolve()
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            try:
                task["setup"](str(work))
            except Exception as e:  # a task that can't be set up isn't the agent's failure
                print(f"[{a.env}] {name}: SETUP FAILED {e!r}"[:300], flush=True)
                continue
            task_env = {**env, **task["env"](str(work), env)} if task.get("env") else env
            # A task may name its own working directory, prompt (with this run's paths) and
            # time limit.
            run_dir = Path(task["cwd"](str(work))) if task.get("cwd") else work
            prompt = task["prompt_fn"](str(work)) if task.get("prompt_fn") else task["prompt"]
            limit = int(task.get("timeout") or a.timeout)
            transcript = a.out / f"{name}.jsonl"
            if a.harness == "claude":
                code, timed_out, secs = run_claude(
                    claude, prompt, run_dir, task_env, a.env, a.model, a.max_turns, limit,
                    transcript, a.out / f"{name}.stderr.txt")
            elif a.harness == "opencode":
                code, timed_out, secs = run_opencode(
                    opencode, prompt, run_dir, task_env, a.model, limit,
                    transcript, a.out / f"{name}.stderr.txt")
            elif a.harness == "gemini":
                code, timed_out, secs = run_gemini(
                    gemini, prompt, run_dir, task_env, limit, transcript, a.out / f"{name}.stderr.txt")
            else:
                code, timed_out, secs = run_codex(
                    codex, prompt, run_dir, task_env, limit, transcript, a.out / f"{name}.stderr.txt")
            redact_key([transcript, a.out / f"{name}.stderr.txt"], key)
            try:
                passed, detail = task["check"](str(work))
            except Exception as e:  # a check must never take the run down
                passed, detail = False, f"check error: {e!r}"
            rec = {"env": a.env, "harness": a.harness, "task": task["id"], "rep": rep, "passed": passed, "detail": detail[:500],
                   "exit_code": code, "timed_out": timed_out, "seconds": round(secs, 1)}
            rec.update({"claude": parse_transcript, "opencode": parse_opencode, "codex": parse_codex,
                        "gemini": parse_gemini}[a.harness](transcript))
            records.append(rec)
            failed = sum(c["is_error"] for c in rec["shell_calls"])
            print(f"[{a.env}] {name}: {'PASS' if passed else 'FAIL'} shell={len(rec['shell_calls'])} "
                  f"failed={failed} turns={rec['num_turns']} {rec['seconds']}s  {detail[:120]}", flush=True)
            (a.out / "results.json").write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")

    n = len(records)
    passed = sum(r["passed"] for r in records)
    shell = sum(len(r["shell_calls"]) for r in records)
    failed = sum(c["is_error"] for r in records for c in r["shell_calls"])
    md = [f"## agent-e2e: {a.harness}, {a.env}", "",
          f"**{passed}/{n} tasks passed**; {shell} shell calls, {failed} errored "
          f"({(100 * failed / shell) if shell else 0:.0f}%).", "",
          "| task | rep | pass | shell calls | errored | turns | in/out tokens | s | tools |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        tools = ", ".join(f"{k}×{v}" for k, v in sorted(r["tools_used"].items()))
        md.append(f"| {r['task']} | {r['rep']} | {'✅' if r['passed'] else '❌'} | {len(r['shell_calls'])} | "
                  f"{sum(c['is_error'] for c in r['shell_calls'])} | {r['num_turns']} | "
                  f"{r['input_tokens']}/{r['output_tokens']} | {r['seconds']} | {tools} |")
    (a.out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
