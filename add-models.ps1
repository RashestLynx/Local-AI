# add-models.ps1 - downloads extra AI models that fit a 6 GB graphics card.
# Safe to run again: models you already have are just checked/updated.
# Stops early if your C: drive would drop below 25 GB free.

$ErrorActionPreference = "Continue"
function Ok($m)   { Write-Host "  OK: $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !! $m" -ForegroundColor Yellow }

$models = @(
    @{ name = "qwen2.5-coder:7b"; gb = 4.7; why = "coding specialist (backup writer)" },
    @{ name = "deepseek-r1:7b";   gb = 4.7; why = "step-by-step reasoning, good for tricky bugs" },
    @{ name = "llama3.1:8b";      gb = 4.9; why = "general chat, a third model family for reviews" },
    @{ name = "qwen3:4b";         gb = 2.6; why = "fast model for quick questions" },
    @{ name = "qwen2.5-coder:1.5b"; gb = 1.0; why = "very fast, for small code snippets" }
)

Write-Host "Adding extra AI models (about 18 GB total)." -ForegroundColor White
try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null }
catch {
    Write-Host "  Starting Ollama..."
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep 5
}

foreach ($m in $models) {
    $free = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
    if ($free - $m.gb -lt 25) {
        Warn "Skipping $($m.name): only $free GB free on C:, keeping at least 25 GB free."
        continue
    }
    Write-Host "`n==== $($m.name)  ($($m.gb) GB) - $($m.why) ====" -ForegroundColor Cyan
    $done = $false
    for ($try = 1; $try -le 6 -and -not $done; $try++) {
        ollama pull $m.name
        if ($LASTEXITCODE -eq 0) { $done = $true; Ok "$($m.name) ready" }
        else { Warn "Interrupted - retrying in 20 seconds (it resumes)..."; Start-Sleep 20 }
    }
    if (-not $done) { Warn "Couldn't download $($m.name). Run this again later." }
}

Write-Host "`nModels on this PC:" -ForegroundColor White
ollama list
Write-Host "`nDone. Pick any of these in the Local AI app under Settings." -ForegroundColor Green
Read-Host "Press Enter to close"
