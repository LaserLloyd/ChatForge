<#
.SYNOPSIS
  Removes ChatForge for the current Windows user. Your data is kept unless you add -Purge.

.DESCRIPTION
  Normally started by "Uninstall ChatForge.bat". Removes: the sign-in (autostart) entry, the Start Menu and
  Desktop shortcuts, the Settings > Installed apps entry, and the install folder (the app and its private
  Python libraries). Keeps: your settings, chats, downloaded models and the local runtime in
  %LOCALAPPDATA%\ChatForge, and the documents ChatForge made in Documents\ChatForge.

  -Purge also deletes %LOCALAPPDATA%\ChatForge (settings, chat history, models, runtime, logs) and the API keys
  ChatForge saved in Windows Credential Manager. Documents\ChatForge is never touched.

  uv and Python are shared tools and are left alone; "uv python uninstall 3.12" removes a uv-managed Python.

  Plain ASCII, Windows PowerShell 5.1 compatible.

.PARAMETER InstallDir       Where ChatForge is installed. Default %LOCALAPPDATA%\Programs\ChatForge.
.PARAMETER Purge            Also delete the data folder and the saved API keys.
.PARAMETER Yes              Ask nothing (keeps data unless -Purge is given).
.PARAMETER ShortcutDir      (testing) Where the installer put shortcuts instead of the real Start Menu / Desktop.
.PARAMETER DataHome         (testing) The data folder to use/purge instead of the default.
.PARAMETER UninstallKeyName (testing) Name of the Installed-apps registry entry. Default ChatForge.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = '',
  [switch]$Purge,
  [switch]$Yes,
  [string]$ShortcutDir = '',
  [string]$DataHome = '',
  [string]$UninstallKeyName = 'ChatForge'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Say([string]$text)  { Write-Host $text }
function Note([string]$text) { Write-Host ('  ' + $text) -ForegroundColor DarkGray }
function Warn([string]$text) { Write-Host ('  ! ' + $text) -ForegroundColor Yellow }
function Test-Interactive {
  try { return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) } catch { return $false }
}

if (-not $InstallDir) {
  # Where did the installer put it? Ask the Installed-apps entry it wrote, then use the default.
  $reg0 = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName"
  $recorded = $null
  if (Test-Path $reg0) { $recorded = (Get-ItemProperty -Path $reg0 -ErrorAction SilentlyContinue).InstallLocation }
  if ($recorded) { $InstallDir = $recorded } else { $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\ChatForge' }
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if ($DataHome) { $env:CHATFORGE_HOME = [IO.Path]::GetFullPath($DataHome) }
$dataDir = if ($env:CHATFORGE_HOME) { $env:CHATFORGE_HOME } else { Join-Path $env:LOCALAPPDATA 'ChatForge' }
$dataDir = [IO.Path]::GetFullPath($dataDir).TrimEnd('\')
$AppDir = Join-Path $InstallDir 'app'
$venvPy = Join-Path $AppDir '.venv\Scripts\python.exe'
$infoFile = Join-Path $InstallDir 'install.json'

function Get-InstallProcesses([string]$dir) {
  # Programs started from the install folder, plus their children (a venv's pythonw.exe launcher starts the
  # real interpreter, which may live elsewhere, as a child; the local AI runtime is a child of that).
  $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
  $ids = New-Object 'System.Collections.Generic.HashSet[uint32]'
  foreach ($p in $all) {
    if ($p.ExecutablePath -and $p.ExecutablePath.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase)) { [void]$ids.Add([uint32]$p.ProcessId) }
  }
  do {
    $before = $ids.Count
    foreach ($p in $all) { if ($ids.Contains([uint32]$p.ParentProcessId)) { [void]$ids.Add([uint32]$p.ProcessId) } }
  } while ($ids.Count -gt $before)
  return @($all | Where-Object { $ids.Contains([uint32]$_.ProcessId) -and $_.ProcessId -ne $PID })
}

# Never delete something that is not plausibly ours.
function Assert-SafeToDelete([string]$path, [string]$what) {
  $full = [IO.Path]::GetFullPath($path).TrimEnd('\')
  $root = [IO.Path]::GetPathRoot($full).TrimEnd('\')
  $profileDir = [Environment]::GetFolderPath('UserProfile').TrimEnd('\')
  if ($full -ieq $root -or $full -ieq $profileDir -or $full.Length -lt 12) { throw "Refusing to delete $what at '$full'." }
}

Say ''
Say 'ChatForge uninstaller'
Say '---------------------'
if (-not (Test-Path $InstallDir)) {
  Say "ChatForge is not installed in $InstallDir (nothing to remove there)."
} else {
  Say "Removing the app from: $InstallDir"
}
Say $(if ($Purge) { "Your data in $dataDir WILL be deleted (-Purge)." } else { "Your data in $dataDir will be kept." })

if ($Purge -and -not $Yes -and (Test-Interactive)) {
  $reply = Read-Host '  Really delete your ChatForge settings, chat history, downloaded models and saved API keys? Type YES to confirm'
  if ($reply -ne 'YES') { Say 'Cancelled. Nothing was deleted.'; exit 2 }
}

$exitCode = 0
try {
  # 1. Stop ChatForge if it runs from the install folder.
  $running = @(Get-InstallProcesses $InstallDir)
  if ($running.Count -gt 0) {
    Note "Closing ChatForge ($($running.Count) process(es))."
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
  }

  # 2. The sign-in entry (through the app itself, which knows what it registered).
  if (Test-Path $venvPy) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = & $venvPy -I -m chatforge autostart disable --no-record 2>&1 | Out-String } finally { $ErrorActionPreference = $prev }
    Note ('Sign-in entry: ' + $out.Trim())
  } else {
    # No app Python to ask. The logon task is named ChatForge for every install, so only remove it when it
    # runs THIS data folder's ChatForge.vbs (never another copy's).
    $xml = (& schtasks.exe /Query /TN ChatForge /XML 2>$null | Out-String)
    $vbs = Join-Path $dataDir 'ChatForge.vbs'
    if ($xml -and $xml.IndexOf($vbs, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
      [void](& schtasks.exe /Delete /TN ChatForge /F 2>$null)
      Note 'Sign-in entry: removed the ChatForge logon task.'
    } elseif ($xml) {
      Warn 'A ChatForge sign-in entry exists but belongs to another copy of ChatForge, so it was left alone.'
    } else {
      Note 'Sign-in entry: none.'
    }
  }

  # 3. Shortcuts: only ones that point into the install folder.
  $dirs = @()
  if ($ShortcutDir) { $dirs = @((Join-Path $ShortcutDir 'StartMenu'), (Join-Path $ShortcutDir 'Desktop')) }
  else { $dirs = @((Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'), [Environment]::GetFolderPath('Desktop')) }
  $shell = New-Object -ComObject WScript.Shell
  foreach ($d in $dirs) {
    $lnk = Join-Path $d 'ChatForge.lnk'
    if (Test-Path $lnk) {
      $target = $shell.CreateShortcut($lnk).TargetPath
      if ($target -and $target.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item $lnk -Force
        Note "Removed $lnk"
      } else {
        Warn "Left $lnk alone: it does not point into $InstallDir."
      }
    }
  }

  # 4. Installed-apps entry (only if it is ours).
  $reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName"
  if (Test-Path $reg) {
    $loc = (Get-ItemProperty -Path $reg -ErrorAction SilentlyContinue).InstallLocation
    if ($loc -and ([IO.Path]::GetFullPath($loc).TrimEnd('\') -ieq $InstallDir)) {
      Remove-Item $reg -Recurse -Force
      Note 'Removed the Settings > Installed apps entry.'
    } else {
      Warn 'Left the Installed apps entry alone: it belongs to another install.'
    }
  }

  # 5. The data, only with -Purge.
  if ($Purge) {
    Assert-SafeToDelete $dataDir 'the data folder'
    if (Test-Path $dataDir) {
      Remove-Item $dataDir -Recurse -Force
      Note "Deleted $dataDir"
    }
    $listing = (& cmdkey.exe /list 2>$null | Out-String)
    foreach ($m in [regex]::Matches($listing, 'Target:\s*(\S+@ChatForge)\b')) {
      [void](& cmdkey.exe "/delete:$($m.Groups[1].Value)" 2>$null)
      Note 'Deleted a saved API key from Windows Credential Manager.'
    }
  }

  # 6. The app. This script may itself run from inside the folder, so delete after a short pause, from a copy.
  if (Test-Path $InstallDir) {
    Assert-SafeToDelete $InstallDir 'the install folder'
    $self = $MyInvocation.MyCommand.Path
    $inside = $self -and $self.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase)
    if ($inside) {
      # Started from the copy in the install folder: finish from a temp cmd so the folder can go.
      $cmd = 'ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{0}"' -f $InstallDir
      Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', $cmd) -WindowStyle Hidden
      Note "Removing $InstallDir in a moment."
    } else {
      Remove-Item $InstallDir -Recurse -Force
      Note "Removed $InstallDir"
    }
  }

  Say ''
  Write-Host 'ChatForge is removed.' -ForegroundColor Green
  if (-not $Purge) {
    Say "Your settings, chats and models are still in $dataDir."
    Say 'Delete that folder by hand, or run "Uninstall ChatForge.bat --purge", if you want them gone too.'
  }
}
catch {
  $exitCode = 1
  Write-Host ''
  Write-Host 'The uninstall did not finish.' -ForegroundColor Red
  Write-Host ('  ' + $_.Exception.Message) -ForegroundColor Red
  Write-Host '  Whatever was already removed stays removed. You can run it again.'
}
exit $exitCode
