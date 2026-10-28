"""Windows-boundary tasks: longer, multi-step work where Windows itself gets in the way of
an agent (native toolchains, .bat and PowerShell, processes that hold ports and files,
non-English text, CRLF, long paths). Each task has a fixture, a prompt phrased as a user
would ask, a check that doesn't depend on the shell under test, and a reference solution
in bash (run by winbench_oracle.py to show the task can be done on the machine).

A check returns (passed, detail). Used as agent-e2e tasks with run.py --task-source winbench.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import zipfile
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _write(root, rel, data, newline="\n", encoding="utf-8"):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding=encoding, newline=newline)


def _read(root, rel):
    p = Path(root, rel)
    return p.read_bytes() if p.exists() else None


def _text(root, rel):
    b = _read(root, rel)
    return None if b is None else b.decode("utf-8", "replace")


def _run(argv, cwd, timeout=600, env=None, shell=False):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, env=env, shell=shell,
                       creationflags=CREATE_NO_WINDOW)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def _port_open(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _processes_with(fragment):
    # (Not this query's own PowerShell, whose command line has the fragment too.)
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + fragment + "*' -and "
          "$_.ProcessId -ne $PID -and $_.Name -notlike 'powershell*' } | ForEach-Object { $_.ProcessId }")
    _, out = _run(["powershell", "-NoProfile", "-Command", ps], None, timeout=60)
    return [l.strip() for l in out.splitlines() if l.strip().isdigit()]


def _kill(pids):
    for pid in pids:
        subprocess.run(["taskkill", "/T", "/F", "/PID", pid], capture_output=True)


def _vcvars():
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if not vswhere.exists():
        return None
    code, out = _run([str(vswhere), "-latest", "-products", "*", "-property", "installationPath"], None)
    vs = out.strip().splitlines()[0] if out.strip() else ""
    bat = Path(vs, "VC", "Auxiliary", "Build", "vcvars64.bat")
    return str(bat) if bat.exists() else None


# --- 1. MSVC + CMake: build, test, fix ----------------------------------------------

def setup_msvc_cmake(root):
    _write(root, "CMakeLists.txt",
           "cmake_minimum_required(VERSION 3.20)\nproject(stats C)\nenable_testing()\n"
           "add_library(stats src/stats.c)\nadd_executable(test_stats tests/test_stats.c)\n"
           "target_link_libraries(test_stats stats)\nadd_test(NAME stats COMMAND test_stats)\n")
    _write(root, "src/stats.h", "double mean(const int *v, int n);\nint median3(int a, int b, int c);\n")
    _write(root, "src/stats.c",
           '#include "stats.h"\n\ndouble mean(const int *v, int n) {\n    int s = 0;\n'
           "    for (int i = 0; i < n; i++) s += v[i];\n    return s / n;\n}\n\n"
           "int median3(int a, int b, int c) {\n    if ((a <= b && b <= c) || (c <= b && b <= a)) return b;\n"
           "    if ((b <= a && a <= c) || (c <= a && a <= b)) return a;\n    return c;\n}\n")
    _write(root, "tests/test_stats.c",
           '#include <stdio.h>\n#include <math.h>\n#include "../src/stats.h"\n\nint main(void) {\n'
           "    int v[] = {1, 2, 4};\n    int fails = 0;\n"
           '    if (fabs(mean(v, 3) - 7.0 / 3.0) > 1e-9) { printf("FAIL mean\\n"); fails++; }\n'
           '    if (median3(3, 1, 2) != 2) { printf("FAIL median3\\n"); fails++; }\n'
           '    if (!fails) printf("all passed\\n");\n    return fails;\n}\n')


def check_msvc_cmake(root):
    log = _text(root, "test.log") or ""
    vc = _vcvars()
    if not vc:
        return False, "no Visual Studio on this machine"
    build = Path(root, "_check_build")
    shutil.rmtree(build, ignore_errors=True)
    # Through a .cmd file: cmd.exe reads a quoted path in a /c string its own way.
    script = Path(root, "_check_build.cmd")
    script.write_text(f'@call "{vc}" >nul\r\ncmake -S . -B _check_build -G "NMake Makefiles" >nul && '
                      "cmake --build _check_build >nul && ctest --test-dir _check_build --output-on-failure\r\n",
                      encoding="utf-8")
    code, out = _run(["cmd.exe", "/d", "/c", str(script)], root, timeout=900)
    script.unlink(missing_ok=True)
    shutil.rmtree(build, ignore_errors=True)
    ok = code == 0 and "100% tests passed" in out and ("passed" in log or "Passed" in log)
    return ok, f"ctest exit {code}; test.log has a pass: {'passed' in log or 'Passed' in log}; {out[-200:]}"


SOLVE_MSVC_CMAKE = r"""sed -i 's|return s / n;|return (double)s / n;|' src/stats.c
vc=$("/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe" -latest -products '*' -property installationPath | tr -d '\r')
cat > build.cmd <<EOF
@call "$vc\\VC\\Auxiliary\\Build\\vcvars64.bat" >nul
cmake -S . -B build -G "NMake Makefiles" && cmake --build build && ctest --test-dir build
EOF
MSYS_NO_PATHCONV=1 cmd.exe /d /c build.cmd > test.log 2>&1
"""


# --- 2. A .bat release script: spaces and % in arguments, a quoting bug -------------

def setup_bat_release(root):
    _write(root, "scripts/release.bat",
           "@echo off\r\nset VERSION=%~1\r\nset NOTES=%~2\r\nset OUT=%~3\r\n"
           "if not exist %OUT% mkdir %OUT%\r\n"
           "echo version=%VERSION%> %OUT%\\manifest.txt\r\n"
           "echo notes=%NOTES%>> %OUT%\\manifest.txt\r\n"
           "echo built %VERSION% into %OUT%\r\n", newline="")


def check_bat_release(root):
    m = _read(root, "dist/My App 1.2.3/manifest.txt")
    if m is None:
        return False, "dist/My App 1.2.3/manifest.txt missing"
    t = m.decode("utf-8", "replace").replace("\r", "")
    ok = "version=1.2.3" in t and "notes=Faster startup, 100% of tests" in t
    return ok, f"manifest={t!r}"


SOLVE_BAT_RELEASE = r"""sed -i 's|if not exist %OUT% mkdir %OUT%|if not exist "%OUT%" mkdir "%OUT%"|; s|> %OUT%\\manifest.txt|> "%OUT%\\manifest.txt"|; s|>> %OUT%\\manifest.txt|>> "%OUT%\\manifest.txt"|' scripts/release.bat
./scripts/release.bat 1.2.3 "Faster startup, 100% of tests" "dist/My App 1.2.3"
"""


# --- 3. A server in the background, integration tests, a clean stop -----------------

PORT_SERVER = 8790


def setup_node_integration(root):
    _write(root, "package.json", json.dumps({"name": "svc", "version": "1.0.0", "private": True,
                                             "scripts": {"start": "node server.js", "test:int": "node test/integration.js"}}, indent=2))
    _write(root, "server.js",
           "const port = Number(process.env.PORT || 3000);\n"
           "require('http').createServer((q, r) => {\n"
           "  if (q.url === '/sum?a=2&b=40') return r.end(JSON.stringify({sum: 42}));\n"
           "  if (q.url === '/health') return r.end('ok');\n  r.statusCode = 404; r.end('no');\n"
           "}).listen(port, '127.0.0.1', () => console.log('listening on ' + port));\n")
    _write(root, "test/integration.js",
           "const port = Number(process.env.PORT || 3000);\n"
           "const get = p => new Promise((ok, no) => require('http').get({host: '127.0.0.1', port, path: p}, r => {"
           " let b = ''; r.on('data', d => b += d); r.on('end', () => ok(b)); }).on('error', no));\n"
           "(async () => { const h = await get('/health'); const s = JSON.parse(await get('/sum?a=2&b=40'));\n"
           "  if (h !== 'ok' || s.sum !== 42) { console.log('FAIL'); process.exit(1); }\n"
           "  console.log('integration: 2 passed'); })().catch(e => { console.log('FAIL ' + e.message); process.exit(1); });\n")


def check_node_integration(root):
    t = (_text(root, "integration.txt") or "").replace("\r", "")
    still = _port_open(PORT_SERVER)
    left = _processes_with("server.js")
    _kill(left)
    ok = "integration: 2 passed" in t and not still
    return ok, f"integration.txt={t.strip()!r} port_still_open={still} leftover_node={len(left)}"


SOLVE_NODE_INTEGRATION = r"""PORT=8790 npm start > server.log 2>&1 &
pid=$!
for i in $(seq 30); do curl -s http://127.0.0.1:8790/health >/dev/null && break; sleep 1; done
PORT=8790 npm run -s test:int > integration.txt
kill $pid
"""


# --- 4. A Python virtual environment: create, activate, install, test, fix ----------

def setup_python_venv(root):
    _write(root, "requirements.txt", "pytest==8.3.3\n")
    _write(root, "slugify.py",
           "import re\n\ndef slugify(title):\n    s = title.lower()\n    s = re.sub(r'[^a-z0-9]+', '-', s)\n"
           "    return s\n")
    _write(root, "test_slugify.py",
           "from slugify import slugify\n\ndef test_simple():\n    assert slugify('Hello World') == 'hello-world'\n\n"
           "def test_edges():\n    assert slugify('  Already--Slugged!  ') == 'already-slugged'\n")
    _write(root, "README.md", "Set up a virtual environment in `.venv`, install `requirements.txt`, run the tests.\n")


def check_python_venv(root):
    py = Path(root, ".venv", "Scripts", "python.exe")
    if not py.exists():
        return False, ".venv\\Scripts\\python.exe missing"
    code, out = _run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider"], root, timeout=300)
    t = (_text(root, "pytest.txt") or "")
    ok = code == 0 and "2 passed" in out and "passed" in t
    return ok, f"pytest exit {code}: {out.strip()[-160:]}; pytest.txt has 'passed': {'passed' in t}"


SOLVE_PYTHON_VENV = r"""python -m venv .venv
source .venv/Scripts/activate
pip install -q -r requirements.txt
cat > slugify.py <<'EOF'
import re

def slugify(title):
    s = title.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')
EOF
python -m pytest -q > pytest.txt 2>&1
"""


# --- 5. A report from CSVs in UTF-8 (BOM, CRLF) and GBK, with Chinese names ---------

SALES = [("张伟", 120), ("李娜", 80), ("张伟", 30), ("王芳", 200), ("李娜", 15)]
CUSTOMERS = {"张伟": "北京", "李娜": "上海", "王芳": "广州"}


def setup_unicode_report(root):
    rows = "客户,金额\r\n" + "".join(f"{n},{a}\r\n" for n, a in SALES)
    _write(root, "data/销售 2026-Q1.csv", b"\xef\xbb\xbf" + rows.encode("utf-8"))
    cust = "客户,城市\n" + "".join(f"{n},{c}\n" for n, c in CUSTOMERS.items())
    _write(root, "data/客户 名单.csv", cust.encode("gbk"))
    _write(root, "README.md", "data/客户 名单.csv comes from the old CRM, which exports in GBK.\n")


def _want_report():
    totals = {}
    for n, a in SALES:
        totals[n] = totals.get(n, 0) + a
    return [f"{n},{CUSTOMERS[n]},{totals[n]}" for n in sorted(totals, key=lambda k: -totals[k])]


def check_unicode_report(root):
    b = _read(root, "report.csv")
    if b is None:
        return False, "report.csv missing"
    if b.startswith(b"\xef\xbb\xbf"):
        return False, "report.csv starts with a BOM"
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        return False, "report.csv isn't UTF-8"
    lines = [l for l in t.replace("\r\n", "\n").split("\n") if l.strip()]
    ok = lines == ["客户,城市,合计"] + _want_report()
    return ok, f"got={lines}"


SOLVE_UNICODE_REPORT = r"""python - <<'EOF'
import csv, io
sales = open('data/销售 2026-Q1.csv', encoding='utf-8-sig').read().splitlines()[1:]
cities = dict(l.split(',') for l in open('data/客户 名单.csv', encoding='gbk').read().splitlines()[1:])
tot = {}
for l in sales:
    n, a = l.split(','); tot[n] = tot.get(n, 0) + int(a)
with open('report.csv', 'w', encoding='utf-8', newline='\n') as f:
    f.write('客户,城市,合计\n')
    for n in sorted(tot, key=lambda k: -tot[k]):
        f.write(f'{n},{cities[n]},{tot[n]}\n')
EOF
"""


# --- 6. A merge conflict in a CRLF file, line endings kept --------------------------

def _git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@e")
    return _run(["git", "-c", "core.autocrlf=false", *args], root, env=env)


def setup_git_crlf_merge(root):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    # As in a user's own repository: git knows who commits (a merge won't start otherwise).
    _git(root, "config", "user.name", "Dev")
    _git(root, "config", "user.email", "dev@example.com")
    _write(root, "config.ini", "[server]\r\nport=8080\r\nhost=localhost\r\n[log]\r\nlevel=info\r\n", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, "config.ini", "[server]\r\nport=9090\r\nhost=localhost\r\n[log]\r\nlevel=info\r\nfile=app.log\r\n", newline="")
    _git(root, "commit", "-qam", "feature: port 9090, log file")
    _git(root, "checkout", "-q", "main")
    _write(root, "config.ini", "[server]\r\nport=8081\r\nhost=0.0.0.0\r\n[log]\r\nlevel=info\r\n", newline="")
    _git(root, "commit", "-qam", "main: host 0.0.0.0, port 8081")


def check_git_crlf_merge(root):
    b = _read(root, "config.ini") or b""
    code, log = _git(root, "log", "--format=%P", "-1")
    merged = len(log.split()) == 2
    _, status = _git(root, "status", "--porcelain")
    lines = b.split(b"\n")
    crlf = all(l.endswith(b"\r") for l in lines[:-1]) and b.endswith(b"\r\n")
    t = b.decode("utf-8", "replace").replace("\r", "")
    content = ("port=9090" in t and "host=0.0.0.0" in t and "file=app.log" in t and "<<<<" not in t)
    ok = merged and crlf and content and not status.strip()
    return ok, f"merge_commit={merged} crlf={crlf} content={content} clean={not status.strip()} file={t!r}"


SOLVE_GIT_CRLF_MERGE = r"""git merge feature
printf '[server]\r\nport=9090\r\nhost=0.0.0.0\r\n[log]\r\nlevel=info\r\nfile=app.log\r\n' > config.ini
git add config.ini
git -c user.email=t@e -c user.name=t commit -qm "Merge feature: port 9090, log file; keep host 0.0.0.0"
git log --oneline --graph -5
"""


# --- 7. A .NET project with a failing test -----------------------------------------

def setup_dotnet_test(root):
    _write(root, "Calc/Calc.csproj",
           '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
           "<Nullable>enable</Nullable></PropertyGroup></Project>\n")
    _write(root, "Calc/Discount.cs",
           "namespace Calc;\npublic static class Discount {\n"
           "    // 10% off orders of 100 or more\n"
           "    public static decimal Apply(decimal total) => total > 100 ? total * 0.9m : total;\n}\n")
    _write(root, "Calc.Tests/Calc.Tests.csproj",
           '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
           "<IsPackable>false</IsPackable></PropertyGroup><ItemGroup>"
           '<PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" />'
           '<PackageReference Include="xunit" Version="2.9.2" />'
           '<PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />'
           '</ItemGroup><ItemGroup><ProjectReference Include="../Calc/Calc.csproj" /></ItemGroup></Project>\n')
    _write(root, "Calc.Tests/DiscountTests.cs",
           "using Xunit;\nusing Calc;\npublic class DiscountTests {\n"
           "    [Fact] public void BelowThreshold() => Assert.Equal(99m, Discount.Apply(99m));\n"
           "    [Fact] public void AtThreshold() => Assert.Equal(90m, Discount.Apply(100m));\n}\n")


def check_dotnet_test(root):
    code, out = _run(["dotnet", "test", "Calc.Tests", "--nologo", "-v", "q"], root, timeout=900)
    t = _text(root, "test-results.txt") or ""
    ok = code == 0 and "Passed!" in out and ("Passed" in t or "passed" in t)
    return ok, f"dotnet test exit {code}: {out.strip()[-200:]}"


SOLVE_DOTNET_TEST = r"""sed -i 's/total > 100/total >= 100/' Calc/Discount.cs
dotnet test Calc.Tests --nologo > test-results.txt 2>&1
"""


# --- 8. A PowerShell script from bash, its JSON post-processed ----------------------

def setup_powershell_interop(root):
    _write(root, "tools/inventory.ps1",
           "param([Parameter(Mandatory)][string]$Path, [string]$Format = 'text')\n"
           "$items = Get-ChildItem -LiteralPath $Path -File | ForEach-Object { [pscustomobject]@{ name = $_.Name; bytes = $_.Length } }\n"
           "if ($Format -eq 'json') { $items | ConvertTo-Json -Compress } else { $items | Format-Table | Out-String }\n")
    for name, size in (("a b.txt", 10), ("报告.txt", 25), ("c.log", 7)):
        _write(root, f"shared data/{name}", b"x" * size)


def check_powershell_interop(root):
    t = (_text(root, "total.txt") or "").strip()
    return t == "42", f"total.txt={t!r}"


SOLVE_POWERSHELL_INTEROP = r"""powershell -NoProfile -ExecutionPolicy Bypass -File tools/inventory.ps1 -Path "shared data" -Format json > inv.json
python -c "import json; print(sum(i['bytes'] for i in json.load(open('inv.json', encoding='utf-8-sig'))))" > total.txt
"""


# --- 9. The port is taken: find what holds it, stop it, start the app --------------

PORT_TAKEN = 8791


def setup_port_taken(root):
    _write(root, "package.json", json.dumps({"name": "app", "version": "1.0.0", "private": True,
                                             "scripts": {"start": "node app.js"}}, indent=2))
    _write(root, "app.js", f"require('http').createServer((q, r) => r.end('app v2 ok')).listen({PORT_TAKEN}, '127.0.0.1');\n")
    _write(root, "old_server.py",
           "import http.server\nclass H(http.server.BaseHTTPRequestHandler):\n"
           "    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'old server')\n"
           f"http.server.HTTPServer(('127.0.0.1', {PORT_TAKEN}), H).serve_forever()\n")
    subprocess.Popen(["python", str(Path(root, "old_server.py"))], cwd=root, creationflags=DETACHED,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if _port_open(PORT_TAKEN):
            break
        time.sleep(0.5)


def check_port_taken(root):
    t = (_text(root, "status.txt") or "").strip()
    old = _processes_with("old_server.py")
    app = _processes_with("app.js")
    _kill(old + app)
    ok = t == "app v2 ok" and not old
    return ok, f"status.txt={t!r} old_server_still_running={bool(old)}"


SOLVE_PORT_TAKEN = r"""pid=$(netstat -ano | grep -E "127\.0\.0\.1:8791 .*LISTENING" | awk '{print $NF}' | head -1 | tr -d '\r')
taskkill //PID "$pid" //F >/dev/null
npm start > app.log 2>&1 &
for i in $(seq 30); do curl -s http://127.0.0.1:8791/ > status.txt && [ -s status.txt ] && break; sleep 1; done
"""


# --- 10. A held file blocks the clean: release it, rebuild ---------------------------

def setup_locked_build(root):
    _write(root, "build.py",
           "import pathlib, shutil\nout = pathlib.Path('build')\nout.mkdir(exist_ok=True)\n"
           "(out / 'app.txt').write_text('built v2\\n')\nprint('built')\n")
    _write(root, "build/app.txt", "built v1\n")
    _write(root, "build/watch.log", "")
    _write(root, "watcher.py", "import time\nf = open('build/watch.log', 'a')\nwhile True:\n    f.write('tick\\n'); f.flush(); time.sleep(1)\n")
    subprocess.Popen(["python", str(Path(root, "watcher.py"))], cwd=root, creationflags=DETACHED,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)


def check_locked_build(root):
    t = (_text(root, "build/app.txt") or "").strip()
    log_left = Path(root, "build", "watch.log").exists()
    watcher = _processes_with("watcher.py")
    _kill(watcher)
    ok = t == "built v2" and not log_left and not watcher
    return ok, f"app.txt={t!r} old watch.log still there={log_left} watcher still running={bool(watcher)}"


SOLVE_LOCKED_BUILD = r"""for pid in $(powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*watcher.py*' } | ForEach-Object { \$_.ProcessId }" | tr -d '\r'); do taskkill //PID $pid //F >/dev/null; done
sleep 1
rm -rf build
python build.py
"""


# --- 11. Paths past 260 characters, into a zip --------------------------------------

DEEP = "/".join(f"module_with_a_rather_long_name_{i:02d}" for i in range(8))


def setup_long_paths(root):
    for i in range(5):
        _write(root, f"src/{DEEP}/file_{i}.txt", f"content {i}\n")
    _write(root, "src/top.txt", "top\n")


def check_long_paths(root):
    z = Path(root, "dist", "src.zip")
    if not z.exists():
        return False, "dist/src.zip missing"
    try:
        names = {n.replace("\\", "/").lstrip("./") for n in zipfile.ZipFile(z).namelist() if not n.endswith("/")}
    except zipfile.BadZipFile:
        return False, "dist/src.zip isn't a zip"
    want = {f"src/{DEEP}/file_{i}.txt" for i in range(5)} | {"src/top.txt"}
    got = {n for n in names if n.startswith("src/")} or {"src/" + n for n in names}
    ok = want <= got
    return ok, f"missing={sorted(want - got)[:2]}"


SOLVE_LONG_PATHS = r"""mkdir -p dist
python -c "import shutil; shutil.make_archive('dist/src', 'zip', '.', 'src')"
"""


# --- 12. A legacy tool's GBK output, to UTF-8 ----------------------------------------

def setup_legacy_output(root):
    _write(root, "tools/legacy_export.py",
           "import sys\nsys.stdout.reconfigure(encoding='gbk')\n"
           "for n, c in [('张伟', '北京'), ('李娜', '上海')]:\n    print(f'{n}\\t{c}')\n")
    _write(root, "README.md", "`python tools/legacy_export.py` is the old exporter; it writes GBK, as the old system did.\n")


def check_legacy_output(root):
    b = _read(root, "customers.tsv")
    if b is None:
        return False, "customers.tsv missing"
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        return False, "customers.tsv isn't UTF-8"
    lines = [l for l in t.replace("\r\n", "\n").split("\n") if l]
    ok = lines == ["张伟\t北京", "李娜\t上海"]
    return ok, f"got={lines}"


SOLVE_LEGACY_OUTPUT = r"""python tools/legacy_export.py | iconv -f GBK -t UTF-8 | tr -d '\r' > customers.tsv
"""


# --- 13. npm scripts written for cmd.exe ----------------------------------------------

def setup_cmd_npm_scripts(root):
    _write(root, "package.json", json.dumps({"name": "site", "version": "1.0.0", "private": True,
                                             "scripts": {"build": "set NODE_ENV=production&& node build.js"}}, indent=2))
    _write(root, "build.js",
           "require('fs').mkdirSync('dist', {recursive: true});\n"
           "require('fs').writeFileSync('dist/build.json', JSON.stringify({env: process.env.NODE_ENV || 'development'}));\n")


def check_cmd_npm_scripts(root):
    t = _text(root, "dist/build.json")
    try:
        env = json.loads(t or "{}").get("env")
    except json.JSONDecodeError:
        env = None
    return env == "production", f"dist/build.json={t!r}"


SOLVE_CMD_NPM_SCRIPTS = r"""NODE_ENV=production node build.js
"""


TASKS = [
    {"id": "msvc-cmake-tests", "setup": setup_msvc_cmake, "check": check_msvc_cmake, "solution": SOLVE_MSVC_CMAKE,
     "timeout": 1500,
     "prompt": "This is a small C library built with CMake. Build it with the Visual Studio (MSVC) compiler "
               "installed on this machine, run its tests, and fix the bug in src/ that makes a test fail "
               "(don't change the tests). Save the output of the final, passing test run to test.log."},
    {"id": "bat-release", "setup": setup_bat_release, "check": check_bat_release, "solution": SOLVE_BAT_RELEASE,
     "prompt": "Run scripts\\release.bat to produce release 1.2.3 with the notes `Faster startup, 100% of tests` "
               "into the folder `dist/My App 1.2.3` (with the spaces). The script has a bug with folders like that; "
               "fix it. The result must be dist/My App 1.2.3/manifest.txt with the version and the notes."},
    {"id": "node-integration", "setup": setup_node_integration, "check": check_node_integration,
     "solution": SOLVE_NODE_INTEGRATION,
     "prompt": "Start this service in the background on port 8790 (it reads the PORT environment variable), "
               "run its integration tests against it (npm run test:int, with the same PORT), save the tests' "
               "output to integration.txt, then stop the service so nothing is left running on the port."},
    {"id": "python-venv", "setup": setup_python_venv, "check": check_python_venv, "solution": SOLVE_PYTHON_VENV,
     "prompt": "Follow the README: create the virtual environment in .venv, install the requirements into it, "
               "and run the tests. One fails; fix slugify.py (not the tests). Save the final test run's output "
               "to pytest.txt."},
    {"id": "unicode-report", "setup": setup_unicode_report, "check": check_unicode_report,
     "solution": SOLVE_UNICODE_REPORT,
     "prompt": "Using the sales in data/销售 2026-Q1.csv and the customer list in data/客户 名单.csv, write "
               "report.csv with the header `客户,城市,合计` and one line per customer: name, city and total sales, "
               "largest total first. Write it as UTF-8 without a BOM."},
    {"id": "git-crlf-merge", "setup": setup_git_crlf_merge, "check": check_git_crlf_merge,
     "solution": SOLVE_GIT_CRLF_MERGE,
     "prompt": "Merge the feature branch into main. config.ini will conflict: keep the feature branch's port "
               "and log file setting and main's host. The file uses Windows (CRLF) line endings; keep them. "
               "Commit the merge so the working tree is clean."},
    {"id": "dotnet-test", "setup": setup_dotnet_test, "check": check_dotnet_test, "solution": SOLVE_DOTNET_TEST,
     "timeout": 1500,
     "prompt": "Run this .NET solution's tests (Calc.Tests). One fails because of a bug in Calc; fix Calc, "
               "not the tests, and save the output of the final passing `dotnet test` run to test-results.txt."},
    {"id": "powershell-interop", "setup": setup_powershell_interop, "check": check_powershell_interop,
     "solution": SOLVE_POWERSHELL_INTEROP,
     "prompt": "tools/inventory.ps1 is our PowerShell inventory script. Run it on the folder `shared data` "
               "with JSON output, and write the total size in bytes of the files it lists to total.txt "
               "(just the number)."},
    {"id": "port-taken", "setup": setup_port_taken, "check": check_port_taken, "solution": SOLVE_PORT_TAKEN,
     "prompt": "Start the app with npm start (it listens on port 8791) and save the response of "
               "http://127.0.0.1:8791/ to status.txt. Something old may already be using that port; if so, "
               "find what and stop it first. Leave the app running."},
    {"id": "locked-build", "setup": setup_locked_build, "check": check_locked_build, "solution": SOLVE_LOCKED_BUILD,
     "prompt": "Do a clean build: delete the whole build folder, then run python build.py. If something keeps "
               "you from deleting it, find what and stop it."},
    {"id": "long-paths", "setup": setup_long_paths, "check": check_long_paths, "solution": SOLVE_LONG_PATHS,
     "prompt": "Package the whole src folder, with its directory structure, into dist/src.zip (paths in the zip "
               "starting with src/)."},
    {"id": "legacy-output", "setup": setup_legacy_output, "check": check_legacy_output,
     "solution": SOLVE_LEGACY_OUTPUT,
     "prompt": "Run the old exporter (see README) and save its output to customers.tsv as UTF-8, one customer "
               "per line, exactly as it lists them."},
    {"id": "cmd-npm-scripts", "setup": setup_cmd_npm_scripts, "check": check_cmd_npm_scripts,
     "solution": SOLVE_CMD_NPM_SCRIPTS,
     "prompt": "Run this project's production build (the build script in package.json) so that "
               "dist/build.json records the production environment."},
]
