<#
.SYNOPSIS
  Reproducible OVMS NPU bring-up for AI Chat (WS1 Phase A). No admin needed.

.DESCRIPTION
  1. Downloads the OVMS 2026.4.0 python_on zip (138,798,816 bytes) into
     %LOCALAPPDATA%\AIChat\runtime\downloads and checks the pinned SHA-256.
  2. Extracts it to %LOCALAPPDATA%\AIChat\runtime\ovms-2026.4.0 (if not already there).
  3. Checks the NPU (Intel AI Boost) and the VC++ x64 runtime.
  4. Applies what setupvars.ps1 sets (OVMS_DIR, PYTHONHOME, SCRIPTS, PATH,
     ESPEAK_DATA_PATH) after removing the venv's VIRTUAL_ENV/PYTHONHOME/PYTHONPATH
     and API_KEY (OVMS turns on API-key auth when API_KEY is set).
  5. Launches ovms.exe hidden on 127.0.0.1 with the validated flags, logs to
     %LOCALAPPDATA%\AIChat\logs\ovms-bringup.log, and times readiness
     (/v2/health/ready 200 + /v1/config state AVAILABLE).
  6. Runs a chat request and a current_datetime tool call, prints tok/s, then
     stops OVMS (unless -KeepRunning) and checks no ovms.exe is left.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ovms_bringup.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ovms_bringup.ps1 -Model OpenVINO/Qwen3-4B-int4-ov -KeepRunning
#>
[CmdletBinding()]
param(
    [string]$Model = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov",
    [string]$Device = "NPU",
    [int]$MaxPromptLen = 4096,
    [int]$Port = 0,
    [string[]]$ExtraArgs = @(),
    [int]$TimeoutSec = 900,
    [switch]$KeepRunning,
    [switch]$SkipChat
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Version = "2026.4.0"
$Asset = "ovms_windows_2026.4.0_python_on.zip"
$AssetBytes = 138798816
$AssetSha256 = "5A022E44E794E6A9CB0F1C6C40822167DAC53A36DAF9AF123C9411974CEF1914"
$ReleaseBase = "https://github.com/openvinotoolkit/model_server/releases/download/v$Version"

$Home_ = if ($env:AICHAT_HOME) { $env:AICHAT_HOME } else { Join-Path $env:LOCALAPPDATA "AIChat" }
$Downloads = Join-Path $Home_ "runtime\downloads"
$InstallDir = Join-Path $Home_ "runtime\ovms-$Version"
$OvmsDir = Join-Path $InstallDir "ovms"
$Exe = Join-Path $OvmsDir "ovms.exe"
$LogsDir = Join-Path $Home_ "logs"
$ModelDir = Join-Path $Home_ ("models\" + ($Model -replace "/", "\"))
$Slug = $Model -replace "/", "--"
$CacheDir = Join-Path $Home_ ("cache\ov\$Slug\" + $Device.ToUpper() + "-$MaxPromptLen")
New-Item -ItemType Directory -Force $Downloads, $LogsDir, $CacheDir | Out-Null

function Say($msg) { Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $msg) }

# --- 1-2. runtime ---------------------------------------------------------------
if (-not (Test-Path $Exe)) {
    $zip = Join-Path $Downloads $Asset
    if (-not (Test-Path $zip) -or (Get-Item $zip).Length -ne $AssetBytes) {
        Say "Downloading $Asset ($AssetBytes bytes)..."
        Invoke-WebRequest -Uri "$ReleaseBase/$Asset" -OutFile "$zip.part" -UseBasicParsing
        Move-Item -Force "$zip.part" $zip
    }
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($hash -ne $AssetSha256) { throw "SHA-256 mismatch for ${zip}: $hash" }
    Say "SHA-256 OK ($hash). Extracting to $InstallDir ..."
    $tmp = "$InstallDir.tmp"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    if (-not (Test-Path (Join-Path $tmp "ovms\ovms.exe"))) { throw "zip has no ovms\ovms.exe" }
    Move-Item $tmp $InstallDir
}
Say ("OVMS: " + ((& $Exe --version 2>$null) | Select-Object -First 1))

# --- 3. hardware / prerequisites -------------------------------------------------
$npu = Get-PnpDevice -FriendlyName '*AI Boost*' -ErrorAction SilentlyContinue
if (-not $npu) { throw "No Intel AI Boost NPU found" }
Say ("NPU: {0} [{1}]" -f $npu.FriendlyName, $npu.Status)
$vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
if (-not $vc -or $vc.Installed -ne 1) { throw "VC++ x64 runtime missing: winget install --id Microsoft.VCRedist.2015+.x64 -e (needs UAC)" }
Say ("VC++ x64 runtime: " + $vc.Version)
if (-not (Test-Path (Join-Path $ModelDir "openvino_model.bin"))) { throw "Model not found: $ModelDir" }

if (Get-Process ovms -ErrorAction SilentlyContinue) { throw "An ovms.exe is already running; stop it first." }

# --- 4. environment (mirrors setupvars.ps1) -------------------------------------
foreach ($name in "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "API_KEY") {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$env:OVMS_DIR = $OvmsDir
if (Test-Path "$OvmsDir\python") {
    $env:PYTHONHOME = "$OvmsDir\python"
    $env:SCRIPTS = "$OvmsDir\python\Scripts"
    $env:PATH = "$OvmsDir;$env:PYTHONHOME;$env:SCRIPTS;$env:PATH"
} else {
    $env:PATH = "$env:PATH;$OvmsDir"
}
if (Test-Path "$OvmsDir\espeak-ng-data") { $env:ESPEAK_DATA_PATH = "$OvmsDir\espeak-ng-data" }

# --- 5. launch -------------------------------------------------------------------
function Test-PortFree([int]$p) {
    $l = $null
    try { $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p); $l.ExclusiveAddressUse = $true; $l.Start(); return $true }
    catch { return $false } finally { if ($l) { $l.Stop() } }
}
if ($Port -eq 0) { $Port = 18611; while (-not (Test-PortFree $Port)) { $Port++ } }

$argv = @("--rest_port", $Port, "--rest_bind_address", "127.0.0.1",
          "--model_name", $Model, "--model_path", "`"$ModelDir`"",
          "--task", "text_generation", "--target_device", $Device.ToUpper())
if ($Device.ToUpper() -like "*NPU*") { $argv += @("--max_prompt_len", $MaxPromptLen) }
$argv += @("--cache_dir", "`"$CacheDir`"", "--tool_parser", "hermes3")
if ($Model -match "Qwen3") { $argv += @("--reasoning_parser", "qwen3") }
$argv += @("--log_level", "INFO") + $ExtraArgs

$log = Join-Path $LogsDir "ovms-bringup.log"
$err = Join-Path $LogsDir "ovms-bringup.err.log"
$cold = -not (Get-ChildItem $CacheDir -Filter *.blob -ErrorAction SilentlyContinue)
Say ("Launching ({0} cache): ovms.exe {1}" -f ($(if ($cold) { "cold" } else { "warm" })), ($argv -join " "))
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath $Exe -ArgumentList $argv -WorkingDirectory $OvmsDir -WindowStyle Hidden `
    -RedirectStandardOutput $log -RedirectStandardError $err -PassThru

$base = "http://127.0.0.1:$Port"
$state = $null
while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if ($proc.HasExited) { throw "ovms.exe exited with code $($proc.ExitCode); see $log" }
    try {
        $ready = Invoke-WebRequest "$base/v2/health/ready" -UseBasicParsing -TimeoutSec 2
        if ($ready.StatusCode -eq 200) {
            $cfg = Invoke-RestMethod "$base/v1/config" -TimeoutSec 2
            $state = $cfg.$Model.model_version_status[0].state
            if ($state -eq "AVAILABLE") { break }
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
$loadS = [math]::Round($sw.Elapsed.TotalSeconds, 1)
if ($state -ne "AVAILABLE") { Stop-Process -Id $proc.Id -Force; throw "not ready after $TimeoutSec s" }
$cacheMB = [math]::Round(((Get-ChildItem $CacheDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
$ovmsMB = [math]::Round(((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Say "READY in $loadS s (pid $($proc.Id), port $Port). Cache $cacheMB MiB, OVMS install $ovmsMB MiB."

# --- 6. smoke --------------------------------------------------------------------
if (-not $SkipChat) {
    $body = @{ model = $Model; max_tokens = 200; chat_template_kwargs = @{ enable_thinking = $false };
               messages = @(@{ role = "system"; content = "You are a concise assistant." },
                            @{ role = "user"; content = "Write three sentences about bicycles." }) } |
            ConvertTo-Json -Depth 6
    $t = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod "$base/v3/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 300
    $tps = [math]::Round($r.usage.completion_tokens / $t.Elapsed.TotalSeconds, 1)
    Say ("chat: {0} tokens, ~{1} tok/s incl. prefill -> {2}" -f $r.usage.completion_tokens, $tps, $r.choices[0].message.content)

    $tools = @(@{ type = "function"; function = @{ name = "current_datetime"; description = "Get the current local date and time.";
                  parameters = @{ type = "object"; properties = @{}; required = @() } } })
    $body = @{ model = $Model; max_tokens = 100; chat_template_kwargs = @{ enable_thinking = $false }; tools = $tools;
               messages = @(@{ role = "user"; content = "What is today's date? Use the tool." }) } | ConvertTo-Json -Depth 8
    $r = Invoke-RestMethod "$base/v3/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 300
    Say ("tools: finish_reason={0} call={1}" -f $r.choices[0].finish_reason, ($r.choices[0].message.tool_calls | ConvertTo-Json -Compress -Depth 5))
}

if ($KeepRunning) {
    Say "Left running: $base/v3 (pid $($proc.Id)). Stop with: Stop-Process -Id $($proc.Id)"
} else {
    Stop-Process -Id $proc.Id -Force
    Start-Sleep -Seconds 2
    if (Get-Process ovms -ErrorAction SilentlyContinue) { throw "ovms.exe still running after stop" }
    Say "Stopped; no ovms.exe left."
}
