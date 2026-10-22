"""Windows debloat / app cleanup for Local AI.

Wraps tools/pc-cleanup/cleanup.ps1:
  - scan():   lists every installed app with its size and when it was last opened
  - remove(): removes the apps / applies the tweaks you picked (protected things are always skipped)
Both run in the background with progress, elapsed time and an ETA, like the security scanner.

Safety:
  - The client only sends IDs. What actually runs (package names, uninstall commands) always comes
    from this PC's own scan, never from the request.
  - Drivers, Windows parts, runtimes, Steam, Ollama, Python, Git, Edge's engine etc. are protected
    here AND inside cleanup.ps1.
  - Removing needs the PIN (checked in app.py). Desktop programs whose uninstaller opens a window
    can only be removed when you're at the PC.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

PROTECTED = re.compile(
    r"nvidia|\bamd\b|radeon|realtek|\bintel|armoury|\basus|\brog\b|steam|ollama|python|\bgit\b|visual c\+\+|\.net|"
    r"windows|driver|dolby|nahimic|claude|microsoft edge|webview|microsoft store|defender|security|directx|vulkan|"
    r"openal|rockstar|epic games|\bea app|tailscale|local ai", re.I)

# Microsoft extras that ship with Windows and that most people never use. Safe to remove; all can be
# reinstalled from the Microsoft Store.
KNOWN_BLOAT = {
    "Microsoft.BingNews": "News app (Microsoft Start)",
    "Microsoft.BingWeather": "Weather app",
    "Microsoft.BingSearch": "Bing search add-on",
    "Microsoft.GetHelp": "Get Help app",
    "Microsoft.Getstarted": "Tips app",
    "Microsoft.MicrosoftSolitaireCollection": "Solitaire (has ads)",
    "Microsoft.People": "People app",
    "Microsoft.WindowsFeedbackHub": "Feedback Hub",
    "Microsoft.WindowsMaps": "Maps",
    "Microsoft.ZuneVideo": "Movies & TV",
    "Microsoft.MicrosoftOfficeHub": "Microsoft 365 (Office) ad hub",
    "Microsoft.549981C3F5F10": "Cortana",
    "Microsoft.PowerAutomateDesktop": "Power Automate",
    "Microsoft.Todos": "Microsoft To Do",
    "Microsoft.OutlookForWindows": "new Outlook",
    "Microsoft.Windows.DevHome": "Dev Home",
    "Microsoft.YourPhone": "Phone Link",
    "MicrosoftWindows.Client.WebExperience": "Widgets",
    "MicrosoftCorporationII.QuickAssist": "Quick Assist",
    "MicrosoftTeams": "Teams (personal)",
    "MSTeams": "Teams (personal)",
    "Clipchamp.Clipchamp": "Clipchamp video editor",
    "Microsoft.MixedReality.Portal": "Mixed Reality Portal",
    "Microsoft.SkypeApp": "Skype",
}

# Reversible settings, offered alongside apps.
TWEAKS = [
    {"id": "ads-off", "kind": "tweak", "name": "Turn off Start menu ads, tips and auto-installed promo apps"},
    {"id": "bing-search-off", "kind": "tweak", "name": "Stop Bing web results in Start menu search"},
    {"id": "background-apps-off", "kind": "tweak", "name": "Stop Store apps running in the background"},
    {"id": "edge", "kind": "edge-quiet", "name": "Stop Edge starting with Windows / running in the background"},
    {"id": "onedrive", "kind": "special", "name": "Remove OneDrive (your online files stay safe)"},
]


def _days_since(date_str):
    try:
        return (time.time() - time.mktime(time.strptime(date_str[:10], "%Y-%m-%d"))) / 86400
    except (TypeError, ValueError):
        return None


class Debloat:
    def __init__(self, home):
        self.home = Path(home)
        self.script = self.home / "tools" / "pc-cleanup" / "cleanup.ps1"
        self.data = self.home / "cleanup"
        self.data.mkdir(exist_ok=True)
        self.apps_path = self.data / "installed-apps.json"
        self.lock = threading.Lock()
        self.job = {"running": False, "kind": None, "started": None, "finished": None, "pct": 0,
                    "phase": "", "log": [], "error": None, "results": []}
        self.proc = None

    # ------------------------------------------------------------ data
    def apps(self):
        """Installed apps from the last scan, with a suggestion for each."""
        try:
            raw = json.loads(self.apps_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if isinstance(raw, dict):
            raw = [raw]
        out = []
        for a in raw:
            name, aid = a.get("name") or "", a.get("id") or ""
            protected = bool(a.get("protected") or PROTECTED.search(name) or PROTECTED.search(aid))
            if aid in KNOWN_BLOAT:  # known Windows extras are always removable
                protected, name = False, KNOWN_BLOAT[aid]
            days = _days_since(a.get("last_used"))
            why, suggest = [], False
            if not protected:
                if aid in KNOWN_BLOAT:
                    why.append(f"Windows extra: {KNOWN_BLOAT[aid]}")
                    suggest = True
                if not a.get("last_used"):
                    why.append("never opened (that Windows knows of)")
                    suggest = suggest or a.get("kind") == "uninstall" or aid in KNOWN_BLOAT
                elif days and days > 180:
                    why.append(f"last opened {int(days // 30)} months ago")
                    suggest = True
            out.append({
                "key": f"{a.get('kind')}:{aid}", "name": name, "kind": a.get("kind"), "id": aid,
                "publisher": a.get("publisher") or "", "size_mb": a.get("size_mb"),
                "last_used": a.get("last_used"), "times_opened": a.get("times_opened"),
                "protected": protected, "suggest": suggest, "why": why,
                "needs_pc": a.get("kind") == "uninstall",
            })
        out.sort(key=lambda x: (x["protected"], not x["suggest"], x["name"].lower()))
        return out

    def scanned_at(self):
        try:
            return self.apps_path.stat().st_mtime
        except OSError:
            return None

    # ------------------------------------------------------------ jobs
    def status(self):
        with self.lock:
            j = dict(self.job, log=self.job["log"][-60:])
        now = time.time()
        if j["started"]:
            j["elapsed"] = (j["finished"] or now) - j["started"]
            j["eta"] = (j["elapsed"] / j["pct"] * (100 - j["pct"])) if j["running"] and j["pct"] >= 3 else None
        j["scanned_at"] = self.scanned_at()
        j["tweaks"] = TWEAKS
        j["apps"] = self.apps()
        return j

    def _run(self, kind, args, items=None):
        with self.lock:
            self.job = {"running": True, "kind": kind, "started": time.time(), "finished": None, "pct": 0,
                        "phase": "Starting...", "log": [], "error": None, "results": []}
        try:
            if os.name != "nt":
                raise RuntimeError("App cleanup only works on Windows.")
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script),
                   "-NoPause", "-Out", str(self.apps_path)] + args
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace",
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in self.proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                with self.lock:
                    if line.startswith("PROGRESS|"):
                        _, pct, text = (line.split("|", 2) + ["", ""])[:3]
                        try:
                            pct = int(pct)
                        except ValueError:
                            pct = self.job["pct"]
                        # removal is the first 70%, the rescan the last 30%
                        if kind == "remove":
                            pct = int(pct * 0.7) if "Removing" in text else 70 + int(pct * 0.3)
                        self.job["pct"], self.job["phase"] = max(self.job["pct"], pct), text
                    else:
                        msg = re.sub(r"^\d\d:\d\d:\d\d\s+", "", line)
                        self.job["log"].append(msg)
                        if msg.strip().startswith(("removed", "skipped", "failed", "not installed", "winget could not",
                                                   "uninstaller finished", "no uninstaller", "Start menu", "Store apps",
                                                   "Edge will", "unknown")):
                            self.job["results"].append(msg.strip())
            self.proc.wait()
            if self.proc.returncode not in (0, None):
                self.job["error"] = f"The cleanup script stopped with code {self.proc.returncode}."
        except Exception as e:
            with self.lock:
                self.job["error"] = str(e)
        finally:
            if items is not None:
                try:
                    os.unlink(items)
                except OSError:
                    pass
            with self.lock:
                self.job.update(running=False, finished=time.time(), pct=100 if not self.job["error"] else self.job["pct"],
                                phase="Done" if not self.job["error"] else "Stopped")
            self.proc = None

    def scan(self):
        if self.job["running"]:
            return False
        threading.Thread(target=self._run, args=("scan", ["-ScanOnly"]), daemon=True).start()
        return True

    def scan_blocking(self, progress=None, max_age=6 * 3600):
        """Used by chat: rescan if the list is missing or old, then return the apps."""
        at = self.scanned_at()
        if at and time.time() - at < max_age and not self.job["running"]:
            return self.apps()
        if not self.job["running"]:
            self.scan()
        while self.job["running"]:
            if progress:
                progress(self.job["phase"] or "Scanning installed apps")
            time.sleep(1.5)
        return self.apps()

    def plan(self, keys, local):
        """Turn requested keys into the exact items to run, using only this PC's own scan data."""
        by_key = {a["key"]: a for a in (self.apps() or [])}
        raw = {}
        try:
            for a in json.loads(self.apps_path.read_text(encoding="utf-8-sig")):
                raw[f"{a.get('kind')}:{a.get('id')}"] = a
        except (OSError, ValueError, AttributeError):
            pass
        tweaks = {f"{t['kind']}:{t['id']}": t for t in TWEAKS}
        items, refused = [], []
        for k in keys:
            if k in tweaks:
                t = tweaks[k]
                items.append({"name": t["name"], "kind": t["kind"], "id": t["id"]})
                continue
            a = by_key.get(k)
            if not a:
                refused.append(f"{k}: not in the app list (scan again)")
            elif a["protected"]:
                refused.append(f"{a['name']}: protected - Windows or your games/tools need it")
            elif a["needs_pc"] and not local:
                refused.append(f"{a['name']}: its uninstaller opens a window, so remove it from the PC")
            elif a["kind"] == "appx":
                items.append({"name": a["name"], "kind": "appx", "id": a["id"]})
            elif a["kind"] == "uninstall":
                items.append({"name": a["name"], "kind": "uninstall", "id": a["id"],
                              "command": (raw.get(k) or {}).get("command")})
        return items, refused

    def remove(self, keys, local):
        if self.job["running"]:
            return {"error": "Cleanup is already running."}
        items, refused = self.plan(keys, local)
        if not items:
            return {"error": "Nothing to remove." + (" " + "; ".join(refused) if refused else "")}
        fd, path = tempfile.mkstemp(suffix=".json", prefix="lai-remove-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f)
        threading.Thread(target=self._run, args=("remove", ["-List", path], path), daemon=True).start()
        return {"ok": True, "count": len(items), "refused": refused}

    def stop(self):
        p = self.proc
        if p:
            try:
                p.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------ for the AI
    def summary_for_ai(self, apps):
        if not apps:
            return "(No app list yet.)"
        sug = [a for a in apps if a["suggest"]]
        prot = sum(1 for a in apps if a["protected"])
        lines = [f"{len(apps)} apps installed; {len(sug)} suggested for removal; {prot} protected (never removed)."]
        lines.append("SUGGESTED (name | size | last opened | why):")
        for a in sug[:45]:
            lines.append(f"- {a['name']} | {str(a['size_mb']) + ' MB' if a['size_mb'] else '?'} | "
                         f"{a['last_used'] or 'never'} | {', '.join(a['why'])}")
        others = [a for a in apps if not a["suggest"] and not a["protected"]]
        lines.append("OTHER REMOVABLE APPS (used recently): " + ", ".join(a["name"] for a in others[:60]))
        lines.append("SETTINGS THAT CAN BE TURNED OFF: " + "; ".join(t["name"] for t in TWEAKS))
        return "\n".join(lines)


CLEANUP_QUESTION = re.compile(
    r"\b(de-?bloa?te?|bloat(ware)?|unused (apps|programs)|apps? (that )?i (never|don'?t|do not) use|"
    r"uninstall|remove (some |the |my )?(apps|programs|software)|clean ?up (my )?(pc|laptop|computer|apps|windows)|"
    r"(what|which) (apps|programs) (can|should) i (remove|delete|uninstall)|free up (disk )?space|"
    r"installed (apps|programs))\b", re.I)
