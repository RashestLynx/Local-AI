# cleanup.ps1 - removes the apps listed in remove-list.json, then scans what's installed.
# Shows elapsed time and an ETA while it works. Writes results next to this script:
#   cleanup-log.txt        what was removed (or failed)
#   installed-apps.json    every installed app, its size, and when it was last opened
#
# Protected (never removed): drivers, Windows components, Armoury Crate, Steam, Ollama, Python, Git,
# runtimes (.NET, Visual C++), and anything Microsoft marks as non-removable.

#
# Local AI runs it with: -List <json> -Out <json> -NoPause [-ScanOnly] [-SkipScan]
# and reads the "PROGRESS|percent|text" lines it prints.
param([string]$List = "", [string]$Out = "", [switch]$NoPause, [switch]$ScanOnly, [switch]$SkipScan)

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Here "cleanup-log.txt"
function Prog($pct, $activity, $status) {
    Write-Progress -Activity $activity -Status $status -PercentComplete $pct
    if ($NoPause) { Write-Host "PROGRESS|$pct|$activity - $status" }
}
$Start = Get-Date
function Log($m) { $line = "$(Get-Date -Format 'HH:mm:ss')  $m"; Write-Host $line; Add-Content $Log $line }
function Fmt($ts) { if ($ts.TotalHours -ge 1) { "{0:h\:mm\:ss}" -f $ts } else { "{0:m\:ss}" -f $ts } }

$Protected = @("visual studio","\bvs_","sdk","runtime","redistributable","framework","pawnio","librehardware","fancontrol","playstation","ubisoft","battle\.net","xbox","gaming services","launcher","anti-?cheat","battleye","vanguard","nvidia","\bamd\b","radeon","realtek","\bintel","armoury","\basus","\brog\b","steam","ollama","python",
               "\bgit\b","visual c\+\+","\.net","windows","driver","dolby","nahimic","claude","microsoft edge",
               "webview","microsoft store","defender","security","directx","vulkan","openal","rockstar","epic games","\bea app")
function IsProtected($name) {
    $n = " " + $name.ToLower() + " "
    foreach ($p in $Protected) { if ($n -match $p) { return $true } }
    return $false
}

Add-Content $Log "`n===== Cleanup started $Start ====="

# ------------------------------------------------------------------ 1. remove listed apps
$listPath = if ($List) { $List } else { Join-Path $Here "remove-list.json" }
$items = @()
if (-not $ScanOnly -and (Test-Path $listPath)) {
    # Windows PowerShell 5 hands back the whole JSON list as ONE object; piping it through
    # ForEach-Object splits it into separate items.
    $parsed = ConvertFrom-Json (Get-Content $listPath -Raw)
    $items = @($parsed | ForEach-Object { $_ })
}
$total = $items.Count
$done = 0
$removeStart = Get-Date
foreach ($it in $items) {
    $done++
    $elapsed = (Get-Date) - $removeStart
    $eta = if ($done -gt 1) { [TimeSpan]::FromSeconds($elapsed.TotalSeconds / ($done - 1) * ($total - $done + 1)) } else { $null }
    $etaText = if ($eta) { "about $(Fmt $eta) left" } else { "estimating..." }
    Prog ([int](100 * ($done - 1) / [math]::Max($total, 1))) "Removing apps ($done of $total)" "$($it.name)"
    Log "[$done/$total] Removing $($it.name)  (elapsed $(Fmt ((Get-Date) - $Start)), $etaText)"
    if ($it.kind -in @("appx", "winget", "uninstall") -and (IsProtected $it.name)) { Log "   skipped - protected"; continue }
    try {
        switch ($it.kind) {
            "appx" {
                $pkgs = @(Get-AppxPackage -Name $it.id -ErrorAction SilentlyContinue)
                if (-not $pkgs) { Log "   not installed"; break }
                foreach ($p in $pkgs) { Remove-AppxPackage -Package $p.PackageFullName -ErrorAction Stop }
                Log "   removed"
            }
            "winget" {
                Log "   (Windows may ask for permission - click Yes)"
                winget uninstall --id $it.id -e --silent --accept-source-agreements --disable-interactivity | Out-Null
                if ($LASTEXITCODE -eq 0) { Log "   removed" } else { Log "   winget could not remove it (code $LASTEXITCODE)" }
            }
            "special" {
                if ($it.id -eq "onedrive") {
                    Get-Process OneDrive -ErrorAction SilentlyContinue | Stop-Process -Force
                    winget uninstall --id Microsoft.OneDrive -e --silent --accept-source-agreements | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        $setup = @("$env:SystemRoot\System32\OneDriveSetup.exe", "$env:SystemRoot\SysWOW64\OneDriveSetup.exe") |
                                 Where-Object { Test-Path $_ } | Select-Object -First 1
                        if ($setup) { Start-Process $setup "/uninstall" -Wait }
                    }
                    Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name OneDrive -ErrorAction SilentlyContinue
                    Log "   removed (your files in OneDrive are still safe online at onedrive.com)"
                }
            }
            "edge-quiet" {
                # Edge stays installed (Windows needs its web engine) but stops running in the background.
                $pol = "HKCU:\Software\Policies\Microsoft\Edge"
                New-Item $pol -Force | Out-Null
                Set-ItemProperty $pol -Name StartupBoostEnabled -Value 0 -Type DWord
                Set-ItemProperty $pol -Name BackgroundModeEnabled -Value 0 -Type DWord
                Set-ItemProperty $pol -Name HideFirstRunExperience -Value 1 -Type DWord
                $run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
                (Get-Item $run).GetValueNames() | Where-Object { $_ -like "MicrosoftEdgeAutoLaunch*" } |
                    ForEach-Object { Remove-ItemProperty $run -Name $_ }
                Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
                Log "   Edge will no longer start with Windows or run in the background"
            }
            "tweak" {
                # Small, reversible settings (current user only).
                switch ($it.id) {
                    "ads-off" {
                        $cdm = "HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
                        foreach ($v in "SystemPaneSuggestionsEnabled","SubscribedContent-338388Enabled","SubscribedContent-338389Enabled",
                                       "SubscribedContent-353694Enabled","SubscribedContent-353696Enabled","SoftLandingEnabled",
                                       "SilentInstalledAppsEnabled","PreInstalledAppsEnabled","OemPreInstalledAppsEnabled") {
                            Set-ItemProperty $cdm -Name $v -Value 0 -Type DWord -ErrorAction SilentlyContinue
                        }
                        $adv = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
                        Set-ItemProperty $adv -Name Start_IrisRecommendations -Value 0 -Type DWord -ErrorAction SilentlyContinue
                        Set-ItemProperty $adv -Name ShowSyncProviderNotifications -Value 0 -Type DWord -ErrorAction SilentlyContinue
                        Log "   Start menu suggestions, tips and auto-installed promo apps turned off"
                    }
                    "bing-search-off" {
                        $pol = "HKCU:\Software\Policies\Microsoft\Windows\Explorer"
                        New-Item $pol -Force | Out-Null
                        Set-ItemProperty $pol -Name DisableSearchBoxSuggestions -Value 1 -Type DWord
                        Log "   Start menu search will only search your PC (no Bing web results)"
                    }
                    "background-apps-off" {
                        $ba = "HKCU:\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications"
                        New-Item $ba -Force | Out-Null
                        Set-ItemProperty $ba -Name GlobalUserDisabled -Value 1 -Type DWord
                        Log "   Store apps can no longer run in the background"
                    }
                    default { Log "   unknown setting" }
                }
            }
            "uninstall" {
                $cmd = $it.command
                if (-not $cmd) { Log "   no uninstaller found"; break }
                Log "   running its uninstaller (it may show its own window - follow it)"
                if ($cmd -match '^\s*"?([^"]+\.exe)"?\s*(.*)$') { Start-Process -FilePath $matches[1] -ArgumentList $matches[2] -Wait }
                else { Start-Process cmd.exe "/c $cmd" -Wait }
                Log "   uninstaller finished"
            }
        }
    } catch { Log "   failed: $($_.Exception.Message)" }
}
if ($total -gt 0) { Write-Progress -Activity "Removing apps" -Completed; Log "Removal finished in $(Fmt ((Get-Date) - $removeStart))." }
if (-not $ScanOnly -and (Test-Path $listPath)) {
    if ($List) { Remove-Item $listPath -Force -ErrorAction SilentlyContinue }
    else { Rename-Item $listPath "remove-list.done.json" -Force -ErrorAction SilentlyContinue }
}
if ($SkipScan) { exit 0 }

# ------------------------------------------------------------------ 2. when was each app last opened?
Prog 5 "Scanning installed apps" "Reading usage history..."
function Rot13($s) { -join ($s.ToCharArray() | ForEach-Object {
    $c = [int]$_
    if ($c -ge 65 -and $c -le 90) { [char](65 + (($c - 65 + 13) % 26)) }
    elseif ($c -ge 97 -and $c -le 122) { [char](97 + (($c - 97 + 13) % 26)) }
    else { $_ } }) }
$usage = @{}   # lowercased key (exe path or app id) -> @{last=..; count=..}
Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist" -ErrorAction SilentlyContinue | ForEach-Object {
    $k = Get-Item (Join-Path $_.PSPath "Count") -ErrorAction SilentlyContinue
    if (-not $k) { return }
    foreach ($v in $k.GetValueNames()) {
        $data = $k.GetValue($v)
        if ($data -isnot [byte[]] -or $data.Length -lt 68) { continue }
        $name = (Rot13 $v).ToLower()
        $count = [BitConverter]::ToInt32($data, 4)
        $ft = [BitConverter]::ToInt64($data, 60)
        $last = if ($ft -gt 0) { [DateTime]::FromFileTime($ft) } else { $null }
        if (-not $usage.ContainsKey($name) -or ($last -and $usage[$name].last -lt $last)) { $usage[$name] = @{ last = $last; count = $count } }
    }
}
# Resolve known-folder GUIDs in UserAssist paths to real folders.
$kf = @{ "{6d809377-6af0-444b-8957-a3773f02200e}" = $env:ProgramFiles; "{7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e}" = ${env:ProgramFiles(x86)};
         "{f38bf404-1d43-42f2-9305-67de0b28fc23}" = $env:SystemRoot; "{1ac14e77-02e7-4e5d-b744-2eb1ae5198b7}" = "$env:SystemRoot\System32" }
$usagePaths = @{}
foreach ($k in $usage.Keys) {
    $p = $k
    foreach ($g in $kf.Keys) { if ($p.StartsWith($g)) { $p = $p.Replace($g, $kf[$g].ToLower()) } }
    $usagePaths[$p] = $usage[$k]
}
function LastUse([string]$folder, [string]$appId) {
    $best = $null
    foreach ($p in $usagePaths.Keys) {
        if (($folder -and $folder.Length -gt 3 -and $p.StartsWith($folder.ToLower().TrimEnd('\'))) -or ($appId -and $p.StartsWith($appId.ToLower()))) {
            $u = $usagePaths[$p]
            if (-not $best -or ($u.last -and $u.last -gt $best.last)) { $best = $u }
        }
    }
    return $best
}

# ------------------------------------------------------------------ 3. list installed apps
$apps = New-Object System.Collections.ArrayList
$roots = @("HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
           "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall")
$regItems = foreach ($r in $roots) { Get-ChildItem $r -ErrorAction SilentlyContinue | ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } }
$seen = @{}
$i = 0; $n = @($regItems).Count
foreach ($a in $regItems) {
    $i++
    if ($i % 20 -eq 0) { Prog ([int](10 + 60 * $i / [math]::Max($n, 1))) "Scanning installed apps" "Programs $i of $n" }
    if (-not $a.DisplayName -or $a.SystemComponent -eq 1 -or $a.ParentKeyName -or $a.ReleaseType -match "Update|Hotfix") { continue }
    if ($seen.ContainsKey($a.DisplayName)) { continue }
    $seen[$a.DisplayName] = 1
    $u = LastUse $a.InstallLocation $null
    if (-not $u -and $a.DisplayIcon) { $u = LastUse (Split-Path ($a.DisplayIcon -replace '"|,\d+$', '') -Parent -ErrorAction SilentlyContinue) $null }
    [void]$apps.Add([pscustomobject]@{
        name = $a.DisplayName; kind = "uninstall"; id = $a.PSChildName; publisher = $a.Publisher
        size_mb = if ($a.EstimatedSize) { [int]($a.EstimatedSize / 1024) } else { $null }
        installed = $a.InstallDate; last_used = if ($u -and $u.last) { $u.last.ToString("yyyy-MM-dd") } else { $null }
        times_opened = if ($u) { $u.count } else { $null }
        command = if ($a.QuietUninstallString) { $a.QuietUninstallString } else { $a.UninstallString }
        protected = (IsProtected $a.DisplayName)
    })
}
$pk = @(Get-AppxPackage -PackageTypeFilter Main -ErrorAction SilentlyContinue | Where-Object { -not $_.NonRemovable -and -not $_.IsFramework })
$i = 0
foreach ($p in $pk) {
    $i++
    if ($i % 10 -eq 0) { Prog ([int](70 + 28 * $i / [math]::Max($pk.Count, 1))) "Scanning installed apps" "Store apps $i of $($pk.Count)" }
    $display = $p.Name
    try { $m = (Get-AppxPackageManifest $p -ErrorAction Stop).Package.Properties.DisplayName; if ($m -and $m -notmatch '^ms-resource') { $display = $m } } catch {}
    $u = LastUse $null $p.PackageFamilyName
    [void]$apps.Add([pscustomobject]@{
        name = $display; kind = "appx"; id = $p.Name; publisher = ($p.Publisher -replace '^CN=([^,]+).*', '$1')
        size_mb = $null; installed = $null
        last_used = if ($u -and $u.last) { $u.last.ToString("yyyy-MM-dd") } else { $null }
        times_opened = if ($u) { $u.count } else { $null }
        command = $null; protected = (IsProtected $display) -or (IsProtected $p.Name)
    })
}
$outPath = if ($Out) { $Out } else { Join-Path $Here "installed-apps.json" }
$apps | Sort-Object name | ConvertTo-Json -Depth 3 | Set-Content $outPath -Encoding UTF8
Write-Progress -Activity "Scanning installed apps" -Completed
$never = @($apps | Where-Object { -not $_.last_used -and -not $_.protected }).Count
Log "Scan finished: $($apps.Count) apps found, $never never opened (not counting protected ones). Total time $(Fmt ((Get-Date) - $Start))."
if (-not $NoPause) {
    Write-Host "`nDone. The list of installed apps is in installed-apps.json (Local AI shows it under Security > Cleanup)." -ForegroundColor Green
    Read-Host "Press Enter to close"
}
