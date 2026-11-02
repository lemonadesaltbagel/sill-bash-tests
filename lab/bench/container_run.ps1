# Runs lab/agent-e2e/run.py inside a Windows container whose system locale is set (see the
# winbench-agent-zh workflow). The host's Git, Node, Python, .NET and sill are mounted at their
# usual paths; this puts them on PATH, installs the harness and starts the runner with the
# arguments in $env:RUN_ARGS.
$ErrorActionPreference = 'Stop'
$env:PATH = @(
    $env:HOST_PYTHON, "$env:HOST_PYTHON\Scripts", 'C:\Program Files\nodejs', "$env:APPDATA\npm",
    'C:\Program Files\Git\cmd', 'C:\Program Files\dotnet', $env:PATH
) -join ';'
Write-Output "code page: $([Text.Encoding]::Default.CodePage); culture: $((Get-Culture).Name)"
switch ($env:HARNESS) {
    'claude' { npm install -g @anthropic-ai/claude-code@latest 2>&1 | Select-Object -Last 2 }
    'opencode' { npm install -g opencode-ai@latest 2>&1 | Select-Object -Last 2 }
    default { throw "harness $env:HARNESS isn't set up for the container" }
}
$runArgs = $env:RUN_ARGS -split '\|'
& python lab\agent-e2e\run.py @runArgs
exit $LASTEXITCODE
