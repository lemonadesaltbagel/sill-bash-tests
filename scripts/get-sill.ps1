# Downloads sill's Windows build into $env:RUNNER_TEMP\sill and prints the path of bash.exe.
# -Tag picks a release of github.com/lemonadesaltbagel/sill (empty: the latest), using
# GH_TOKEN (the workflow's own token is enough). -Tag nightly is the prerelease build of
# sill's current source, which needs SILL_DOWNLOAD_TOKEN. -Tag run:ID is the build a run of
# sill's build workflow made (its sill-windows-x64 artifact): a change before its release.
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
if ($Tag -like 'run:*') {
    gh run download $Tag.Substring(4) -R $repo -n sill-windows-x64 -D $dest
} else {
    $ghArgs = @('release', 'download')
    if ($Tag) { $ghArgs += $Tag }
    $ghArgs += @('-R', $repo, '-p', 'sill-windows-x64.zip', '-D', $dest, '--clobber')
    gh @ghArgs
}
if ($LASTEXITCODE) { throw 'download failed' }
$zip = Get-ChildItem $dest -Recurse -Filter sill-windows-x64.zip | Select-Object -First 1
if (-not $zip) { throw 'no sill-windows-x64.zip downloaded' }
Expand-Archive $zip.FullName $dest -Force
$bash = Get-ChildItem $dest -Recurse -Filter bash.exe | Where-Object { $_.Directory.Name -eq 'sill' } | Select-Object -First 1
if (-not $bash) { throw 'no sill\bash.exe in the zip' }
$bash.FullName
