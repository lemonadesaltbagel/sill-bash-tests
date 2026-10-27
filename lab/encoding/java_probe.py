"""What does Java write into a harness's pipe, and what does the agent read back?

Since JDK 18 (JEP 400) Java's default charset is UTF-8, but System.out still encodes for the
platform: `stdout.encoding` is the console's code page when there is a console and
`native.encoding` (the ANSI code page: GBK on Chinese Windows) when output is a pipe, as
under an agent. javac's diagnostics go the same way, in the system's language. This runs a
Java program and a failing javac through sill as a harness does (a pipe, a hidden console),
directly and through sill's MCP server, with and without JAVA_TOOL_OPTIONS asking for UTF-8,
and reports the raw bytes and what an agent decoding UTF-8 would see.

Usage (on Windows):  python java_probe.py --sill C:\\...\\sill\\bash.exe --java-home C:\\jdk
                     [--out java-probe.json] [--report java-probe.md]
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "conformance"))
from run import McpClient  # noqa: E402

CREATE_NO_WINDOW = 0x08000000  # how Node starts a harness's shell (windowsHide)
TEXT = "中文 café"
HELLO = f"""public class Hello {{
    public static void main(String[] args) {{
        System.out.println("out: {TEXT}");
        System.err.println("err: {TEXT}");
    }}
}}
"""
BAD = "public class Bad { void f() { int x = \"s\"; } }\n"
UTF8_OPTS = "-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8"


def classify(raw):
    """How the bytes read: as UTF-8, or only in the ANSI code page."""
    try:
        return "utf-8", raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return f"cp{ctypes.windll.kernel32.GetACP()}", raw.decode(f"cp{ctypes.windll.kernel32.GetACP()}")
    except UnicodeDecodeError:
        return "neither", raw.decode("utf-8", "replace")


def direct(sill, script, cwd, env):
    p = subprocess.run([sill, "-c", script], cwd=cwd, env=env, capture_output=True,
                       stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=120)
    out_kind, out = classify(p.stdout)
    err_kind, err = classify(p.stderr)
    return {"exit": p.returncode,
            "stdout": {"bytes": p.stdout[:200].hex(" "), "reads_as": out_kind, "text": out[:300],
                       "agent_sees": p.stdout.decode("utf-8", "replace")[:300]},
            "stderr": {"bytes": p.stderr[:200].hex(" "), "reads_as": err_kind, "text": err[:300],
                       "agent_sees": p.stderr.decode("utf-8", "replace")[:300]}}


def via_mcp(sill, script, cwd, env):
    client = McpClient([sill, "--mcp"], cwd, env)
    try:
        result = client.request("tools/call", {"name": "bash", "arguments": {"command": script}})
    finally:
        client.close()
    return {"text": "".join(c.get("text", "") for c in result.get("content", []))[:600]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sill", required=True)
    ap.add_argument("--java-home", required=True)
    ap.add_argument("--out", default="java-probe.json")
    ap.add_argument("--report", default="java-probe.md")
    a = ap.parse_args()

    k32 = ctypes.windll.kernel32
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["JAVA_HOME"] = a.java_home
    env["PATH"] = str(Path(a.java_home) / "bin") + os.pathsep + env["PATH"]
    env.pop("JAVA_TOOL_OPTIONS", None)
    work = Path(tempfile.mkdtemp(prefix="java-probe-"))
    (work / "Hello.java").write_text(HELLO, encoding="utf-8")
    (work / "Bad.java").write_text(BAD, encoding="utf-8")
    java = str(Path(a.java_home) / "bin" / "java.exe")
    props = subprocess.run([java, "-XshowSettings:properties", "-version"], capture_output=True,
                           env=env, creationflags=CREATE_NO_WINDOW).stderr
    props = [l.strip() for l in props.decode("utf-8", "replace").splitlines()
             if "encoding" in l or "user.language" in l or "user.country" in l or "java.version " in l]

    subprocess.run([str(Path(a.java_home) / "bin" / "javac.exe"), "Hello.java"], cwd=work, env=env, check=True)
    with_opts = dict(env, JAVA_TOOL_OPTIONS=UTF8_OPTS)
    results = {
        "acp": k32.GetACP(), "oemcp": k32.GetOEMCP(), "java_properties": props,
        "run": {
            "direct": direct(a.sill, "java -cp . Hello", work, env),
            "direct, JAVA_TOOL_OPTIONS": direct(a.sill, "java -cp . Hello", work, with_opts),
            "mcp": via_mcp(a.sill, "java -cp . Hello", work, env),
            "mcp, JAVA_TOOL_OPTIONS": via_mcp(a.sill, "java -cp . Hello", work, with_opts),
        },
        "javac error": {
            "direct": direct(a.sill, "javac Bad.java", work, env),
            "direct, JAVA_TOOL_OPTIONS": direct(a.sill, "javac Bad.java", work, with_opts),
            "mcp": via_mcp(a.sill, "javac Bad.java", work, env),
        },
    }
    Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    md = ["# Java output under an agent harness", "",
          f"ANSI code page {results['acp']}, OEM code page {results['oemcp']}.", "", "```"]
    md += props + ["```", ""]
    for group in ("run", "javac error"):
        md += [f"## {group}", ""]
        for name, r in results[group].items():
            md += [f"### {name}", ""]
            if "text" in r and "stdout" not in r:
                md += ["MCP result:", "```", r["text"], "```", ""]
                continue
            for stream in ("stdout", "stderr"):
                s = r[stream]
                md += [f"{stream}: reads as **{s['reads_as']}**; an agent decoding UTF-8 sees:", "```",
                       s["agent_sees"], "```", ""]
    Path(a.report).write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
