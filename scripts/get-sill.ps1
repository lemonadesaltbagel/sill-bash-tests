# Downloads sill's Windows build from its (private) release into $env:RUNNER_TEMP\sill and
# prints the path of bash.exe. Needs SILL_DOWNLOAD_TOKEN (a read-only token for the sill
# repository). -Tag picks a release; empty means the latest.
param([string] $Tag = '')
$ErrorActionPreference = 'Stop'
if (-not $env:SILL_DOWNLOAD_TOKEN) { throw 'SILL_DOWNLOAD_TOKEN is not set (forks and pull requests from forks do not get it)' }
$env:GH_TOKEN = $env:SILL_DOWNLOAD_TOKEN
$dest = Join-Path $env:RUNNER_TEMP 'sill'
$args = @('release', 'download')
if ($Tag) { $args += $Tag }
$args += @('-R', 'lemonadesaltbagel/sill-bash', '-p', 'sill-windows-x64.zip', '-D', $dest, '--clobber')
gh @args
if ($LASTEXITCODE) { throw 'download failed' }
Expand-Archive (Join-Path $dest 'sill-windows-x64.zip') $dest -Force
$bash = Get-ChildItem $dest -Recurse -Filter bash.exe | Where-Object { $_.Directory.Name -eq 'sill' } | Select-Object -First 1
if (-not $bash) { throw 'no sill\bash.exe in the zip' }
$bash.FullName
