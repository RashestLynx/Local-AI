"""
pcinfo.py - diagnostics the AI can use to look at this PC.

Each check is a small, read-only PowerShell/nvidia-smi query. The chat picks checks based on the
question (see pick_checks), runs them, and gives the facts to the model. Nothing here changes the PC.
"""

import json
import os
import re
import subprocess
import time

try:
    import diag_tool  # the user's own Diag-Tool (github.com/RashestLynx/Diag-Tool), included unchanged
except Exception:  # pragma: no cover
    diag_tool = None

IS_WIN = os.name == "nt"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ps(script, timeout=60):
    if not IS_WIN:
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, timeout=timeout, creationflags=NOWIN,
                           encoding="utf-8", errors="replace")
        return r.stdout.strip()
    except Exception as e:
        return f"(check failed: {e})"


def _json(script, timeout=60):
    out = _ps(script + " | ConvertTo-Json -Compress -Depth 4", timeout)
    try:
        return json.loads(out) if out else None
    except Exception:
        return out or None


def _nvidia(query):
    try:
        r = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, creationflags=NOWIN)
        return r.stdout.strip()
    except Exception:
        return None


# ---------------------------------------------------------------- the checks
def system():
    return _json("$c=Get-CimInstance Win32_ComputerSystem; $o=Get-CimInstance Win32_OperatingSystem; $p=Get-CimInstance Win32_Processor | Select-Object -First 1; "
                 "[pscustomobject]@{ model=\"$($c.Manufacturer) $($c.Model)\"; windows=\"$($o.Caption) $($o.Version)\"; "
                 "cpu=$p.Name.Trim(); cores=$p.NumberOfCores; threads=$p.NumberOfLogicalProcessors; cpu_load_pct=$p.LoadPercentage; "
                 "ram_total_gb=[math]::Round($o.TotalVisibleMemorySize/1MB,1); ram_free_gb=[math]::Round($o.FreePhysicalMemory/1MB,1); "
                 "uptime_hours=[math]::Round(((Get-Date)-$o.LastBootUpTime).TotalHours,1) }")


def gpu():
    raw = _nvidia("name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,driver_version")
    info = {}
    if raw:
        f = [x.strip() for x in raw.split(",")]
        keys = ["name", "vram_used_mb", "vram_total_mb", "gpu_load_pct", "temp_c", "power_w", "driver"]
        info["nvidia"] = dict(zip(keys, f))
    try:
        r = subprocess.run(["nvidia-smi", "--query-compute-apps=process_name,used_memory", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10, creationflags=NOWIN)
        info["programs_using_gpu"] = [os.path.basename(l.split(",")[0]) for l in r.stdout.splitlines() if l.strip()][:30]
    except Exception:
        pass
    info["all_graphics"] = _json("Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,CurrentRefreshRate")
    return info


def top_processes():
    return _json("Get-Process | Where-Object { $_.Id -ne 0 } | Sort-Object WorkingSet64 -Descending | Select-Object -First 12 "
                 "@{n='name';e={$_.ProcessName}}, @{n='ram_mb';e={[math]::Round($_.WorkingSet64/1MB)}}, "
                 "@{n='cpu_seconds';e={[math]::Round($_.CPU,1)}}")


def cpu_hogs():
    # Two samples one second apart to see who is using CPU *right now*
    return _json("$a=Get-Process | Select-Object Id,ProcessName,CPU; Start-Sleep -Milliseconds 1000; $b=Get-Process | Select-Object Id,ProcessName,CPU; "
                 "$n=[Environment]::ProcessorCount; $b | ForEach-Object { $p=$_; $old=($a | Where-Object Id -eq $p.Id).CPU; "
                 "if ($old -ne $null -and $p.CPU -ne $null) { [pscustomobject]@{ name=$p.ProcessName; cpu_pct=[math]::Round(($p.CPU-$old)*100/$n,1) } } } | "
                 "Sort-Object cpu_pct -Descending | Select-Object -First 8", 30)


def disks():
    return {"drives": _json("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object @{n='drive';e={$_.DeviceID}}, "
                            "@{n='free_gb';e={[math]::Round($_.FreeSpace/1GB,1)}}, @{n='size_gb';e={[math]::Round($_.Size/1GB,1)}}"),
            "health": _json("Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,@{n='size_gb';e={[math]::Round($_.Size/1GB)}}")}


def battery():
    return _json("$b=Get-CimInstance Win32_Battery; $r = if ($b) { [pscustomobject]@{ charge_pct=$b.EstimatedChargeRemaining; "
                 "status=$b.BatteryStatus; plugged_in=($b.BatteryStatus -eq 2); power_plan=(powercfg /getactivescheme) -replace '.*\\((.*)\\).*','$1' } } "
                 "else { [pscustomobject]@{ note='No battery found' } }; $r")


def network():
    return {"adapters": _json("Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name,InterfaceDescription,LinkSpeed"),
            "ip": _json("Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | "
                        "Select-Object InterfaceAlias,IPAddress"),
            "wifi": _ps("netsh wlan show interfaces | Select-String 'SSID|Signal|Receive rate|Transmit rate' | ForEach-Object { $_.Line.Trim() }"),
            "internet": _ps("if (Test-Connection 1.1.1.1 -Count 2 -Quiet) { 'online' } else { 'offline' }", 20)}


def startup():
    return _json("Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location")


def recent_errors():
    return _json("Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2; StartTime=(Get-Date).AddDays(-2)} -MaxEvents 40 "
                 "-ErrorAction SilentlyContinue | Group-Object ProviderName | Sort-Object Count -Descending | Select-Object -First 8 "
                 "@{n='source';e={$_.Name}}, Count, @{n='latest';e={($_.Group[0].Message -split \"`n\")[0].Substring(0,[math]::Min(160,($_.Group[0].Message -split \"`n\")[0].Length))}}", 60)


def temperatures():
    return {"gpu_c": (_nvidia("temperature.gpu") or "").strip() or None,
            "cpu_note": "Windows doesn't expose CPU temperature without extra drivers; Armoury Crate shows it."}


def ai_models():
    try:
        import urllib.request
        url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        with urllib.request.urlopen(f"{url}/api/ps", timeout=5) as r:
            return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1), "on_gpu_gb": round(m.get("size_vram", 0) / 1e9, 1)}
                    for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        return f"Ollama not reachable: {e}"


def diagtool_scan():
    """Run Diag-Tool's own health scan (disk, RAM, CPU, temperature, battery, uptime + fix steps)."""
    if not diag_tool:
        return "Diag-Tool not available"
    return {"device": diag_tool.identify_host(),
            "findings": [{"level": f.level, "title": f.title, "detail": f.detail, "fixes": f.fixes[:4]}
                         for f in diag_tool.run_scan()]}


def diagtool_diagnose(question):
    """Look the described symptoms up in Diag-Tool's knowledge base."""
    if not diag_tool:
        return None
    symptoms = diag_tool.detect_symptoms(question)
    if not symptoms:
        return None
    device = diag_tool.detect_device(question) or "laptop"  # this PC is a laptop
    out = []
    for sym in symptoms[:3]:
        causes = diag_tool.causes_for(sym, device)[:4]
        out.append({"symptom": sym.title,
                    "likely_causes": [{"cause": c.name, "how_likely": diag_tool.LIKELIHOOD_LABEL.get(c.likelihood),
                                       "fixes": c.fixes[:4], **({"safety": c.warning} if c.warning else {})}
                                      for c in causes]})
    return {"device": device, "diagnosis": out}


CHECKS = {
    "diagtool_scan": (diagtool_scan, "Running your Diag-Tool health scan"),
    "system": (system, "Computer model, Windows version, CPU, RAM, uptime"),
    "gpu": (gpu, "Graphics card load, memory, temperature, programs using it"),
    "top_processes": (top_processes, "Programs using the most memory"),
    "cpu_hogs": (cpu_hogs, "Programs using the CPU right now"),
    "disks": (disks, "Drive space and disk health"),
    "battery": (battery, "Battery charge and power plan"),
    "network": (network, "Network adapters, IP, Wi-Fi signal, internet check"),
    "startup": (startup, "Programs that start with Windows"),
    "recent_errors": (recent_errors, "Errors Windows logged in the last 2 days"),
    "temperatures": (temperatures, "Temperatures"),
    "ai_models": (ai_models, "AI models currently loaded"),
}

TOPICS = [
    (r"\b(slow|lag|laggy|freez|stutter|performance|fps|speed up|faster|hang)", ["system", "cpu_hogs", "top_processes", "gpu", "disks", "ai_models"]),
    (r"\b(ram|memory)\b", ["system", "top_processes"]),
    (r"\b(cpu|processor)\b", ["system", "cpu_hogs"]),
    (r"\b(gpu|graphics|vram|nvidia|video card)\b", ["gpu", "ai_models"]),
    (r"\b(hot|heat|temp|temperature|fan|overheat)", ["temperatures", "gpu", "cpu_hogs"]),
    (r"\b(disk|drive|storage|space|ssd|hard drive|full)\b", ["disks"]),
    (r"\b(battery|charge|charging|power|plugged)\b", ["battery"]),
    (r"\b(wifi|wi-fi|internet|network|ethernet|ip address|connection|ping)\b", ["network"]),
    (r"\b(startup|start up|boot|booting)\b", ["startup", "system"]),
    (r"\b(crash|error|blue screen|bsod|keeps closing|not working)\b", ["recent_errors", "system"]),
    (r"\b(spec|specs|my pc|my laptop|my computer|what do i have|hardware)\b", ["system", "gpu", "disks"]),
    (r"\b(model|ai|ollama)\b.*\b(slow|loaded|memory|gpu)\b", ["ai_models", "gpu"]),
]


def pick_checks(question):
    chosen = []
    for pattern, checks in TOPICS:
        if re.search(pattern, question, re.I):
            chosen += [c for c in checks if c not in chosen]
    if diag_tool and diag_tool.detect_symptoms(question):
        chosen = ["diagtool_scan"] + [c for c in chosen if c != "diagtool_scan"] or ["diagtool_scan"]
    elif chosen and any(c in chosen for c in ("system", "disks", "battery", "temperatures")):
        chosen.insert(0, "diagtool_scan")
    return chosen


def run(names, progress=None, question=""):
    out = {}
    if question:
        d = diagtool_diagnose(question)
        if d:
            if progress:
                progress("Looking up causes in your Diag-Tool knowledge base")
            out["diagtool_diagnosis"] = d
    for n in names:
        fn, label = CHECKS[n]
        if progress:
            progress(label)
        try:
            out[n] = fn()
        except Exception as e:
            out[n] = f"(check failed: {e})"
    return out
