# setup.ps1 - installs your local AI coding setup in one go.
# Installs (from official sources via winget): Git, Python 3.12, Ollama
# Downloads models: qwen3:8b (coding writer + chat), gemma4:e4b (reviewer)
# Downloads Odysseus from its official GitHub repo and creates start shortcuts.
# Safe to run again: anything already installed is skipped.

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

function Step($msg) { Write-Host "`n==== $msg ====" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !! $msg" -ForegroundColor Yellow }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Ollama", "$env:ProgramFiles\Git\cmd",
                     "$env:LOCALAPPDATA\Programs\Python\Python312",
                     "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts")) {
        if ((Test-Path $p) -and ($env:Path -notlike "*$p*")) { $env:Path += ";$p" }
    }
}

function Wait-Internet {
    # Waits (up to ~10 minutes) until the internet is reachable again.
    for ($i = 0; $i -lt 60; $i++) {
        try { Resolve-DnsName registry.ollama.ai -ErrorAction Stop | Out-Null; return $true } catch {}
        if ($i -eq 0) { Warn "No internet right now - waiting for it to come back..." }
        Start-Sleep 10
    }
    return $false
}

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Install-App($id, $cmd, $name) {
    if (Have $cmd) { Ok "$name already installed"; return }
    Write-Host "  Installing $name ..."
    winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
    if (Have $cmd) { Ok "$name installed" } else { Warn "$name may need a restart of this window to show up" }
}

Write-Host "Local AI setup - this can take 20-40 minutes (the models are ~9 GB)." -ForegroundColor White

# ---------------------------------------------------------------- 1. Programs
Step "1/5  Installing Git, Python and Ollama"
if (-not (Have "winget")) {
    Warn "winget not found. Install 'App Installer' from the Microsoft Store, then run this again."
    Read-Host "Press Enter to exit"; exit 1
}
Refresh-Path
Install-App "Git.Git"             "git"    "Git"
Install-App "Python.Python.3.12"  "python" "Python 3.12"
Install-App "Ollama.Ollama"       "ollama" "Ollama"

# ---------------------------------------------------------------- 2. Ollama running
Step "2/5  Starting Ollama"
$up = $false
try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; $up = $true } catch {}
if (-not $up) {
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not $up; $i++) {
        Start-Sleep 2
        try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; $up = $true } catch {}
    }
}
if ($up) { Ok "Ollama is running" } else { Warn "Ollama didn't start. Open the Ollama app from the Start menu, then run this again." }

# ---------------------------------------------------------------- 3. Models
Step "3/5  Downloading AI models (the slow part)"
foreach ($m in @("qwen3:4b", "qwen3:8b", "llama3.1:8b")) {
    $done = $false
    for ($try = 1; $try -le 10 -and -not $done; $try++) {
        Wait-Internet | Out-Null
        Write-Host "  Downloading $m (attempt $try of 10) ..."
        ollama pull $m          # resumes where it left off if interrupted
        if ($LASTEXITCODE -eq 0) { $done = $true; Ok "$m ready" }
        else { Warn "Download interrupted. Retrying in 20 seconds (it resumes, nothing is lost)..."; Start-Sleep 20 }
    }
    if (-not $done) { Warn "Couldn't download $m - check your internet and run setup again (it resumes)." }
}

# ---------------------------------------------------------------- 4. Odysseus
Step "4/5  Downloading Odysseus (PewDiePie's AI workspace)"
$Ody = Join-Path $Here "odysseus"
if (Test-Path "$Ody\.git") {
    git -C $Ody pull --ff-only
    if ($LASTEXITCODE -eq 0) { Ok "Odysseus is up to date" } else { Warn "Couldn't check for Odysseus updates (it's already downloaded, so that's fine)" }
} else {
    git clone https://github.com/odysseus-dev/odysseus.git $Ody
    if (Test-Path "$Ody\.git") {
        git -C $Ody checkout main 2>$null   # stable branch if it exists
        Ok "Odysseus downloaded"
    } else { Warn "Couldn't download Odysseus" }
}

# ---------------------------------------------------------------- 5. Shortcuts
Step "5/5  Creating start shortcuts"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }

@"
@echo off
title Local AI Assistant
cd /d "%~dp0"
"$py" assistant.py
pause
"@ | Set-Content "$Here\Start Assistant.bat" -Encoding ASCII

@"
@echo off
title Odysseus
cd /d "%~dp0odysseus"
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1
pause
"@ | Set-Content "$Here\Start Odysseus.bat" -Encoding ASCII

$desk = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
foreach ($n in @("Local AI", "Start Odysseus")) {
    $lnk = $ws.CreateShortcut("$desk\$n.lnk")
    $lnk.TargetPath = "$Here\$n.bat"
    $lnk.WorkingDirectory = $Here
    $lnk.Save()
}
Ok "Desktop shortcuts: 'Local AI' and 'Start Odysseus'"

# ---------------------------------------------------------------- Check
Step "Checking everything"
& $py "$Here\coder_team.py" --test-search "python read exif date"
Write-Host ""
ollama list

Write-Host "`nALL DONE." -ForegroundColor Green
Write-Host "  - Double-click 'Local AI' on your desktop: chat, search, coding team, files, security scan."
Write-Host "    (The older terminal version is still there: Start Assistant.bat)"
Write-Host "  - Double-click 'Start Odysseus' for PewDiePie's app. First start takes a few minutes;"
Write-Host "    it prints the web address and a temporary admin password. In its Settings, add"
Write-Host "    the endpoint  http://localhost:11434/v1  to use your models."
Read-Host "`nPress Enter to close"
