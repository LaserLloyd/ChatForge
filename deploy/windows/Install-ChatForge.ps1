<#
.SYNOPSIS
  Installs (or upgrades) ChatForge for the current Windows user. No administrator rights, no terminal needed.

.DESCRIPTION
  Normally started by "Install ChatForge.bat" (double-click). Steps, each skipped when already done:
    1. check this PC (Windows, 64-bit, free disk space)
    2. uv: use the one on PATH, else download a pinned, hash-checked copy (winget as the fallback)
    3. Python 3.12: use one that is there, else let uv fetch it (winget / python.org as fallbacks)
    4. the app: copy it from this folder (release zip or git clone), else git clone, else download the source
    5. "uv sync --locked": the app's own private environment inside the install folder
    6. Start Menu and Desktop shortcuts, and an entry in Settings > Installed apps (current user only)
    7. ask once whether to start ChatForge when you sign in (default: yes), then open ChatForge

  Run it again to upgrade: the app files are replaced, your settings, chats and models (in
  %LOCALAPPDATA%\ChatForge) are never touched. Uninstall with "Uninstall ChatForge.bat".

  This file is deliberately plain ASCII and Windows PowerShell 5.1 compatible (the one every
  Windows 11 PC has).

.PARAMETER InstallDir      Where the app goes. Default %LOCALAPPDATA%\Programs\ChatForge.
.PARAMETER SourceDir       A folder that holds the app (pyproject.toml). Default: look beside this script, else download.
.PARAMETER Repo            Where to download the app from when no SourceDir is found.
.PARAMETER Ref             Branch or tag to download (default main).
.PARAMETER Yes             Ask nothing: start at sign-in unless -NoAutostart is given.
.PARAMETER NoAutostart     Do not ask and do not register the sign-in entry (recorded as "no", so the app does not ask either).
.PARAMETER NoLaunch        Do not open ChatForge at the end.
.PARAMETER NoShortcuts     Do not create Start Menu / Desktop shortcuts.
.PARAMETER NoDesktopIcon    Create the Start Menu shortcut only.
.PARAMETER ShortcutDir     (testing) Put the shortcuts under this folder instead of the real Start Menu and Desktop.
.PARAMETER DataHome        (testing) Use this folder as ChatForge's data folder (CHATFORGE_HOME) for the steps below.
.PARAMETER UninstallKeyName (testing) Name of the Installed-apps registry entry. Default ChatForge.
.PARAMETER ForceDownloadUv (diagnostic) Ignore any uv on PATH and use a private copy in the install folder.
.PARAMETER ForceManagedPython (diagnostic) Ignore Pythons already on this PC and let uv fetch its own 3.12.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = '',
  [string]$SourceDir = '',
  [string]$Repo = 'https://github.com/LaserLloyd/ChatForge',
  [string]$Ref = 'main',
  [switch]$Yes,
  [switch]$NoAutostart,
  [switch]$NoLaunch,
  [switch]$NoShortcuts,
  [switch]$NoDesktopIcon,
  [string]$ShortcutDir = '',
  [string]$DataHome = '',
  [string]$UninstallKeyName = 'ChatForge',
  [switch]$ForceDownloadUv,
  [switch]$ForceManagedPython
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is 10x slower with the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

# --- pinned downloads (checked before they are used) -------------------------------------------
$UvVersion = '0.12.23'
$UvUrl     = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvSha256  = '75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90'
$PythonSeries = '3.12'
$PythonOrgUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$MinFreeBytes = 1.5GB

# --- small helpers -------------------------------------------------------------------------------
function Say([string]$text)  { Write-Host $text }
function Step([int]$n, [string]$text) { Write-Host ''; Write-Host ("[{0}/7] {1}" -f $n, $text) -ForegroundColor Cyan }
function Note([string]$text) { Write-Host ('      ' + $text) -ForegroundColor DarkGray }
function Warn([string]$text) { Write-Host ('      ! ' + $text) -ForegroundColor Yellow }
function Fail([string]$text) { throw $text }

function Test-Interactive {
  try { return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) } catch { return $false }
}

function Get-Download([string]$url, [string]$dest) {
  $last = $null
  foreach ($attempt in 1..3) {
    try {
      if (Test-Path $dest) { Remove-Item $dest -Force }
      Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
      return
    } catch {
      $last = $_
      Start-Sleep -Seconds (2 * $attempt)
    }
  }
  Fail ("Could not download $url`n      $($last.Exception.Message)`n      Check the internet connection (a proxy or a firewall can block it) and run the installer again.")
}

function Test-Sha256([string]$path, [string]$expected) {
  return ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -eq $expected.ToLowerInvariant())
}

function Invoke-Native([string]$exe, [string[]]$arguments, [switch]$Quiet) {
  # Runs a program, returns its exit code; output is shown unless -Quiet.
  # (A native tool's stderr is not an error to PowerShell 5.1; only the exit code is.)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    if ($Quiet) { & $exe @arguments *> $null } else { & $exe @arguments 2>&1 | ForEach-Object { Write-Host ('      ' + $_) } }
    return $LASTEXITCODE
  } finally { $ErrorActionPreference = $prev }
}

function Get-NativeOutput([string]$exe, [string[]]$arguments) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $exe @arguments 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return (($out | Out-String).Trim())
  } catch { return $null } finally { $ErrorActionPreference = $prev }
}

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

function Add-ToProcessPath([string]$dir) {
  if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$dir;$env:Path" }
}

# --- 0. paths --------------------------------------------------------------------------------------
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\ChatForge' }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$AppDir   = Join-Path $InstallDir 'app'
$ToolsDir = Join-Path $InstallDir 'tools'
$LogDir   = Join-Path $InstallDir 'logs'
$TempDir  = Join-Path ([IO.Path]::GetTempPath()) ('chatforge-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($DataHome) { $env:CHATFORGE_HOME = [IO.Path]::GetFullPath($DataHome) }
if ($ForceManagedPython) { $env:UV_PYTHON_PREFERENCE = 'only-managed' }

New-Item -ItemType Directory -Force -Path $InstallDir, $LogDir | Out-Null
$Transcript = Join-Path $LogDir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
try { Start-Transcript -Path $Transcript | Out-Null } catch { $Transcript = $null }

$exitCode = 0
try {
  Say ''
  Say 'ChatForge installer'
  Say '-------------------'
  Say "Installing for the current user into:  $InstallDir"
  Say 'No administrator rights are needed. Your settings, chats and models are never deleted by this installer.'

  # ---- 1. this PC --------------------------------------------------------------------------------
  Step 1 'Checking this PC'
  if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Fail 'ChatForge is a Windows app. This installer only runs on Windows.' }
  $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
  if ($arch -ne 'AMD64') {
    Fail "This PC is $arch. ChatForge's local AI runtime (OpenVINO Model Server) only has 64-bit Intel/AMD Windows builds, so the installer stops here."
  }
  $drive = (Get-Item $InstallDir).PSDrive
  if ($drive.Free -lt $MinFreeBytes) {
    Fail ("Not enough free disk space on {0}: {1:N1} GB free, 1.5 GB needed for the app (the AI model needs about 1.1 GB more)." -f $drive.Name, ($drive.Free / 1GB))
  }
  Note ('Windows, 64-bit, {0:N0} GB free on {1}:' -f ($drive.Free / 1GB), $drive.Name)

  # Stop a running ChatForge that lives in this install folder (its files would be locked).
  $running = @(Get-InstallProcesses $InstallDir)
  if ($running.Count -gt 0) {
    Note "Closing the running ChatForge ($($running.Count) process(es)) so it can be upgraded..."
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
  }

  # ---- 2. uv ---------------------------------------------------------------------------------------
  Step 2 'Finding or installing uv (the tool that sets up Python and the app)'
  $uv = $null
  $private = Join-Path $ToolsDir 'uv.exe'
  if (-not $ForceDownloadUv) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $uv = $cmd.Source }
    elseif (Test-Path (Join-Path $env:USERPROFILE '.local\bin\uv.exe')) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
  }
  if (-not $uv -and (Test-Path $private)) {
    $have = Get-NativeOutput $private @('--version')
    if ($have -and $have -match [regex]::Escape($UvVersion)) { $uv = $private }
  }
  if (-not $uv) {
    New-Item -ItemType Directory -Force -Path $TempDir, $ToolsDir | Out-Null
    $zip = Join-Path $TempDir 'uv.zip'
    Note "Downloading uv $UvVersion (about 18 MB) from github.com/astral-sh/uv ..."
    try {
      Get-Download $UvUrl $zip
      if (-not (Test-Sha256 $zip $UvSha256)) { Fail 'The downloaded uv file does not match its published SHA-256 checksum, so it was not used.' }
      Expand-Archive -LiteralPath $zip -DestinationPath $TempDir -Force
      $found = Get-ChildItem -Path $TempDir -Recurse -Filter 'uv.exe' | Select-Object -First 1
      if (-not $found) { Fail 'uv.exe was not in the downloaded archive.' }
      Copy-Item $found.FullName $private -Force
      $uv = $private
      Note 'Checked the SHA-256 of the download before using it.'
    } catch {
      Warn "Direct download failed: $($_.Exception.Message)"
      $winget = Get-Command winget -ErrorAction SilentlyContinue
      if (-not $winget) { throw }
      Note 'Trying winget instead ...'
      [void](Invoke-Native $winget.Source @('install', '--id', 'astral-sh.uv', '-e', '--scope', 'user', '--accept-source-agreements', '--accept-package-agreements'))
      $cmd = Get-Command uv -ErrorAction SilentlyContinue
      if ($cmd) { $uv = $cmd.Source } elseif (Test-Path (Join-Path $env:USERPROFILE '.local\bin\uv.exe')) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
      if (-not $uv) { Fail 'uv could not be installed (neither the direct download nor winget worked).' }
    }
  }
  $uvVer = Get-NativeOutput $uv @('--version')
  if (-not $uvVer) { Fail "uv was found at $uv but does not run." }
  Add-ToProcessPath (Split-Path -Parent $uv)
  Note "$uvVer  ($uv)"

  # ---- 3. Python -------------------------------------------------------------------------------------
  Step 3 "Finding or installing Python $PythonSeries"
  $python = Get-NativeOutput $uv @('python', 'find', $PythonSeries)
  if (-not $python) {
    Note 'Python 3.12 is not on this PC. Letting uv fetch a private copy (about 30 MB, no admin, nothing else on the PC changes) ...'
    $code = Invoke-Native $uv @('python', 'install', $PythonSeries)
    $python = Get-NativeOutput $uv @('python', 'find', $PythonSeries)
    if ($code -ne 0 -or -not $python) {
      Warn 'uv could not fetch Python.'
      $winget = Get-Command winget -ErrorAction SilentlyContinue
      if ($winget) {
        Note 'Trying winget (Python.Python.3.12, current user) ...'
        [void](Invoke-Native $winget.Source @('install', '--id', 'Python.Python.3.12', '-e', '--scope', 'user', '--accept-source-agreements', '--accept-package-agreements'))
        $python = Get-NativeOutput $uv @('python', 'find', $PythonSeries)
      }
      if (-not $python) {
        Note 'Trying the python.org installer (current user only, signature checked) ...'
        New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
        $exe = Join-Path $TempDir 'python-installer.exe'
        Get-Download $PythonOrgUrl $exe
        $sig = Get-AuthenticodeSignature -LiteralPath $exe
        if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
          Fail 'The python.org installer did not carry a valid Python Software Foundation signature, so it was not run.'
        }
        [void](Invoke-Native $exe @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0'))
        $python = Get-NativeOutput $uv @('python', 'find', $PythonSeries)
      }
      if (-not $python) { Fail 'Python 3.12 could not be installed. See docs\SETUP.md for installing it by hand, then run this installer again.' }
    }
  }
  Note "$(Get-NativeOutput $python @('--version'))  ($python)"

  # ---- 4. the app --------------------------------------------------------------------------------------
  Step 4 'Putting the app in place'
  if (-not $SourceDir) {
    foreach ($candidate in @((Join-Path $ScriptDir '..\..\app'), (Join-Path $ScriptDir '..\..'), (Join-Path $ScriptDir 'app'))) {
      if (Test-Path (Join-Path $candidate 'pyproject.toml')) { $SourceDir = (Resolve-Path $candidate).Path; break }
    }
  }
  $source = $SourceDir
  $sourceNote = ''
  if ($source) {
    if (-not (Test-Path (Join-Path $source 'pyproject.toml'))) { Fail "-SourceDir $source has no pyproject.toml; it is not a ChatForge folder." }
    $source = (Resolve-Path $source).Path
    $sourceNote = "copied from $source"
  } else {
    New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
    $cloned = Join-Path $TempDir 'clone'
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
      Note "Downloading ChatForge ($Ref) with git ..."
      $code = Invoke-Native $git.Source @('clone', '--depth', '1', '--branch', $Ref, "$Repo.git", $cloned) -Quiet
      if ($code -eq 0 -and (Test-Path (Join-Path $cloned 'pyproject.toml'))) { $source = $cloned; $sourceNote = "git clone of $Repo ($Ref)" }
    }
    if (-not $source) {
      Note "Downloading ChatForge ($Ref) from github.com ..."
      $zip = Join-Path $TempDir 'app.zip'
      Get-Download "$Repo/archive/$Ref.zip" $zip
      Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $TempDir 'zip') -Force
      $top = Get-ChildItem -Path (Join-Path $TempDir 'zip') -Directory | Select-Object -First 1
      if (-not $top -or -not (Test-Path (Join-Path $top.FullName 'pyproject.toml'))) { Fail 'The downloaded archive is not a ChatForge source tree.' }
      $source = $top.FullName
      $sourceNote = "source archive of $Repo ($Ref)"
    }
  }
  $sameFolder = ([IO.Path]::GetFullPath($source).TrimEnd('\') -ieq [IO.Path]::GetFullPath($AppDir).TrimEnd('\'))
  if (-not $sameFolder) {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    # /MIR makes app\ an exact copy but keeps .venv (excluded on both sides); never copies .git or test data.
    $rc = Invoke-Native 'robocopy.exe' @($source, $AppDir, '/MIR', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:2', '/W:1',
      '/XD', '.venv', '.git', 'node_modules', '.scratch-data', '__pycache__', '.pytest_cache', '.ruff_cache',
      '/XF', '*.pyc') -Quiet
    if ($rc -ge 8) { Fail "Copying the app failed (robocopy exit code $rc)." }
  }
  if (-not (Test-Path (Join-Path $AppDir 'pyproject.toml'))) { Fail 'The app folder is empty after copying.' }
  $version = '0.0.0'
  $m = Select-String -Path (Join-Path $AppDir 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
  if ($m) { $version = $m.Matches[0].Groups[1].Value }
  Note "ChatForge $version, $sourceNote"

  # ---- 5. its environment --------------------------------------------------------------------------------
  Step 5 'Installing the app''s libraries (about 150 MB, one to three minutes the first time)'
  Push-Location $AppDir
  try {
    # --inexact: libraries someone added to this environment (an add-on) survive an upgrade.
    $code = Invoke-Native $uv @('sync', '--locked', '--no-dev', '--inexact', '--python', $python)
  } finally { Pop-Location }
  if ($code -ne 0) { Fail "Installing the app's libraries failed (uv sync exit code $code). The lines above say why; a network problem is the usual cause, and running the installer again resumes." }
  $venvPy  = Join-Path $AppDir '.venv\Scripts\python.exe'
  $venvPyw = Join-Path $AppDir '.venv\Scripts\pythonw.exe'
  $check = Get-NativeOutput $venvPy @('-I', '-c', 'import chatforge; print(chatforge.__version__)')
  if (-not $check) { Fail 'The app was installed but "import chatforge" fails. See the log for details.' }
  Note "ChatForge $check is ready."

  # ---- 6. shortcuts and the Installed-apps entry ---------------------------------------------------------------
  Step 6 'Creating shortcuts'
  $icon = Join-Path $InstallDir 'ChatForge.ico'
  [void](Invoke-Native $venvPy @('-I', '-c', "from chatforge.desktop.icon import write_app_ico; write_app_ico(r'$icon')") -Quiet)
  $shortcuts = @()
  if ($NoShortcuts) {
    Note 'Skipped (-NoShortcuts).'
  } else {
    if ($ShortcutDir) {
      $startMenu = Join-Path $ShortcutDir 'StartMenu'
      $desktop = Join-Path $ShortcutDir 'Desktop'
    } else {
      $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
      $desktop = [Environment]::GetFolderPath('Desktop')
    }
    New-Item -ItemType Directory -Force -Path $startMenu, $desktop | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $targets = @($startMenu)
    if (-not $NoDesktopIcon) { $targets += $desktop }
    foreach ($dir in $targets) {
      $lnk = Join-Path $dir 'ChatForge.lnk'
      $s = $shell.CreateShortcut($lnk)
      $s.TargetPath = $venvPyw
      $s.Arguments = '-P -m chatforge --show'
      $s.WorkingDirectory = $AppDir
      $s.Description = 'ChatForge - a small AI assistant in your tray (Ctrl+Alt+C)'
      if (Test-Path $icon) { $s.IconLocation = $icon }
      $s.Save()
      $shortcuts += $lnk
      Note "Created $lnk"
    }
  }

  # Uninstall scripts live in the install folder so Settings > Installed apps can run them.
  $unDir = Join-Path $InstallDir 'uninstall'
  New-Item -ItemType Directory -Force -Path $unDir | Out-Null
  $unPs1 = Join-Path $ScriptDir 'Uninstall-ChatForge.ps1'
  if (-not (Test-Path $unPs1)) { $unPs1 = Join-Path $AppDir 'deploy\windows\Uninstall-ChatForge.ps1' }
  $unBat = Join-Path $AppDir 'Uninstall ChatForge.bat'
  if (Test-Path $unPs1) { Copy-Item $unPs1 (Join-Path $unDir 'Uninstall-ChatForge.ps1') -Force }
  if (Test-Path $unBat) { Copy-Item $unBat (Join-Path $unDir 'Uninstall ChatForge.bat') -Force }
  $uninstallCmd = Join-Path $unDir 'Uninstall ChatForge.bat'
  $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName"
  if (Test-Path $uninstallCmd) {
    New-Item -Path $regPath -Force | Out-Null
    $props = @{
      DisplayName     = 'ChatForge'
      DisplayVersion  = $version
      InstallLocation = $InstallDir
      UninstallString = ('"{0}"' -f $uninstallCmd)
      NoModify        = 1
      NoRepair        = 1
    }
    if (Test-Path $icon) { $props['DisplayIcon'] = $icon }
    foreach ($k in $props.Keys) { New-ItemProperty -Path $regPath -Name $k -Value $props[$k] -Force | Out-Null }
    Note "Added ChatForge to Settings > Apps > Installed apps."
  }

  # ---- 7. autostart question, then open the app ----------------------------------------------------------------
  Step 7 'Start with Windows?'
  $autostartEnabled = $false
  $status = Get-NativeOutput $venvPy @('-I', '-m', 'chatforge', 'autostart', 'status')
  if ($status -and $status -match 'autostart enabled') {
    $autostartEnabled = $true
    Note 'ChatForge already starts when you sign in; leaving that as it is.'
  } elseif ($NoAutostart) {
    Note 'Not starting at sign-in (-NoAutostart). You can turn it on later in the tray menu.'
    [void](Invoke-Native $venvPy @('-I', '-m', 'chatforge', 'autostart', 'disable') -Quiet)  # records "asked: no" so the app does not ask again
  } else {
    $answer = $null
    if ($Yes) { $answer = $true }
    elseif (Test-Interactive) {
      $reply = Read-Host '      Start ChatForge automatically when you sign in to Windows? [Y/n]'
      $answer = -not ($reply -match '^\s*n')
    } else {
      Note 'Cannot ask here (no console), so nothing is registered. Turn it on any time in the tray menu.'
    }
    if ($answer) {
      $code = Invoke-Native $venvPy @('-I', '-m', 'chatforge', 'autostart', 'enable')
      if ($code -eq 0) { $autostartEnabled = $true } else { Warn 'Could not register the sign-in entry. You can try again from the tray menu.' }
    } elseif ($null -ne $answer) {
      Note 'OK, ChatForge will only start when you open it.'
      [void](Invoke-Native $venvPy @('-I', '-m', 'chatforge', 'autostart', 'disable') -Quiet)  # also records "asked: no" in config.toml, so the app does not ask again
    }
  }

  $info = [ordered]@{
    version = $version; installed_at = (Get-Date).ToString('o'); install_dir = $InstallDir; source = $sourceNote
    uv = $uvVer; python = $python; shortcuts = $shortcuts; uninstall_key = $regPath
    data_home = $env:CHATFORGE_HOME; autostart_registered = $autostartEnabled
  }
  $info | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $InstallDir 'install.json') -Encoding UTF8

  if (-not $NoLaunch) {
    Say ''
    Say 'Opening ChatForge ...'
    Start-Process -FilePath $venvPyw -ArgumentList @('-P', '-m', 'chatforge', '--show') -WorkingDirectory $AppDir
  }

  Say ''
  Write-Host "Done. ChatForge $version is installed." -ForegroundColor Green
  Say '  Open it from the Start menu or the desktop icon, or press Ctrl+Alt+C.'
  Say '  First time: click "Set up local AI" in the window to download the starter model (about 1.1 GB),'
  Say '  or choose StudioForge / an API key instead.'
  Say '  To upgrade later, run this installer again. To remove ChatForge, run "Uninstall ChatForge".'
}
catch {
  $exitCode = 1
  Write-Host ''
  Write-Host 'The installation did not finish.' -ForegroundColor Red
  Write-Host ('  ' + $_.Exception.Message) -ForegroundColor Red
  if ($Transcript) { Write-Host "  Details: $Transcript" }
  Write-Host '  Nothing was deleted. You can run the installer again; it picks up where it stopped.'
}
finally {
  if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue }
  try { Stop-Transcript | Out-Null } catch { }
}
exit $exitCode
