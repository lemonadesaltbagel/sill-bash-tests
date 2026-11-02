# Runs lab/agent-e2e/run.py inside a Windows container whose system locale is set (see the
# winbench-agent-zh workflow). The host's Git, Node, Python, .NET and sill are mounted at their
# usual paths; this puts them on PATH, installs the harness and starts the runner with the
# arguments in $env:RUN_ARGS.
# Not 'Stop': Windows PowerShell 5.1 makes a native program's stderr (npm's notices) an error.
$ErrorActionPreference = 'Continue'
$env:PATH = @(
    $env:HOST_PYTHON, "$env:HOST_PYTHON\Scripts", 'C:\Program Files\nodejs', "$env:APPDATA\npm",
    'C:\Program Files\Git\cmd', 'C:\Program Files\dotnet', $env:PATH
) -join ';'
Write-Output "code page: $([Text.Encoding]::Default.CodePage); culture: $((Get-Culture).Name)"
$package = @{ claude = '@anthropic-ai/claude-code@latest'; opencode = 'opencode-ai@latest' }[$env:HARNESS]
if (-not $package) { Write-Output "harness $env:HARNESS isn't set up for the container"; exit 2 }
cmd /c "npm install -g $package 2>&1"
if ($LASTEXITCODE) { Write-Output "installing $package failed"; exit $LASTEXITCODE }
$runArgs = $env:RUN_ARGS -split '\|'
& python lab\agent-e2e\run.py @runArgs
exit $LASTEXITCODE
