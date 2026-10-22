"""
scanner.py - Local AI's own malware finder and remover.

How it works
  1. Collect what could be dangerous:
       - programs that start with Windows (Run keys, Startup folders, scheduled tasks)
       - running processes
       - risky file types in Downloads, Desktop, Temp, AppData (quick) or the whole user folder (full)
  2. Score each item with rules (see RULES below): fake double extensions, Windows look-alikes,
     unsigned/tampered programs, encoded PowerShell, things running from Temp, known-bad hashes...
  3. Optional second opinion from the Microsoft Defender engine for anything suspicious.
  4. Actions never delete outright:
       quarantine  -> file is moved into LocalAI\\quarantine and renamed so it can't run (restorable)
       disable     -> startup entry is removed/disabled (saved so it can be restored)
       trust       -> remember this file/entry as safe

Everything here runs locally. Only "update threat list" touches the internet (abuse.ch MalwareBazaar).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.request
from pathlib import Path

IS_WIN = os.name == "nt"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXEC_EXT = {".exe", ".scr", ".com", ".pif", ".cpl", ".msi", ".dll", ".sys"}
SCRIPT_EXT = {".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".hta", ".lnk"}
RISKY_EXT = EXEC_EXT | SCRIPT_EXT
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "jpg", "jpeg", "png", "gif", "txt", "mp3", "mp4", "zip", "rar"}

SYSTEM_NAMES = {"svchost.exe", "csrss.exe", "lsass.exe", "winlogon.exe", "services.exe", "smss.exe",
                "explorer.exe", "spoolsv.exe", "taskhostw.exe", "dwm.exe", "wininit.exe", "conhost.exe",
                "rundll32.exe", "dllhost.exe", "ctfmon.exe", "searchhost.exe", "runtimebroker.exe"}
TRUSTED_SIGNERS = ("microsoft", "google", "nvidia", "advanced micro devices", "amd", "asustek", "intel",
                   "valve", "logitech", "discord", "opera", "mozilla", "github", "python software",
                   "ollama", "tailscale", "realtek", "dolby", "rockstar", "epic games", "electronic arts",
                   "anthropic", "oracle", "node.js", "openjs", "eclipse", "win.rar", "videolan", "obs",
                   "apple", "sony", "ubisoft", "jellyfin", "playstation", "steam")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "site-packages", "WinSxS", "assembly", ".cache",
             "Packages", "WindowsApps", "Microsoft\\WindowsApps", "pip", "npm-cache", "Steam\\steamapps"}

ENCODED_PS = re.compile(r"(-e(nc(odedcommand)?)?\s+[A-Za-z0-9+/=]{40,})|frombase64string|iex\s*\(|invoke-expression"
                        r"|downloadstring|net\.webclient|bitsadmin\s+/transfer", re.I)
LOLBIN = re.compile(r"\b(mshta|wscript|cscript|regsvr32|rundll32|certutil|bitsadmin|powershell|pwsh)(\.exe)?\b", re.I)
def looks_random(stem):
    """True for names like 'x8kq2vz9pl3' or long hex, not for normal words like 'WidgetService'."""
    s = stem.lower()
    if re.fullmatch(r"[0-9a-f]{16,}", s) or re.fullmatch(r"\{?[0-9a-f-]{32,}\}?", s):
        return True
    if not re.fullmatch(r"[a-z0-9]{8,}", s):
        return False
    letters = [c for c in s if c.isalpha()]
    digits = sum(c.isdigit() for c in s)
    vowels = sum(c in "aeiou" for c in letters)
    if re.search(r"[a-z]{4,}", s) and re.search(r"[aeiou]", s) and vowels / max(len(letters), 1) >= 0.25:
        return False  # contains pronounceable words
    return digits >= 3 and len(letters) >= 3 and vowels / max(len(letters), 1) < 0.25


def _ps(script, timeout=600):
    """Run PowerShell and return its stdout (Windows only)."""
    if not IS_WIN:
        return ""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, timeout=timeout, creationflags=NOWIN,
                       encoding="utf-8", errors="replace")
    return r.stdout


def sha256(path, limit=150 * 1024 * 1024):
    try:
        if os.path.getsize(path) > limit:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def exe_from_command(cmd):
    """Pull the program path out of a command line like '"C:\\x\\a.exe" --flag'."""
    if not cmd:
        return ""
    cmd = os.path.expandvars(cmd.strip()).lstrip('"').lstrip()
    if cmd and not cmd.startswith('"') and '"' in cmd[:3]:
        cmd = cmd.replace('"', "", 1)
    m = re.match(r'^([^"]+?\.(exe|bat|cmd|vbs|js|ps1|com|scr|hta|msi))"', cmd, re.I)
    if m:
        return m.group(1)
    m = re.match(r'^"([^"]+)"', cmd)
    if m:
        return m.group(1)
    m = re.match(r"^(.+?\.(exe|bat|cmd|vbs|js|ps1|com|scr|hta|msi))\b", cmd, re.I)
    return m.group(1) if m else cmd.split(" ")[0]


class Scanner:
    def __init__(self, home_dir):
        self.home = Path(home_dir)
        self.qdir = self.home / "quarantine"
        self.data = self.home / "security"
        self.data.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.state = {"running": False, "phase": "idle", "done": 0, "total": 0, "started": None,
                      "finished": None, "mode": None, "findings": [], "scanned": 0, "error": None}
        self.cancel = False

    # ------------------------------------------------------------ persistence
    def _load(self, name, default):
        p = self.data / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save(self, name, obj):
        (self.data / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")

    def trusted(self):
        return self._load("trusted.json", {"hashes": [], "paths": []})

    def bad_hashes(self):
        p = self.data / "known-bad-sha256.txt"
        if not p.exists():
            return set()
        return {l.strip().lower() for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                if len(l.strip()) == 64 and not l.startswith("#")}

    def update_threat_list(self):
        """Download recent known-malware fingerprints from abuse.ch MalwareBazaar (free, public)."""
        url = "https://bazaar.abuse.ch/export/txt/sha256/recent/"
        req = urllib.request.Request(url, headers={"User-Agent": "LocalAI-Scanner/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8", errors="ignore")
        hashes = [l.strip().lower() for l in text.splitlines() if len(l.strip()) == 64 and not l.startswith("#")]
        if not hashes:
            raise RuntimeError("The download had no fingerprints in it.")
        old = self.bad_hashes()
        merged = sorted(old | set(hashes))
        (self.data / "known-bad-sha256.txt").write_text("# from abuse.ch MalwareBazaar\n" + "\n".join(merged),
                                                        encoding="utf-8")
        return {"added": len(set(hashes) - old), "total": len(merged)}

    # ------------------------------------------------------------ progress
    def status(self):
        with self.lock:
            s = dict(self.state)
        if s["started"]:
            end = s["finished"] or time.time()
            s["elapsed"] = round(end - s["started"], 1)
            # ETA: phases weighted; file phase dominates.
            if s["running"] and s["total"] and s["done"]:
                rate = (time.time() - s["phase_started"]) / s["done"]
                s["eta"] = round(rate * (s["total"] - s["done"]) + s.get("eta_extra", 0), 1)
            elif s["running"]:
                s["eta"] = None
        s["findings"] = sorted(s["findings"], key=lambda f: -f["score"])
        return s

    def _phase(self, name, total=0, eta_extra=0):
        with self.lock:
            self.state.update(phase=name, done=0, total=total, phase_started=time.time(), eta_extra=eta_extra)

    def _tick(self, n=1):
        with self.lock:
            self.state["done"] += n

    # ------------------------------------------------------------ collection
    def startup_items(self):
        items = []
        if IS_WIN:
            import winreg
            keys = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
                    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM")]
            for hive, sub, label in keys:
                try:
                    k = winreg.OpenKey(hive, sub)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    items.append({"where": f"{label}\\{sub}", "name": name, "command": str(val),
                                  "kind": "run", "hive": label, "key": sub})
            # Startup folders (.lnk shortcuts resolved by PowerShell below)
            for folder in (Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
                           Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"):
                if folder.exists():
                    for f in folder.iterdir():
                        if f.name.lower() == "desktop.ini":
                            continue
                        items.append({"where": "Startup folder", "name": f.name, "command": str(f), "kind": "startup-file",
                                      "file": str(f), "entry_path": str(f)})
            lnks = [i for i in items if i["kind"] == "startup-file" and i["file"].lower().endswith(".lnk")]
            if lnks:
                script = ("$s=New-Object -ComObject WScript.Shell; $o=@(); " +
                          "; ".join(f"$l=$s.CreateShortcut('{i['file'].replace(chr(39), chr(39) * 2)}'); $o+=($l.TargetPath+' '+$l.Arguments)"
                                    for i in lnks) + "; $o -join \"`n\"")
                for i, line in zip(lnks, _ps(script, 60).splitlines()):
                    i["command"] = line.strip() or i["command"]
            # Scheduled tasks that aren't part of Windows itself
            out = _ps("Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' -and $_.State -ne 'Disabled' } | "
                      "ForEach-Object { $a = $_.Actions | Select-Object -First 1; [pscustomobject]@{ n=$_.TaskName; p=$_.TaskPath; "
                      "c=(\"$($a.Execute) $($a.Arguments)\").Trim(); au=$_.Author } } | ConvertTo-Json -Compress", 120)
            try:
                tasks = json.loads(out) if out.strip() else []
                tasks = tasks if isinstance(tasks, list) else [tasks]
            except Exception:
                tasks = []
            for t in tasks:
                if t.get("c"):
                    items.append({"where": f"Scheduled task {t.get('p', '')}", "name": t.get("n"), "command": t["c"],
                                  "kind": "task", "task": (t.get("p") or "\\") + (t.get("n") or "")})
        for it in items:
            it["file"] = it.get("file") if it["kind"] == "startup-file" and not it["file"].lower().endswith(".lnk") \
                else exe_from_command(it["command"])
        return items

    def processes(self):
        if not IS_WIN:
            return []
        out = _ps("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath } | "
                  "Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress", 120)
        try:
            procs = json.loads(out) if out.strip() else []
            return procs if isinstance(procs, list) else [procs]
        except Exception:
            return []

    def file_roots(self, mode):
        home = Path(os.path.expanduser("~"))
        tmp = Path(os.environ.get("TEMP", "/tmp"))
        if mode == "full":
            roots = [(home, 12)]
            if IS_WIN:
                roots.append((Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")), 6))
            roots.append((tmp, 6))
        else:
            roots = [(home / "Downloads", 3), (home / "Desktop", 3), (tmp, 3),
                     (Path(os.environ.get("APPDATA", home / "AppData/Roaming")), 2),
                     (Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")), 2),
                     (home / "Documents", 2)]
        extra = os.environ.get("LAI_SCAN_EXTRA")  # used by tests
        if extra:
            roots.append((Path(extra), 4))
        return roots

    def walk_files(self, mode, limit=60000):
        seen, out = set(), []
        for root, depth in self.file_roots(mode):
            if not root.exists():
                continue
            base = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                if self.cancel:
                    return out
                rel_depth = len(Path(dirpath).parts) - base
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("$")
                               and rel_depth < depth and not any(s in os.path.join(dirpath, d) for s in SKIP_DIRS if "\\" in s)]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in RISKY_EXT and not (ext in (".dll", ".sys") and mode != "full"):
                        p = os.path.join(dirpath, fn)
                        if p.lower() not in seen:
                            seen.add(p.lower())
                            out.append(p)
                            if len(out) >= limit:
                                return out
        return out

    def signatures(self, paths):
        """Authenticode status for many files at once: {path_lower: (status, signer)}."""
        res = {}
        if not IS_WIN or not paths:
            return res
        listfile = self.data / "siglist.txt"
        for i in range(0, len(paths), 400):
            chunk = paths[i:i + 400]
            listfile.write_text("\n".join(chunk), encoding="utf-8")
            out = _ps(f"Get-Content -LiteralPath '{listfile}' -Encoding UTF8 | ForEach-Object {{ try {{ $s = Get-AuthenticodeSignature -LiteralPath $_ -ErrorAction Stop; "
                      "$cn = if ($s.SignerCertificate) { $s.SignerCertificate.Subject -replace '\\|',' ' } else { '' }; "
                      "\"$($_)|$($s.Status)|$cn\" } catch { \"$($_)|Error|\" } }", 900)
            for line in out.splitlines():
                parts = line.rsplit("|", 2)
                if len(parts) == 3:
                    res[parts[0].lower()] = (parts[1], parts[2])
            self._tick(len(chunk))
        try:
            listfile.unlink()
        except Exception:
            pass
        return res

    def defender_check(self, path):
        """Ask the Microsoft Defender engine about one file. Returns threat name or None."""
        if not IS_WIN:
            return None
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
        if not exe.exists():
            return None
        try:
            r = subprocess.run([str(exe), "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"],
                               capture_output=True, text=True, timeout=180, creationflags=NOWIN, errors="replace")
        except Exception:
            return None
        if r.returncode == 2:
            m = re.search(r"Threat\s*:\s*(.+)", r.stdout)
            return m.group(1).strip() if m else "Threat found by Defender"
        return None

    # ------------------------------------------------------------ rules
    def score(self, path, sig, context, bad, trusted, cmd=""):
        """Returns (score, reasons). context: 'file' | 'startup' | 'process'."""
        reasons = []
        add = lambda pts, why: reasons.append((pts, why))
        p = path or ""
        pl = p.lower().replace("/", "\\")
        name = re.split(r"[\\/]", pl)[-1]
        stem, ext = os.path.splitext(name)
        status, signer = sig if sig else ("Unknown", "")
        signer_l = (signer or "").lower()
        trusted_signer = status == "Valid" and any(t in signer_l for t in TRUSTED_SIGNERS)
        org = re.search(r"\bO=\"?([^,\"]+)", signer or "") or re.search(r"CN=\"?([^,\"]+)", signer or "")
        signer_name = org.group(1).strip() if org else signer

        h = sha256(p) if p and os.path.isfile(p) else None
        if h and h in bad:
            add(100, "Matches a known malware fingerprint (abuse.ch MalwareBazaar)")
        if (h and h in trusted["hashes"] or pl in [t.lower() for t in trusted["paths"]]) and not (h and h in bad):
            return 0, [], h

        # fake extensions: invoice.pdf.exe, photo.jpg.scr, "readme.txt      .exe"
        parts = name.split(".")
        if len(parts) >= 3 and parts[-2] in DOC_EXT and "." + parts[-1] in EXEC_EXT | SCRIPT_EXT:
            add(65, f"Disguised file name ('.{parts[-2]}' pretending, really '.{parts[-1]}')")
        if re.search(r"\s{4,}\.", name):
            add(50, "Hides its real extension with lots of spaces")
        # Windows look-alikes outside Windows folder
        if name in SYSTEM_NAMES and "\\windows\\" not in pl:
            add(75, f"Named like a Windows system file ({name}) but isn't in the Windows folder")
        if re.match(r"^(svch0st|scvhost|svchosts|lsas|lssas|explore|expl0rer|csrsss|winlog0n)\.exe$", name):
            add(70, "Misspelled copy of a Windows system file name")
        # location
        in_temp = "\\temp\\" in pl or "\\tmp\\" in pl
        in_roaming = "\\appdata\\roaming\\" in pl
        if ext in EXEC_EXT and in_temp and not (status == "Valid" and context == "file"):
            add(25 if context == "file" else 40, "Program sitting in a Temp folder" + (" and running/auto-starting" if context != "file" else ""))
        if context in ("startup", "process") and ("\\downloads\\" in pl):
            add(30, "Runs straight from the Downloads folder")
        if context == "startup" and in_roaming and ext in EXEC_EXT and status != "Valid":
            add(25, "Unsigned program in AppData starts with Windows")
        # signatures
        if ext in (".exe", ".dll", ".scr", ".sys", ".msi") and os.path.isfile(p):
            if status == "HashMismatch":
                add(70, "Its digital signature is broken - the file was modified after signing")
            elif status in ("NotSigned", "UnknownError", "NotTrusted") and IS_WIN and not re.search(
                    r"\\program files\\windowsapps\\|\\windows\\systemapps\\|\\driverstore\\|\\windows\\system32\\", pl):
                add(20 if context != "file" else 10, "Not digitally signed by any publisher")
        # command line tricks (scored separately: a signed tool like powershell.exe can still be misused)
        cmd_reasons = []
        if cmd:
            if ENCODED_PS.search(cmd):
                cmd_reasons.append((60, "Uses a hidden/encoded PowerShell command or downloads and runs code"))
            if LOLBIN.search(exe_from_command(cmd) or "") and re.search(r"appdata|temp|http|\\users\\public", cmd, re.I):
                cmd_reasons.append((35, "Uses a built-in Windows tool to run something from AppData/Temp/the internet"))
            if re.search(r"-w(indowstyle)?\s+hid", cmd, re.I) and cmd_reasons:
                cmd_reasons.append((20, "Runs in a hidden window"))
        # odd names
        if ext in EXEC_EXT and looks_random(stem) and not trusted_signer:
            add(15, "Random-looking file name")
        if context == "file" and ext in (".vbs", ".js", ".jse", ".hta", ".wsf") and ("\\downloads\\" in pl or "\\desktop\\" in pl or in_temp):
            add(20, "Script file type often used to deliver malware")
        if os.path.isfile(p) and IS_WIN:
            try:
                import ctypes
                attrs = ctypes.windll.kernel32.GetFileAttributesW(p)
                if attrs != -1 and attrs & 0x2 and ext in EXEC_EXT:
                    add(20, "Hidden program file")
            except Exception:
                pass
        if p and not os.path.exists(p) and context == "startup" and "\\" in p:
            add(10, "Starts a program that no longer exists (leftover)")

        total = sum(x for x, _ in reasons)
        if trusted_signer and total < 100:
            total = min(total, 10)  # signed by a known company: only flag truly strong evidence
            reasons.append((0, f"Signed by {signer_name} (trusted publisher)"))
        total += sum(x for x, _ in cmd_reasons)
        reasons += cmd_reasons
        return total, [w for x, w in sorted(reasons, key=lambda t: -t[0]) if x or total >= 20], h

    # ------------------------------------------------------------ main scan
    def start(self, mode="quick", defender=True):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "phase": "Starting...", "done": 0, "total": 0, "started": time.time(),
                          "finished": None, "mode": mode, "findings": [], "scanned": 0, "error": None,
                          "phase_started": time.time(), "eta_extra": 0}
        self.cancel = False
        threading.Thread(target=self._run, args=(mode, defender), daemon=True).start()
        return True

    def stop(self):
        self.cancel = True

    def _add(self, f):
        with self.lock:
            self.state["findings"].append(f)

    def _run(self, mode, defender):
        try:
            bad, trusted = self.bad_hashes(), self.trusted()
            quarantined = {q["original"].lower() for q in self._load("quarantine.json", [])}
            self._phase("Checking programs that start with Windows...")
            startup = self.startup_items()
            self._phase("Checking running programs...")
            procs = self.processes()
            self._phase("Finding files to check...")
            files = self.walk_files(mode)
            exe_like = sorted({p for p in files if os.path.splitext(p)[1].lower() in (".exe", ".dll", ".scr", ".sys", ".msi")} |
                              {i["file"] for i in startup if i.get("file") and os.path.isfile(i["file"])} |
                              {pr["ExecutablePath"] for pr in procs if pr.get("ExecutablePath")})
            self._phase("Checking digital signatures...", total=len(exe_like), eta_extra=len(files) * 0.01)
            sigs = self.signatures(exe_like)
            seen = set()

            def finding(kind, path, sc, why, h, **extra):
                key = (kind, (path or "").lower(), extra.get("entry", ""))
                if key in seen or sc < 20:
                    return
                seen.add(key)
                level = "high" if sc >= 70 else "medium" if sc >= 40 else "low"
                self._add(dict(id=uuid.uuid4().hex[:10], kind=kind, path=path, score=sc, level=level, reasons=why,
                               sha256=h, quarantined=(path or "").lower() in quarantined, **extra))

            self._phase("Scoring startup entries and running programs...", total=len(startup) + len(procs))
            for it in startup:
                f = it.get("file") or ""
                sc, why, h = self.score(f, sigs.get(f.lower()), "startup", bad, trusted, it["command"])
                finding("startup", f, sc, why, h, entry=it["name"], where=it["where"], command=it["command"],
                        startup=it)
                self._tick()
            for pr in procs:
                f = pr.get("ExecutablePath") or ""
                sc, why, h = self.score(f, sigs.get(f.lower()), "process", bad, trusted, pr.get("CommandLine") or "")
                finding("process", f, sc, why, h, entry=pr.get("Name"), pid=pr.get("ProcessId"),
                        command=pr.get("CommandLine") or "")
                self._tick()
            self._phase("Checking files...", total=len(files))
            for p in files:
                if self.cancel:
                    break
                sc, why, h = self.score(p, sigs.get(p.lower()), "file", bad, trusted)
                finding("file", p, sc, why, h)
                self._tick()
                with self.lock:
                    self.state["scanned"] += 1
            if defender and not self.cancel:
                sus = [f for f in self.state["findings"] if f["score"] >= 40 and f.get("path") and os.path.isfile(f["path"])]
                self._phase("Getting a second opinion from the Defender engine...", total=len(sus))
                for f in sus:
                    if self.cancel:
                        break
                    threat = self.defender_check(f["path"])
                    if threat:
                        f["score"] = max(f["score"], 100)
                        f["level"] = "high"
                        f["reasons"].insert(0, f"Microsoft Defender engine identifies it as: {threat}")
                    else:
                        f["reasons"].append("Defender engine found nothing (it can still be unwanted)")
                    self._tick()
            with self.lock:
                self.state["scanned"] = len(files) + len(startup) + len(procs)
                self.state["phase"] = "Stopped" if self.cancel else "Done"
        except Exception as e:
            with self.lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
                self.state["phase"] = "Error"
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["finished"] = time.time()
            self._save("last-scan.json", self.status())

    # ------------------------------------------------------------ investigation (fed to the AI)
    def investigate(self, fid):
        """Gather extra facts about one finding so the AI (and you) can judge it."""
        f = self.find(fid)
        if not f:
            return None
        info = {k: f.get(k) for k in ("kind", "path", "score", "level", "reasons", "sha256", "entry", "where", "command", "pid")}
        p = f.get("path") or ""
        if p and os.path.isfile(p):
            st = os.stat(p)
            info["size_kb"] = round(st.st_size / 1024, 1)
            info["modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            info["created"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(getattr(st, "st_birthtime", st.st_ctime)))
        if IS_WIN and p:
            q = p.replace("'", "''")
            pid = int(f["pid"]) if f.get("pid") else 0
            out = _ps(
                f"$p='{q}'; $o=[ordered]@{{}}; "
                "if (Test-Path -LiteralPath $p) { $v=(Get-Item -LiteralPath $p).VersionInfo; "
                "$o.company=$v.CompanyName; $o.product=$v.ProductName; $o.description=$v.FileDescription; $o.version=$v.FileVersion; $o.original_name=$v.OriginalFilename; "
                "$s=Get-AuthenticodeSignature -LiteralPath $p; $o.signature=[string]$s.Status; "
                "if ($s.SignerCertificate) { $o.signed_by=$s.SignerCertificate.Subject; $o.cert_valid_until=$s.SignerCertificate.NotAfter.ToString('yyyy-MM-dd') } }; "
                f"$procs=@(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq $p -or ({pid} -gt 0 -and $_.ProcessId -eq {pid}) }}); "
                "$o.running_copies=$procs.Count; "
                "$o.started_by=@($procs | ForEach-Object { $pp=Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.ParentProcessId)\" -ErrorAction SilentlyContinue; if ($pp) { $pp.Name } } | Select-Object -Unique); "
                "$o.network=@($procs | ForEach-Object { Get-NetTCPConnection -OwningProcess $_.ProcessId -State Established -ErrorAction SilentlyContinue } | "
                "Select-Object -First 8 | ForEach-Object { \"$($_.RemoteAddress):$($_.RemotePort)\" }); "
                "$o | ConvertTo-Json -Compress -Depth 3", 120)
            try:
                info.update(json.loads(out))
            except Exception:
                pass
        if info.get("sha256"):
            info["lookup_online"] = f"https://www.virustotal.com/gui/file/{info['sha256']}"
        return info

    # ------------------------------------------------------------ actions
    def find(self, fid):
        with self.lock:
            for f in self.state["findings"]:
                if f["id"] == fid:
                    return f
        return None

    def quarantine(self, fid):
        f = self.find(fid)
        if not f or not f.get("path") or not os.path.isfile(f["path"]):
            return {"error": "That file isn't there any more."}
        if f.get("pid") and IS_WIN:
            subprocess.run(["taskkill", "/F", "/PID", str(f["pid"])], capture_output=True, creationflags=NOWIN)
        # also stop any other running copy of the same file
        if IS_WIN:
            _ps(f"Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq '{f['path'].replace(chr(39), chr(39) * 2)}' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }", 60)
        self.qdir.mkdir(exist_ok=True)
        qid = uuid.uuid4().hex[:12]
        dest = self.qdir / f"{qid}.quarantined"
        try:
            shutil.move(f["path"], dest)
        except Exception as e:
            return {"error": f"Couldn't move it ({e}). It may be in use or need admin rights."}
        q = self._load("quarantine.json", [])
        q.append({"qid": qid, "original": f["path"], "sha256": f.get("sha256"), "reasons": f["reasons"][:3],
                  "time": time.strftime("%Y-%m-%d %H:%M"), "file": str(dest)})
        self._save("quarantine.json", q)
        f["quarantined"] = True
        res = {"ok": True, "message": "Moved to quarantine. It can't run, and you can restore it later."}
        if f["kind"] == "startup":
            self.disable(fid)  # its startup entry would just error otherwise
        return res

    def disable(self, fid):
        f = self.find(fid)
        if not f or f["kind"] != "startup":
            return {"error": "Only startup entries can be disabled."}
        it = f["startup"]
        saved = self._load("disabled.json", [])
        try:
            if it["kind"] == "run" and IS_WIN:
                import winreg
                hive = winreg.HKEY_CURRENT_USER if it["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
                k = winreg.OpenKey(hive, it["key"], 0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(k, it["name"])
            elif it["kind"] == "startup-file":
                self.qdir.mkdir(exist_ok=True)
                dest = self.qdir / (uuid.uuid4().hex[:12] + ".startup")
                shutil.move(it["entry_path"], dest)
                it["moved_to"] = str(dest)
            elif it["kind"] == "task" and IS_WIN:
                r = subprocess.run(["schtasks", "/Change", "/TN", it["task"], "/Disable"], capture_output=True,
                                   text=True, creationflags=NOWIN)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout).strip())
        except PermissionError:
            return {"error": "Windows needs admin rights to change that one (it's a machine-wide entry)."}
        except Exception as e:
            return {"error": f"Couldn't disable it: {e}"}
        saved.append(dict(it, time=time.strftime("%Y-%m-%d %H:%M"), did=uuid.uuid4().hex[:10]))
        self._save("disabled.json", saved)
        f["disabled"] = True
        return {"ok": True, "message": "It won't start with Windows any more. You can turn it back on later."}

    def trust(self, fid):
        f = self.find(fid)
        if not f:
            return {"error": "Not found."}
        t = self.trusted()
        if f.get("sha256"):
            t["hashes"].append(f["sha256"])
        if f.get("path"):
            t["paths"].append(f["path"])
        self._save("trusted.json", t)
        with self.lock:
            self.state["findings"] = [x for x in self.state["findings"] if x["id"] != fid]
        return {"ok": True, "message": "Marked as safe. Future scans will skip it."}

    def quarantine_list(self):
        return {"quarantined": self._load("quarantine.json", []), "disabled": self._load("disabled.json", [])}

    def restore(self, qid):
        q = self._load("quarantine.json", [])
        for item in q:
            if item["qid"] == qid:
                if os.path.exists(item["original"]):
                    return {"error": "A file already exists at the original location."}
                os.makedirs(os.path.dirname(item["original"]), exist_ok=True)
                shutil.move(item["file"], item["original"])
                self._save("quarantine.json", [x for x in q if x["qid"] != qid])
                return {"ok": True, "message": f"Restored to {item['original']}"}
        return {"error": "Not found in quarantine."}

    def enable(self, did):
        saved = self._load("disabled.json", [])
        for it in saved:
            if it.get("did") == did:
                try:
                    if it["kind"] == "run" and IS_WIN:
                        import winreg
                        hive = winreg.HKEY_CURRENT_USER if it["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
                        k = winreg.OpenKey(hive, it["key"], 0, winreg.KEY_SET_VALUE)
                        winreg.SetValueEx(k, it["name"], 0, winreg.REG_SZ, it["command"])
                    elif it["kind"] == "startup-file" and it.get("moved_to"):
                        shutil.move(it["moved_to"], it["entry_path"])
                    elif it["kind"] == "task" and IS_WIN:
                        subprocess.run(["schtasks", "/Change", "/TN", it["task"], "/Enable"], capture_output=True,
                                       creationflags=NOWIN)
                except Exception as e:
                    return {"error": f"Couldn't turn it back on: {e}"}
                self._save("disabled.json", [x for x in saved if x.get("did") != did])
                return {"ok": True, "message": "Turned back on."}
        return {"error": "Not found."}
