# Downloads sill's Windows build into $env:RUNNER_TEMP\sill and prints the path of bash.exe.
# -Tag picks a release of github.com/lemonadesaltbagel/sill (empty: the latest), using
# GH_TOKEN (the workflow's own token is enough). -Tag nightly is the prerelease build of
# sill's current source, which needs SILL_DOWNLOAD_TOKEN.
param([string] $Tag = '')
$ErrorActionPreference = 'Stop'
if ($Tag -eq 'nightly') {
    if (-not $env:SILL_DOWNLOAD_TOKEN) { throw 'SILL_DOWNLOAD_TOKEN is not set (forks and pull requests from forks do not get it)' }
    $env:GH_TOKEN = $env:SILL_DOWNLOAD_TOKEN
    $repo = 'lemonadesaltbagel/sill-bash'
} else {
    $repo = 'lemonadesaltbagel/sill'
}
$dest = Join-Path $env:RUNNER_TEMP 'sill'
$ghArgs = @('release', 'download')
if ($Tag) { $ghArgs += $Tag }
$ghArgs += @('-R', $repo, '-p', 'sill-windows-x64.zip', '-D', $dest, '--clobber')
gh @ghArgs
if ($LASTEXITCODE) { throw 'download failed' }
Expand-Archive (Join-Path $dest 'sill-windows-x64.zip') $dest -Force
$bash = Get-ChildItem $dest -Recurse -Filter bash.exe | Where-Object { $_.Directory.Name -eq 'sill' } | Select-Object -First 1
if (-not $bash) { throw 'no sill\bash.exe in the zip' }
$bash.FullName
