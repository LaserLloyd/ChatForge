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
function Get-Quiet([string]$exe, [string[]]$arguments) {
  # A native tool's stdout, or '' when it fails. Its stderr ("ERROR: The system cannot find the file
  # specified." when there is no such task) must not become a terminating error under 'Stop' in PowerShell 5.1.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { return ((& $exe @arguments 2>$null) | Out-String) } catch { return '' } finally { $ErrorActionPreference = $prev }
}
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

function Select-InstallProcesses([string]$dir, $all, [uint32]$self) {
  # Programs started from the install folder, plus their children (a venv's pythonw.exe launcher starts the
  # real interpreter, which may live elsewhere, as a child; the local AI runtime is a child of that).
  # "From the install folder" means below "<dir>\", never a sibling such as "<dir>2" or "<dir> Beta". A child
  # counts only when it started after its parent, so a recycled process id never pulls in an unrelated program.
  $prefix = $dir.TrimEnd('\') + '\'
  $byId = @{}
  foreach ($p in $all) { $byId[[uint32]$p.ProcessId] = $p }
  $ids = New-Object 'System.Collections.Generic.HashSet[uint32]'
  foreach ($p in $all) {
    if ($p.ExecutablePath -and $p.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { [void]$ids.Add([uint32]$p.ProcessId) }
  }
  do {
    $before = $ids.Count
    foreach ($p in $all) {
      $id = [uint32]$p.ProcessId
      $parentId = [uint32]$p.ParentProcessId
      if ($ids.Contains($id) -or -not $ids.Contains($parentId)) { continue }
      $parent = $byId[$parentId]
      if ($parent -and $p.CreationDate -and $parent.CreationDate -and $p.CreationDate -ge $parent.CreationDate) { [void]$ids.Add($id) }
    }
  } while ($ids.Count -gt $before)
  return @($all | Where-Object { $ids.Contains([uint32]$_.ProcessId) -and [uint32]$_.ProcessId -ne $self })
}

function Get-InstallProcesses([string]$dir) {
  return @(Select-InstallProcesses $dir @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) $PID)
}

# What the installer puts in the install folder. Anything else there is not ours: the installer refuses such a
# folder and the uninstaller never deletes it.
$OwnItems = @('app', 'tools', 'logs', 'uninstall', 'ChatForge.ico', 'install.json')
function Get-ForeignItems([string]$dir) {
  if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return @() }
  return @(Get-ChildItem -LiteralPath $dir -Force | Where-Object { $OwnItems -notcontains $_.Name } | ForEach-Object { $_.Name })
}

# The Credential Manager entries ChatForge and its voice add-on saved, from "cmdkey /list" output. keyring keeps
# the most recently saved key under the bare service name ("ChatForge") and older ones as "<provider>@ChatForge";
# the voice add-on's service is "ChatForge Voice". Matched on the "LegacyGeneric:target=" part, which Windows
# does not translate (the word "Target:" is).
function Select-ChatForgeCredentials([string]$listing) {
  $out = @()
  foreach ($line in ($listing -split "`r?`n")) {
    if ($line -match 'LegacyGeneric:target=((?:[^@]+@)?ChatForge(?: Voice)?)\s*$') { $out += ('LegacyGeneric:target=' + $Matches[1]) }
  }
  return $out
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
    $xml = (Get-Quiet 'schtasks.exe' @('/Query', '/TN', 'ChatForge', '/XML')).Trim()
    $vbs = Join-Path $dataDir 'ChatForge.vbs'
    if ($xml -and $xml.IndexOf($vbs, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
      [void](Get-Quiet 'schtasks.exe' @('/Delete', '/TN', 'ChatForge', '/F'))
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
      if ($target -and $target.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
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
    $listing = Get-Quiet 'cmdkey.exe' @('/list')
    foreach ($target in @(Select-ChatForgeCredentials $listing)) {
      [void](Get-Quiet 'cmdkey.exe' @("/delete:$target"))
      Note 'Deleted a saved API key from Windows Credential Manager.'
    }
  }

  # 6. The app. This script may itself run from inside the folder, so delete after a short pause, from a copy.
  # Only what the installer put there ($OwnItems); the folder itself goes only when nothing else is left in it
  # (an install into an existing folder such as D:\Apps must never take that folder's other contents along).
  if (Test-Path -LiteralPath $InstallDir) {
    Assert-SafeToDelete $InstallDir 'the install folder'
    $foreign = @(Get-ForeignItems $InstallDir)
    $self = $MyInvocation.MyCommand.Path
    $inside = $self -and $self.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase)
    if ($inside) {
      # Started from the copy in the install folder: finish from a temp cmd so the folder can go.
      $parts = @('ping 127.0.0.1 -n 3 >nul')
      foreach ($name in $OwnItems) {
        $item = Join-Path $InstallDir $name
        if (Test-Path -LiteralPath $item -PathType Container) { $parts += ('rmdir /s /q "{0}"' -f $item) }
        elseif (Test-Path -LiteralPath $item) { $parts += ('del /f /q "{0}"' -f $item) }
      }
      $parts += ('rmdir "{0}"' -f $InstallDir)  # without /s: only when it is empty
      Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', ($parts -join ' & ')) -WindowStyle Hidden
      Note "Removing $InstallDir in a moment."
    } else {
      foreach ($name in $OwnItems) {
        $item = Join-Path $InstallDir $name
        if (Test-Path -LiteralPath $item) { Remove-Item -LiteralPath $item -Recurse -Force }
      }
      if ($foreign.Count -eq 0) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
        Note "Removed $InstallDir"
      }
    }
    if ($foreign.Count -gt 0) {
      Warn "Kept the folder $InstallDir itself: it also holds files ChatForge did not put there ($(($foreign | Select-Object -First 3) -join ', '))."
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
