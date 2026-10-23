"""Agent end-to-end tasks: each builds a fixture in an empty directory, gives the agent
a prompt, and checks the result. Tasks target known failure categories of agents on
Windows (habits from Linux, escaping, reading output, finding the environment, line
endings) on purpose, but are phrased the way a user would ask.

A check returns (passed, detail).
"""
import json
import os
import subprocess
from pathlib import Path


def _write(root, rel, data, newline="\n"):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8", newline=newline)


def _read(root, rel):
    p = Path(root, rel)
    return p.read_bytes() if p.exists() else None


def _text(root, rel):
    b = _read(root, rel)
    return None if b is None else b.decode("utf-8", "replace")


def _lines(root, rel):
    t = _text(root, rel)
    return None if t is None else [l.rstrip("\r") for l in t.splitlines() if l.strip()]


# --- grep with structured output ------------------------------------------------

def setup_todos(root):
    _write(root, "src/app/main.py", "import os\n# TODO: parse args\nprint('x')\n")
    _write(root, "src/app/util.py", "def f():\n    pass  # TODO handle errors\n")
    _write(root, "src/lib/readme.md", "Nothing to do here.\n")
    _write(root, "src/lib/core.js", "// TODO(sam): cache\nconst a = 1;\n// todo lowercase is not a match\n")


def check_todos(root):
    want = sorted([
        "src/app/main.py:2:# TODO: parse args",
        "src/app/util.py:2:    pass  # TODO handle errors",
        "src/lib/core.js:1:// TODO(sam): cache",
    ])
    got = _lines(root, "todos.txt")
    if got is None:
        return False, "todos.txt missing"
    got = sorted(l.replace("\\", "/").removeprefix("./") for l in got)
    return got == want, f"got {got!r}"


# --- in-place edit that must keep CRLF ----------------------------------------------

def setup_crlf(root):
    _write(root, "config/app.ini", "[server]\nhost=0.0.0.0\nport=8080\n\n[log]\nlevel=info\n", newline="\r\n")


def check_crlf(root):
    b = _read(root, "config/app.ini")
    want = b"[server]\r\nhost=0.0.0.0\r\nport=9090\r\n\r\n[log]\r\nlevel=info\r\n"
    if b is None:
        return False, "file missing"
    return b == want, f"bytes {b!r}"


# --- env var for one command ---------------------------------------------------------

def setup_env(root):
    _write(root, "scripts/show-mode.js",
           "const m = process.env.APP_MODE || 'unset';\nconsole.log(`mode=${m}`);\n")


def check_env(root):
    t = _text(root, "out/mode.txt")
    return (t is not None and t.strip() == "mode=staging"), f"out/mode.txt={t!r}"


# --- MSVC build (environment discovery) ----------------------------------------------

def setup_msvc(root):
    _write(root, "src/hello.c",
           '#include <stdio.h>\nint main(void) { printf("hello from msvc\\n"); return 0; }\n')


def check_msvc(root):
    exe = _read(root, "hello.exe")
    out = _text(root, "build.txt")
    ok = exe is not None and exe[:2] == b"MZ" and out is not None and "hello from msvc" in out
    return ok, f"hello.exe={'yes' if exe else 'no'} build.txt={out!r}"


# --- quoting stress ---------------------------------------------------------------------

QUOTE_LINE = 'He said "it\'s at C:\\Temp\\new folder\\" & left; cost was $5 (50% off) `now`'


def setup_quote(root):
    pass


def check_quote(root):
    t = _text(root, "note.txt")
    if t is None:
        return False, "note.txt missing"
    got = t.rstrip("\r\n")
    return got == QUOTE_LINE, f"got {got!r}"


# --- git ---------------------------------------------------------------------------------

def setup_git(root):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    _write(root, "README.md", "demo\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, env=env)


def check_git(root):
    def git(*a):
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True).stdout.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    msg = git("log", "-1", "--format=%s")
    files = git("show", "--name-only", "--format=", "HEAD")
    content = _text(root, "notes/today.md")
    ok = branch == "feature/notes" and msg == "Add today's notes" and "notes/today.md" in files \
        and content is not None and content.strip() == "- ship sill"
    return ok, f"branch={branch!r} msg={msg!r} files={files!r} content={content!r}"


# --- find by size ----------------------------------------------------------------------

def setup_bigfiles(root):
    sizes = {"logs/a.log": 300, "logs/old/b.log": 5000, "logs/c.log": 1200, "logs/old/deep/d.log": 9000,
             "logs/e.txt": 99999, "logs/f.log": 10}
    for rel, n in sizes.items():
        _write(root, rel, b"x" * n)


def check_bigfiles(root):
    want = ["logs/old/deep/d.log", "logs/old/b.log", "logs/c.log"]
    got = _lines(root, "big.txt")
    if got is None:
        return False, "big.txt missing"
    got = [l.replace("\\", "/").removeprefix("./").strip() for l in got]
    return got == want, f"got {got!r}"


# --- python + json ------------------------------------------------------------------------

def setup_json(root):
    _write(root, "data/items.json", json.dumps({"items": [
        {"name": "a", "price": 1.25}, {"name": "b", "price": 10}, {"name": "c", "price": 3.5}]}, indent=2))


def check_json(root):
    t = _text(root, "total.txt")
    try:
        return abs(float(t.strip()) - 14.75) < 1e-9, f"total.txt={t!r}"
    except Exception:
        return False, f"total.txt={t!r}"


# --- unicode through a shell redirect (encoding) -----------------------------------

UNICODE_TEXT = "naïve café 日本語 €"


def setup_unicode(root):
    pass


def check_unicode(root):
    b = _read(root, "unicode.txt")
    if b is None:
        return False, "unicode.txt missing"
    ok = b in (UNICODE_TEXT.encode() + b"\n", UNICODE_TEXT.encode() + b"\r\n")
    return ok, f"bytes {b[:80]!r}"


# --- background process lifecycle (job control) -------------------------------------

def setup_server(root):
    _write(root, "site/hello.txt", "hello from site\n")


def check_server(root):
    import socket
    t = _text(root, "fetched.txt")
    s = socket.socket()
    s.settimeout(2)
    still_running = s.connect_ex(("127.0.0.1", 8765)) == 0
    s.close()
    ok = t is not None and t.replace("\r", "").strip() == "hello from site" and not still_running
    return ok, f"fetched={t!r} server_still_running={still_running}"


# --- Unix-first package.json script ----------------------------------------------------

def setup_npm(root):
    _write(root, "package.json", json.dumps({
        "name": "demo", "version": "1.0.0", "private": True,
        "scripts": {"build": "rm -rf dist && mkdir -p dist && cp src/*.txt dist/ && echo built > dist/STAMP"},
    }, indent=2))
    _write(root, "src/a.txt", "a\n")
    _write(root, "src/b.txt", "b\n")
    _write(root, "dist/stale.txt", "old\n")


def check_npm(root):
    d = Path(root, "dist")
    names = sorted(p.name for p in d.iterdir()) if d.is_dir() else None
    return names == ["STAMP", "a.txt", "b.txt"], f"dist={names}"


# --- a repo's own scripts, run as the README says ---------------------------------------

def setup_scripts(root):
    _write(root, "README.md", "# demo\n\nGenerate the data, then check it:\n\n    ./scripts/gen.sh\n    ./tools/check.py out/data.txt\n")
    _write(root, "scripts/gen.sh", "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p out\nfor i in 1 2 3; do echo \"row $i\"; done > out/data.txt\n")
    _write(root, "tools/check.py", "#!/usr/bin/env python3\nimport sys\nrows = open(sys.argv[1]).read().split()\nprint(f\"{len(rows) // 2} rows ok\")\n")


def check_scripts(root):
    data = _text(root, "out/data.txt")
    out = _text(root, "check.txt")
    ok = data is not None and data.replace("\r", "") == "row 1\nrow 2\nrow 3\n" and out is not None and "3 rows ok" in out
    return ok, f"data={data!r} check={out!r}"


# --- harder tasks (2026-10-23): Windows-boundary edges where shells differ ---------------

def setup_make(root):
    _write(root, "Makefile",
           "SRCS := $(wildcard src/*.txt)\n"
           "OUT := $(patsubst src/%.txt,build/%.out,$(SRCS))\n"
           "all: $(OUT)\n"
           "build/%.out: src/%.txt\n\t@mkdir -p build\n\tsed 's/^/> /' $< > $@\n"
           "test: all\n\t@for f in $(OUT); do grep -q '^> ' $$f && echo \"ok $$f\" || echo \"FAIL $$f\"; done\n"
           "clean:\n\trm -rf build\n.PHONY: all test clean\n")
    _write(root, "src/alpha.txt", "one\ntwo\n")
    _write(root, "src/beta.txt", "three\n")


def check_make(root):
    out = _text(root, "test.txt")
    built = sorted(p.name for p in Path(root, "build").glob("*.out")) if Path(root, "build").is_dir() else []
    ok = out is not None and "ok build/alpha.out" in out and "ok build/beta.out" in out and "FAIL" not in out and built == ["alpha.out", "beta.out"]
    return ok, f"built={built} test={out!r}"


def setup_json_report(root):
    users = [{"name": f"u{i}", "email": f"user{i}@example.com", "active": i % 3 != 0} for i in range(1, 13)]
    _write(root, "data/users.json", json.dumps({"users": users}, indent=2))


def check_json_report(root):
    t = _text(root, "emails.txt")
    emails = [f"user{i}@example.com" for i in range(1, 13) if i % 3 != 0]
    got = [l.rstrip("\r") for l in (t or "").split("\n") if l.strip()]
    stray_cr = t is not None and any("\r" in l for l in t.split("\r\n"))
    # "Sorted" is ambiguous here (user10@ < user1@ by bytes; user1 first by number or by
    # the name before the @); any of them is right. The task is about getting the data.
    orders = (sorted(emails), sorted(emails, key=lambda e: "".join(c for c in e if c.isalnum())),
              sorted(emails, key=lambda e: (int(e[4:e.index("@")]), e)),
              sorted(emails, key=lambda e: e.split("@")[0]))
    return got in orders and not stray_cr, f"got={got[:4]}... stray_cr={stray_cr}"


def setup_dev_server(root):
    _write(root, "package.json", json.dumps({"name": "dev", "version": "1.0.0", "private": True,
                                             "scripts": {"start": "node server.js"}}, indent=2))
    _write(root, "server.js",
           "require('http').createServer((q, r) => r.end(q.url === '/health' ? 'ok\\n' : 'nope\\n'))"
           ".listen(8770, '127.0.0.1', () => console.log('listening on 8770'));\n")


def check_dev_server(root):
    import socket
    t = _text(root, "health.txt")
    s = socket.socket()
    s.settimeout(2)
    still = s.connect_ex(("127.0.0.1", 8770)) == 0
    s.close()
    ok = t is not None and t.replace("\r", "").strip() == "ok" and not still
    return ok, f"health={t!r} server_still_running={still}"


UNICODE_DIRS = {"my docs/报告 2026/第一.txt": "alpha beta gamma\n", "my docs/报告 2026/second note.txt": "one two\n",
                "my docs/top level.txt": "x y z w\n"}


def setup_unicode_paths(root):
    for rel, text in UNICODE_DIRS.items():
        _write(root, rel, text)


def check_unicode_paths(root):
    t = _text(root, "counts.txt")
    want = sorted(f"{rel}: {len(text.split())}" for rel, text in UNICODE_DIRS.items())
    got = [l.rstrip("\r") for l in (t or "").split("\n") if l.strip()]
    return got == want, f"got={got}"


def setup_bat_args(root):
    _write(root, "tools/report.bat", "@echo off\r\necho name=[%~1]\r\necho pct=[%~2]\r\n", newline="")


def check_bat_args(root):
    t = (_text(root, "report.txt") or "").replace("\r", "")
    ok = "name=[release build]" in t and "pct=[100%]" in t
    return ok, f"report={t!r}"


def setup_many_files(root):
    d = Path(root, "data")
    d.mkdir(parents=True, exist_ok=True)
    for i in range(4000):
        (d / f"record_{i:04d}_with_a_longer_name.txt").write_text(f"record {i}\n" + ("TODO-FIXME\n" if i in (17, 1234, 3999) else ""))


def check_many_files(root):
    t = _text(root, "hits.txt")
    want = [f"data/record_{i:04d}_with_a_longer_name.txt" for i in (17, 1234, 3999)]
    got = [l.rstrip("\r") for l in (t or "").split("\n") if l.strip()]
    return got == want, f"got={got}"


TASKS = [
    {"id": "grep-todos", "setup": setup_todos, "check": check_todos,
     "prompt": "Find every line under src/ that contains the exact uppercase word TODO. Write them to todos.txt, one per line, as path:line_number:line_text with paths relative to the current directory using forward slashes, sorted. Do not include lowercase 'todo'."},
    {"id": "crlf-edit", "setup": setup_crlf, "check": check_crlf,
     "prompt": "In config/app.ini change the port from 8080 to 9090. The file uses Windows (CRLF) line endings; keep them exactly, and change nothing else."},
    {"id": "env-one-command", "setup": setup_env, "check": check_env,
     "prompt": "Run `node scripts/show-mode.js` with the environment variable APP_MODE set to staging for that one command only, and save its standard output to out/mode.txt (create the out directory)."},
    {"id": "msvc-build", "setup": setup_msvc, "check": check_msvc,
     "prompt": "Compile src/hello.c into hello.exe in the current directory using the Microsoft Visual C++ compiler (cl.exe from the installed Visual Studio). Then run hello.exe and save its output to build.txt."},
    {"id": "quote-stress", "setup": setup_quote, "check": check_quote,
     "prompt": "Create note.txt containing exactly this one line (followed by a newline), byte for byte:\n" + QUOTE_LINE},
    {"id": "git-branch-commit", "setup": setup_git, "check": check_git,
     "prompt": "This is a git repository. Create and switch to a branch named feature/notes, add a file notes/today.md containing the single line `- ship sill`, and commit it with the message: Add today's notes"},
    {"id": "find-largest", "setup": setup_bigfiles, "check": check_bigfiles,
     "prompt": "Under logs/ (including subdirectories), find the 3 largest files with the .log extension. Write their paths, relative to the current directory with forward slashes, largest first, one per line, to big.txt."},
    {"id": "python-json-sum", "setup": setup_json, "check": check_json,
     "prompt": "Using Python, read data/items.json and write the sum of all the items' price fields to total.txt (just the number)."},
    {"id": "unicode-redirect", "setup": setup_unicode, "check": check_unicode,
     "prompt": "Using a shell output redirect (>), save the output of this command to unicode.txt: python -c \"print('" + UNICODE_TEXT + "')\"\nThe file must contain exactly that text as UTF-8 (one line)."},
    {"id": "background-server", "setup": setup_server, "check": check_server,
     "prompt": "Start a local HTTP server on port 8765 that serves the files in site/ (use Python's http.server), running in the background. Fetch http://127.0.0.1:8765/hello.txt and save the response body to fetched.txt. Then stop the server; make sure nothing is left listening on port 8765."},
    {"id": "repo-scripts", "setup": setup_scripts, "check": check_scripts,
     "prompt": "Follow the README: run the generator script, then run the checker on its output, and save the checker's output to check.txt."},
    {"id": "make-project", "setup": setup_make, "check": check_make,
     "prompt": "Build this project with make (the default target), then run its test target and save the test target's output to test.txt."},
    {"id": "json-report", "setup": setup_json_report, "check": check_json_report,
     "prompt": "From data/users.json, write the email addresses of the users whose `active` field is true to emails.txt, one per line, sorted."},
    {"id": "dev-server-npm", "setup": setup_dev_server, "check": check_dev_server,
     "prompt": "Start this project's dev server with `npm start`, running in the background. Fetch http://127.0.0.1:8770/health and save the response body to health.txt. Then stop the server completely: nothing may be left listening on port 8770."},
    {"id": "unicode-paths", "setup": setup_unicode_paths, "check": check_unicode_paths,
     "prompt": "Count the words in every .txt file under the `my docs` directory (recursively). Write one line per file to counts.txt, formatted as `<path>: <count>`, with paths relative to the current directory using forward slashes, sorted by path."},
    {"id": "bat-args", "setup": setup_bat_args, "check": check_bat_args,
     "prompt": "Run the batch script tools\\report.bat with two arguments: the first is `release build` (one argument, with the space), the second is `100%`. Save its output to report.txt."},
    {"id": "many-files", "setup": setup_many_files, "check": check_many_files,
     "prompt": "Among the files directly under data/, find those that contain the text TODO-FIXME. Write their paths to hits.txt, one per line, sorted, in the form data/<file name>."},
    {"id": "npm-unix-script", "setup": setup_npm, "check": check_npm,
     "prompt": "Run this project's build script (npm run build) and make sure it succeeds. Afterwards dist/ must contain exactly a.txt, b.txt and STAMP."},
]
