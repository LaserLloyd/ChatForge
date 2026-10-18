<#
.SYNOPSIS
  Builds ChatForge-<version>-windows.zip: the download for people who do not use git.

.DESCRIPTION
  The zip holds
    Install ChatForge.bat / Uninstall ChatForge.bat   (double-click)
    README.txt                                         (three lines of instructions)
    deploy\windows\*.ps1                               (what the .bat files run)
    app\                                               (the application source, tracked files only)
  and is self-contained apart from what the installer downloads (uv, Python 3.12, the libraries).
  A SHA-256 file is written next to it. Run it from anywhere inside the repository:

    powershell -ExecutionPolicy Bypass -File deploy\windows\build-release-zip.ps1

  Only files tracked by git go in (an untracked local file, such as a stray .env or config.toml, never
  does), from the working tree, so build from a clean checkout of the tag you are releasing.

.PARAMETER OutDir  Where the zip goes. Default <repo>\dist.
#>
[CmdletBinding()]
param([string]$OutDir = '')

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $OutDir) { $OutDir = Join-Path $repo 'dist' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$version = (Select-String -Path (Join-Path $repo 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1).Matches[0].Groups[1].Value
$name = "ChatForge-$version-windows"
$stage = Join-Path ([IO.Path]::GetTempPath()) ("$name-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$root = Join-Path $stage $name
$appDir = Join-Path $root 'app'
New-Item -ItemType Directory -Force -Path $appDir | Out-Null

try {
  $files = & git -C $repo ls-files --cached
  if ($LASTEXITCODE -ne 0 -or -not $files) { throw 'git ls-files failed: build from a git checkout.' }
  foreach ($rel in $files) {
    $src = Join-Path $repo $rel
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
    $dest = Join-Path $appDir $rel
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dest
  }
  # Batch files and PowerShell scripts need Windows line endings to run reliably.
  Get-ChildItem -Path $appDir -Recurse -Include *.bat, *.ps1, *.iss | ForEach-Object {
    $text = [IO.File]::ReadAllText($_.FullName)
    [IO.File]::WriteAllText($_.FullName, ($text -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $false))
  }

  # The top level: the two double-click files, the scripts they run, and the instructions.
  Copy-Item -LiteralPath (Join-Path $appDir 'Install ChatForge.bat') -Destination $root
  Copy-Item -LiteralPath (Join-Path $appDir 'Uninstall ChatForge.bat') -Destination $root
  $dep = Join-Path $root 'deploy\windows'
  New-Item -ItemType Directory -Force -Path $dep | Out-Null
  Copy-Item -LiteralPath (Join-Path $appDir 'deploy\windows\Install-ChatForge.ps1') -Destination $dep
  Copy-Item -LiteralPath (Join-Path $appDir 'deploy\windows\Uninstall-ChatForge.ps1') -Destination $dep
  Copy-Item -LiteralPath (Join-Path $appDir 'deploy\windows\zip-readme.txt') -Destination (Join-Path $root 'README.txt')

  $zip = Join-Path $OutDir "$name.zip"
  if (Test-Path $zip) { Remove-Item $zip -Force }
  Compress-Archive -Path $root -DestinationPath $zip
  # .NET directly: Get-FileHash is not found by Windows PowerShell 5.1 when a PowerShell 7 host started it.
  $sha = [Security.Cryptography.SHA256]::Create()
  $stream = [IO.File]::OpenRead((Resolve-Path -LiteralPath $zip).ProviderPath)
  try { $hash = (([BitConverter]::ToString($sha.ComputeHash($stream))) -replace '-', '').ToLowerInvariant() }
  finally { $stream.Dispose(); $sha.Dispose() }
  Set-Content -Path "$zip.sha256" -Value "$hash *$name.zip" -Encoding ASCII
  Write-Host "Built $zip ($([int]((Get-Item $zip).Length / 1KB)) KB)"
  Write-Host "SHA-256 $hash"
} finally {
  if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
