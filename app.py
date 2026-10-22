#!/usr/bin/env python3
"""
app.py - "Local AI" app server.

Runs on your PC and serves the app to:
  - your PC (opens in its own window), and
  - your phone (iPhone or Android) on the same Wi-Fi or over Tailscale, protected by a PIN.

Two modes:
  Chat - talk to a local model (optionally with web search).
  Code - hands the job to coder_team.py (writer -> test run -> reviewer) and shows progress live.

Only standard-library Python. Put this next to coder_team.py and ui.html, then run:  python app.py
Options:
  --no-phone     only allow this PC (no phone access)
  --port 8765    change the port
  --no-window    don't open the PC window automatically
"""

import argparse
import hashlib
import http.cookies
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coder_team as ct  # noqa: E402  (reuses Ollama + web search helpers)
import scanner  # noqa: E402  (our own malware finder)
import pcinfo  # noqa: E402  (read-only PC diagnostics the AI can use)
import files  # noqa: E402  (safe, read-only access to your folders)
import urllib.parse  # noqa: E402

SCAN = scanner.Scanner(HERE)

SECURITY_SYSTEM = """You are a calm, practical computer-security assistant. The user's own malware scanner flagged
an item on their Windows PC. Using ONLY the facts provided, explain in plain English:
1. What this most likely is (name the real program if the facts make it clear).
2. Verdict: "Likely safe", "Unwanted / not needed", "Suspicious", or "Likely malware" - and why, pointing to specific facts.
3. What to do: keep it, Trust it, Disable it from starting, or Quarantine it.
Be honest about uncertainty. Never tell the user to run commands or download "removal tools". Keep it short."""

PERF_WORDS = re.compile(r"\b(slow|lag|speed|fast|gpu|vram|ram|memory|cpu|temperature|hot|fan|performance|fps|freez)", re.I)

APP_VERSION = "3.6"
RESTARTING = False
CONFIG_PATH = HERE / "app-config.json"
OUTPUT_DIR = HERE / "output"
LEARN_DIR = HERE / "learning"
UPLOAD_DIR = HERE / "uploads"

CHAT_SYSTEM = """You are a helpful, friendly assistant for everyday tasks, running privately on the user's
own PC. Be quick and to the point: short answers unless asked for detail. Use Markdown lists when helpful."""

SEARCH_SYSTEM = """You are a research assistant. Answer the user's question using ONLY the numbered web
results below. Cite sources inline like [1] or [2]. If the results don't contain the answer, say so plainly
instead of guessing. Lead with the direct answer, then key details. Be concise."""

# Which models belong to which category, best first. The first one installed is the default.
CATEGORIES = {
    # Ordered so models that fit fully on a 6 GB graphics card come first (much faster).
    "daily":    {"label": "Everyday (fast)", "prefs": ["ajax", "qwen3:4b", "llama3.1:8b", "qwen3:8b", "gemma4:e4b"]},
    "search":   {"label": "Search & research", "prefs": ["qwen3:4b", "llama3.1:8b", "qwen3:8b", "gemma4:e4b"]},
    "writer":   {"label": "Coding", "prefs": ["qwen3:8b", "qwen2.5-coder:7b", "qwen2.5-coder:1.5b"]},
    "reviewer": {"label": "Code review & debugging", "prefs": ["llama3.1:8b", "qwen2.5-coder:7b", "deepseek-r1:7b", "gemma4:e4b"]},
}
# What each model is best at (for grouping the menus).
MODEL_GROUP = [
    ("qwen2.5-coder", "Coding"), ("deepseek-r1", "Reasoning & debugging"), ("qwen3:8b", "Coding"),
    ("qwen3:4b", "Everyday (fast)"), ("llama3.1", "Search & research"), ("gemma4", "Too big for 6 GB (slow)"),
    ("27b", "Too big for 6 GB (slow)"), ("30b", "Too big for 6 GB (slow)"), ("32b", "Too big for 6 GB (slow)"),
    ("ajax", "Everyday (fast)"),
]
KEEP_ALIVE = "30m"  # keep a model in memory between messages so replies start fast


def model_group(name):
    for key, group in MODEL_GROUP:
        if key in name.lower():
            return group
    return "Other"


# ---------------------------------------------------------------- learning (feedback memory)
LEARN_LOCK = threading.Lock()
LEARN_CATS = ("daily", "search", "code")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#._-]{2,}")
STOP = set("the and for with that this what how are you can your from have will into about there them then than "
           "make write give tell want need please using use like just".split())


def words(text):
    return {w for w in WORD_RE.findall((text or "").lower()) if w not in STOP}


def learn_add(cat, entry):
    if cat not in LEARN_CATS:
        return
    LEARN_DIR.mkdir(exist_ok=True)
    entry = dict(entry, time=int(time.time()))
    with LEARN_LOCK, open(LEARN_DIR / f"{cat}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def learn_load(cat):
    path = LEARN_DIR / f"{cat}.jsonl"
    if not path.exists():
        return []
    out = []
    with LEARN_LOCK:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def lessons_for(cat, query, k=4):
    """Pick the past feedback most related to this request and turn it into instructions."""
    if not CFG.get("learning", True):
        rules = ""
    else:
        q = words(query)
        scored = []
        for e in learn_load(cat):
            overlap = len(q & words(e.get("question", "")))
            if e.get("rating", 0) < 0 and e.get("note"):
                overlap += 0.5  # corrections matter more
            if overlap >= 1:
                scored.append((overlap, e.get("time", 0), e))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        lines = []
        for _, _, e in scored[:k]:
            qtext = e.get("question", "")[:160].replace("\n", " ")
            if e.get("rating", 0) < 0:
                fix = e.get("note") or "it was not helpful; do better this time"
                lines.append(f"- For a similar request (\"{qtext}\"), the user disliked the answer. Their correction: {fix[:400]}")
            else:
                lines.append(f"- For a similar request (\"{qtext}\"), the user liked this answer - match its approach:\n"
                             f"  {e.get('answer', '')[:700]}")
        rules = ("LESSONS FROM THE USER'S PAST FEEDBACK (follow them):\n" + "\n".join(lines)) if lines else ""
    custom = (CFG.get("rules") or {}).get(cat, "").strip()
    if custom:
        rules = f"THE USER'S STANDING RULES (always follow):\n{custom[:1500]}\n\n" + rules
    return rules.strip()


def pick(category, models):
    for pref in CATEGORIES[category]["prefs"]:
        for m in models:
            if m.lower() == pref or m.lower().startswith(pref + ":") or (":" not in pref and pref in m.lower()):
                return m
    return models[0] if models else ""


# ---------------------------------------------------------------- config / auth

def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    changed = False
    if "pin" not in cfg:
        cfg["pin"] = f"{secrets.randbelow(10**6):06d}"
        changed = True
    if "secret" not in cfg:
        cfg["secret"] = secrets.token_hex(16)
        changed = True
    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


CFG = load_config()
FAILED_PINS = {}  # ip -> (count, first_time)
DOWNLOAD_LINKS = {}  # token -> (path, expires): lets the phone app hand a download to the phone's browser


def session_token():
    # Changes whenever the PIN changes, so a new PIN logs out old phones.
    return hashlib.sha256(f"{CFG['secret']}:{CFG['pin']}".encode()).hexdigest()


def local_ips():
    """LAN and Tailscale addresses of this PC, for showing phone links."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the main network card
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.discard("127.0.0.1")
    lan = sorted(ip for ip in ips if not ip.startswith("100."))
    tail = sorted(ip for ip in ips if ip.startswith("100."))  # Tailscale uses 100.x.x.x
    return lan, tail


# ---------------------------------------------------------------- Ollama

def ollama_models():
    try:
        with urllib.request.urlopen(f"{ct.OLLAMA_URL}/api/tags", timeout=5) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])], None
    except Exception as e:
        return [], f"Can't reach Ollama ({e}). Make sure the Ollama app is running."


GPU_STATE = {"cpu": False}


def stream_ollama_chat(model, messages, cpu, ctx=8192):
    """Yields ('text', str) / ('error', str). Hides any <think> sections."""
    options = {"temperature": 0.6, "num_ctx": ctx}
    if cpu:
        options["num_gpu"] = 0
    body = {"model": model, "messages": messages, "stream": True, "options": options, "keep_alive": KEEP_ALIVE}
    if "qwen3" in model.lower():
        body["think"] = False
        # Qwen3's own switch: "/no_think" in the last user message stops it writing its reasoning out loud.
        msgs = [dict(m) for m in messages]
        for m in reversed(msgs):
            if m.get("role") == "user":
                m["content"] = m["content"].rstrip() + " /no_think"
                break
        body["messages"] = msgs
    req = urllib.request.Request(f"{ct.OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    buf, in_think = "", False
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    yield "error", chunk["error"]
                    return
                buf += chunk.get("message", {}).get("content", "")
                out = []
                while buf:
                    if in_think:
                        end = buf.find("</think>")
                        if end == -1:
                            buf = buf[-10:]
                            break
                        buf, in_think = buf[end + 8:], False
                    else:
                        start = buf.find("<think>")
                        if start == -1:
                            lt = buf.rfind("<")
                            cut = lt if lt != -1 and len(buf) - lt < 7 else len(buf)
                            out.append(buf[:cut])
                            buf = buf[cut:]
                            break
                        out.append(buf[:start])
                        buf, in_think = buf[start + 7:], True
                if out and "".join(out):
                    yield "text", "".join(out)
                if chunk.get("done"):
                    break
        if buf and not in_think:
            yield "text", buf
    except urllib.error.HTTPError as e:
        yield "error", f"HTTP {e.code}: {e.read().decode(errors='ignore')}"
    except urllib.error.URLError as e:
        yield "error", f"Can't reach Ollama: {e.reason}. Is the Ollama app running?"


# ---------------------------------------------------------------- code jobs

JOBS = {}  # job_id -> Popen, so the Stop button can end them
JOBS_LOCK = threading.Lock()


def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file.txt").strip("._") or "file.txt"
    return name[:80]


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "LocalAI/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---- helpers
    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def authed(self):
        if self.is_local():
            return True
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c.get("lai") and c["lai"].value
        return bool(tok) and secrets.compare_digest(tok, session_token())

    def send_json(self, obj, code=200, extra_headers=None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self, limit=2_000_000):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > limit:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def emit(self, obj):
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise BrokenPipeError()  # the viewer closed the page; stop quietly

    # ---- routes
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/files/download" and "t=" in self.path:
            tok = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("t") or [""])[0]
            link = DOWNLOAD_LINKS.get(tok)
            if not link or link[1] < time.time():
                return self.send_json({"error": "This download link expired. Tap Download again."}, 403)
            try:
                return self.files_get("/api/files/download", {"path": [link[0]]})
            except PermissionError as e:
                return self.send_json({"error": str(e)}, 403)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
        if path.startswith("/api/files/"):
            if not self.authed():
                return self.send_json({"error": "Locked. Enter the PIN shown on your PC."}, 401)
            try:
                return self.files_get(path, urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            except PermissionError as e:
                return self.send_json({"error": str(e)}, 403)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            except Exception as e:
                return self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        if path in ("/", "/index.html"):
            return self.send_file(HERE / "ui.html", "text/html; charset=utf-8")
        if path == "/manifest.json":
            return self.send_json({
                "name": "Local AI", "short_name": "Local AI", "start_url": "/", "display": "standalone",
                "background_color": "#0f1115", "theme_color": "#0f1115",
                "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
                          {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}]})
        if path in ("/icon.png", "/icon-192.png", "/apple-touch-icon.png"):
            f = HERE / ("icon-192.png" if "192" in path else "icon.png")
            if f.exists():
                return self.send_file(f, "image/png")
            return self.send_response(404) or self.end_headers()
        if path == "/api/diag":
            if not self.is_local():
                return self.send_json({"error": "Only on the PC."}, 403)
            return self.send_json(get_diag())
        if path == "/api/scan/status":
            if not self.authed():
                return self.send_json({"error": "locked"}, 401)
            st = SCAN.status()
            st["can_act"] = self.is_local()
            for f in st["findings"]:
                f.pop("startup", None)
            return self.send_json(st)
        if path == "/api/scan/quarantine":
            if not self.authed():
                return self.send_json({"error": "locked"}, 401)
            return self.send_json(SCAN.quarantine_list())
        if path == "/api/status":
            models, err = ollama_models()
            lan, tail = local_ips()
            port = self.server.server_address[1]
            return self.send_json({
                "authed": self.authed(), "local": self.is_local(), "version": APP_VERSION,
                "models": models if self.authed() else [], "ollama_error": err,
                "defaults": {c: pick(c, models) for c in CATEGORIES},
                "groups": {m: model_group(m) for m in models},
                "learning": self.learning_info() if self.authed() else None,
                "phone": None if not (self.is_local() and self.server.phone) else {
                    "pin": CFG["pin"],
                    "lan": [f"http://{ip}:{port}" for ip in lan],
                    "tailscale": [f"http://{ip}:{port}" for ip in tail]}})
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/login":
            return self.login()
        if not self.authed():
            return self.send_json({"error": "Locked. Enter the PIN shown on your PC."}, 401)
        try:
            if path == "/api/chat":
                return self.chat()
            if path == "/api/code":
                return self.code()
            if path == "/api/stop":
                return self.stop()
            if path == "/api/files/upload":
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > 2_000_000_000:
                    return self.send_json({"error": "That file is bigger than 2 GB."}, 413)
                name = urllib.parse.unquote(self.headers.get("X-Filename", "upload"))
                tmp = files.UPLOAD_DIR / (".incoming-" + secrets.token_hex(4))
                files.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as f:  # stream to disk so big videos don't fill memory
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                dest = files.save_upload(name, b"")
                os.replace(tmp, dest)
                files.INDEX.refresh(CFG, force=True)
                return self.send_json({"ok": True, "saved_to": dest})
            if path == "/api/files/delete":
                req = self.read_json()
                ip = self.client_address[0]
                count, first = FAILED_PINS.get(ip, (0, time.time()))
                if time.time() - first > 600:
                    count, first = 0, time.time()
                if count >= 5:
                    return self.send_json({"error": "Too many wrong PINs. Wait 10 minutes."}, 429)
                if not secrets.compare_digest(str(req.get("pin", "")).strip(), CFG["pin"]):
                    FAILED_PINS[ip] = (count + 1, first)
                    time.sleep(1)
                    return self.send_json({"error": "Wrong PIN - nothing was deleted."}, 403)
                FAILED_PINS.pop(ip, None)
                try:
                    gone = files.to_recycle_bin(CFG, req.get("path"))
                except PermissionError as e:
                    return self.send_json({"error": str(e)}, 403)
                print(f"Moved to Recycle Bin (PIN confirmed): {gone}")
                return self.send_json({"ok": True, "message": "Moved to the Recycle Bin. You can restore it from there."})
            if path == "/api/files/open":
                if not self.is_local():
                    return self.send_json({"error": "Opening files only works on the PC."}, 403)
                try:
                    target = files.resolve(CFG, self.read_json().get("path"))
                except PermissionError as e:
                    return self.send_json({"error": str(e)}, 403)
                if hasattr(os, "startfile"):
                    os.startfile(str(target if target.is_dir() else target.parent))
                return self.send_json({"ok": True})
            if path == "/api/files/settings":
                if not self.is_local():
                    return self.send_json({"error": "Only the PC can change which folders are shared."}, 403)
                roots = [r.strip() for r in self.read_json().get("roots", []) if r.strip()]
                CFG["file_roots"] = roots or files.DEFAULT_ROOTS
                CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
                files.INDEX.refresh(CFG, force=True)
                return self.send_json({"ok": True, "roots": [str(r) for r in files.roots(CFG)]})
            if path.startswith("/api/scan/"):
                return self.scan_route(path)
            if path == "/api/feedback":
                req = self.read_json()
                cat = {"chat": "daily", "daily": "daily", "search": "search", "code": "code"}.get(req.get("category"))
                if not cat:
                    return self.send_json({"error": "bad category"}, 400)
                learn_add(cat, {"question": str(req.get("question", ""))[:2000], "answer": str(req.get("answer", ""))[:3000],
                                "rating": 1 if req.get("rating", 0) > 0 else -1, "note": str(req.get("note", ""))[:1000]})
                return self.send_json({"ok": True})
            if path == "/api/learning":
                req = self.read_json()
                if "rules" in req and isinstance(req["rules"], dict):
                    CFG["rules"] = {k: str(v)[:2000] for k, v in req["rules"].items() if k in LEARN_CATS}
                if "enabled" in req:
                    CFG["learning"] = bool(req["enabled"])
                if req.get("forget") in LEARN_CATS:
                    with LEARN_LOCK:
                        (LEARN_DIR / f"{req['forget']}.jsonl").unlink(missing_ok=True)
                CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
                return self.send_json(self.learning_info())
            if path == "/api/restart":
                if not self.is_local():
                    return self.send_json({"error": "Only the PC can do that."}, 403)
                self.send_json({"ok": True, "message": "Restarting with the newest version..."})
                # Start the new copy FIRST; it sees this older one running and takes over its port.
                # (Shutting down first would end this process before the new one could be launched.)
                global RESTARTING
                RESTARTING = True
                subprocess.Popen([sys.executable, str(HERE / "app.py"), "--no-window"], cwd=str(HERE),
                                 creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                return
            if path == "/api/shutdown":
                if not self.is_local():
                    return self.send_json({"error": "Only the PC can do that."}, 403)
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/new-pin":
                if not self.is_local():
                    return self.send_json({"error": "Only the PC can change the PIN."}, 403)
                CFG["pin"] = f"{secrets.randbelow(10**6):06d}"
                CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
                return self.send_json({"pin": CFG["pin"]})
            if path == "/api/open-folder":
                if not self.is_local():
                    return self.send_json({"error": "Only on the PC."}, 403)
                folder = Path(self.read_json().get("path", "")).resolve()
                if OUTPUT_DIR.resolve() in folder.parents or folder == OUTPUT_DIR.resolve():
                    if hasattr(os, "startfile"):
                        os.startfile(str(folder if folder.is_dir() else folder.parent))
                    return self.send_json({"ok": True})
                return self.send_json({"error": "Not an output folder."}, 400)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        return self.send_json({"error": "not found"}, 404)

    def files_get(self, path, q):
        arg = lambda k: (q.get(k) or [""])[0]
        if path == "/api/files/list":
            return self.send_json(files.list_dir(CFG, arg("path") or None))
        if path == "/api/files/search":
            return self.send_json({"results": files.INDEX.search(CFG, arg("q"))})
        if path == "/api/files/read":
            return self.send_json(files.read_text(CFG, arg("path")))
        if path == "/api/files/settings":
            return self.send_json({"roots": CFG.get("file_roots") or files.DEFAULT_ROOTS,
                                   "resolved": [str(r) for r in files.roots(CFG)], "upload_dir": str(files.UPLOAD_DIR)})
        if path == "/api/files/link":
            p = files.resolve(CFG, arg("path"))
            for t in [t for t, (_, exp) in DOWNLOAD_LINKS.items() if exp < time.time()]:
                DOWNLOAD_LINKS.pop(t, None)
            tok = secrets.token_urlsafe(24)
            DOWNLOAD_LINKS[tok] = (str(p), time.time() + 300)  # valid 5 minutes
            return self.send_json({"url": f"/api/files/download?t={tok}", "name": p.name})
        if path == "/api/files/download":
            p = files.resolve(CFG, arg("path"))
            if p.is_dir():
                return self.send_json({"error": "That's a folder."}, 400)
            size = p.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(p.name))
            self.end_headers()
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        return self.send_json({"error": "not found"}, 404)

    def scan_route(self, path):
        req = self.read_json()
        local = self.is_local()
        if path == "/api/scan/start":
            ok = SCAN.start("full" if req.get("mode") == "full" else "quick", bool(req.get("defender", True)))
            return self.send_json({"ok": ok, "error": None if ok else "A scan is already running."})
        if path == "/api/scan/stop":
            SCAN.stop()
            return self.send_json({"ok": True})
        if path == "/api/scan/explain":
            return self.scan_explain(req.get("id"))
        if not local:
            return self.send_json({"error": "For safety, removing or restoring things only works on the PC itself."}, 403)
        if path == "/api/scan/action":
            act = {"quarantine": SCAN.quarantine, "disable": SCAN.disable, "trust": SCAN.trust}.get(req.get("action"))
            return self.send_json(act(req.get("id")) if act else {"error": "Unknown action."})
        if path == "/api/scan/restore":
            return self.send_json(SCAN.restore(req["qid"]) if req.get("qid") else SCAN.enable(req.get("did")))
        if path == "/api/scan/update-list":
            try:
                return self.send_json(dict(SCAN.update_threat_list(), ok=True))
            except Exception as e:
                return self.send_json({"error": f"Couldn't download the threat list: {e}"})
        return self.send_json({"error": "not found"}, 404)

    def scan_explain(self, fid):
        self.start_stream()
        self.emit({"status": "Investigating (publisher, signature, network, what started it)..."})
        facts = SCAN.investigate(fid)
        if not facts:
            self.emit({"error": "That finding isn't in the current results.", "done": True})
            return
        self.emit({"facts": facts})
        models = ollama_models()[0]
        model = pick("daily", models)
        self.emit({"status": f"{model} is reviewing the evidence..."})
        msgs = [{"role": "system", "content": SECURITY_SYSTEM},
                {"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=1)[:6000]}]
        for kind, val in stream_ollama_chat(model, msgs, GPU_STATE["cpu"], 4096):
            self.emit({"text": val} if kind == "text" else {"error": val})
        self.emit({"done": True})

    @staticmethod
    def learning_info():
        counts = {}
        for c in LEARN_CATS:
            items = learn_load(c)
            counts[c] = {"liked": sum(1 for e in items if e.get("rating", 0) > 0),
                         "corrections": sum(1 for e in items if e.get("rating", 0) < 0)}
        return {"enabled": CFG.get("learning", True), "rules": CFG.get("rules", {}), "counts": counts}

    def login(self):
        ip = self.client_address[0]
        count, first = FAILED_PINS.get(ip, (0, time.time()))
        if time.time() - first > 600:
            count, first = 0, time.time()
        if count >= 5:
            return self.send_json({"error": "Too many wrong PINs. Wait 10 minutes."}, 429)
        pin = str(self.read_json().get("pin", "")).strip()
        if secrets.compare_digest(pin, CFG["pin"]):
            FAILED_PINS.pop(ip, None)
            cookie = f"lai={session_token()}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
            return self.send_json({"ok": True}, extra_headers={"Set-Cookie": cookie})
        FAILED_PINS[ip] = (count + 1, first)
        time.sleep(1)
        return self.send_json({"error": "Wrong PIN."}, 403)

    def chat(self):
        req = self.read_json()
        mode = "search" if req.get("mode") == "search" else "daily"
        models = ollama_models()[0]
        model = req.get("model") or pick(mode, models)
        history = [m for m in req.get("messages", []) if m.get("role") in ("user", "assistant")][-20:]
        self.start_stream()
        sources, ctx = [], 4096
        system = CHAT_SYSTEM
        if mode == "search" and history:
            ctx = 8192
            q = history[-1]["content"][:200]
            self.emit({"status": "Searching the web..."})
            sargs = types.SimpleNamespace(engine_list=ct.DEFAULT_ENGINES.split(","), searx=None)
            results = ct.web_search(q, sargs, n=6)
            if results:
                self.emit({"status": f"Reading {min(3, len(results))} pages..."})
                blocks = []
                for i, r in enumerate(results, 1):
                    block = f"[{i}] {r['title']} ({r['url']})\n{r['snippet']}"
                    if i <= 3:
                        block += "\n" + ct.page_excerpt(r["url"], 1400)
                    blocks.append(block)
                system = SEARCH_SYSTEM + "\n\nWEB RESULTS:\n" + "\n\n".join(blocks)[:9000]
                sources = [{"title": r["title"], "url": r["url"]} for r in results]
            else:
                system = CHAT_SYSTEM + "\n\n(A web search found nothing - say so, then answer from general knowledge.)"
        elif req.get("web") and history:  # optional web lookup in everyday chat
            q = history[-1]["content"][:150]
            self.emit({"status": "Searching the web..."})
            sargs = types.SimpleNamespace(engine_list=ct.DEFAULT_ENGINES.split(","), searx=None)
            results = ct.web_search(q, sargs, n=5)
            if results:
                notes = ct.notes_from_results(q, results, fetch_pages=1, page_chars=1000)[:4000]
                system += f"\n\nWEB RESULTS FOR THE USER'S LATEST MESSAGE:\n{notes}"
                sources = [{"title": r["title"], "url": r["url"]} for r in results[:5]]
        checks = pcinfo.pick_checks(history[-1]["content"]) if (mode == "daily" and history and self.is_local()
                                                                 or mode == "daily" and history and req.get("pc_checks")) else []
        if checks:
            facts = pcinfo.run(checks, progress=lambda label: self.emit({"status": f"{label}..."}),
                               question=history[-1]["content"])
            self.emit({"checks": list(facts)})
            system += ("\n\nAnswer the user directly (\"your laptop...\"). Do NOT narrate your thinking or describe the data; "
                       "give: the main reasons it's happening (with the specific numbers/programs), then numbered fixes."
                       "\n\nLIVE DIAGNOSTICS FROM THE USER'S PC (just measured - base your answer on these facts, "
                       "name the specific programs/numbers, and suggest concrete fixes. 'diagtool_*' entries come from the user's own "
                       "Diag-Tool - prefer its fix steps):\n" + json.dumps(facts, default=str)[:7000])
        if req.get("file_path"):  # "Ask AI about this file"
            try:
                doc = files.read_text(CFG, req["file_path"])
                if doc.get("text"):
                    self.emit({"status": f"Reading {doc['name']}..."})
                    system += (f"\n\nTHE USER IS ASKING ABOUT THIS FILE FROM THEIR PC: {doc['name']}\n"
                               f"----- start of file -----\n{doc['text'][:12000]}\n----- end of file -----"
                               + ("\n(The file is longer; only the beginning is shown.)" if doc.get("truncated") or len(doc["text"]) > 12000 else ""))
                else:
                    system += f"\n\n(The user attached {doc['name']}, but it can't be read as text: {doc.get('note', '')})"
            except PermissionError as e:
                self.emit({"error": str(e)})
        elif history and files.FILE_QUESTION.search(history[-1]["content"]):
            terms = files.search_terms(history[-1]["content"])
            self.emit({"status": f"Searching your files for \"{terms}\"..."})
            hits = files.INDEX.search(CFG, terms, limit=15)
            self.emit({"files": hits})
            if re.search(r"\b(delete|remove|trash|get rid of|erase)\b", history[-1]["content"], re.I):
                self.emit({"delete_intent": True})
                system += ("\n\nThe user wants to delete files. You cannot delete anything yourself. The app shows the matching "
                           "files below with a Delete button; deleting needs their PIN and moves the file to the Recycle Bin. "
                           "Tell them which results match and to tap Delete on each one they want gone. Warn if a file looks important.")
            system += ("\n\nFILE SEARCH RESULTS FROM THE USER'S PC (the app shows them as clickable results; "
                       "summarize which look like what they want, mention the folder each is in):\n"
                       + "\n".join(f"- {h['name']}  ({h['path']}, {h['modified']}, {h['size'] or 0} bytes)" for h in hits)
                       if hits else "\n\n(A search of the user's shared folders found no matching files - say so and suggest other words.)")
        lessons = lessons_for(mode, history[-1]["content"] if history else "")
        if lessons:
            system += "\n\n" + lessons
        messages = [{"role": "system", "content": system}] + history
        self.emit({"status": f"{model} is answering..." + (" (on CPU)" if GPU_STATE["cpu"] else "")})
        sent = False
        for _ in range(2):
            err = None
            for kind, val in stream_ollama_chat(model, messages, GPU_STATE["cpu"], ctx):
                if kind == "text":
                    sent = True
                    self.emit({"text": val})
                else:
                    err = val
            if err and not sent and not GPU_STATE["cpu"] and ct.gpu_error(err):
                GPU_STATE["cpu"] = True
                self.emit({"status": "Graphics card busy (a game?) - switching to the CPU. Slower, but it works."})
                continue
            if err:
                self.emit({"error": err})
            break
        self.emit({"done": True, "sources": sources})

    def code(self):
        req = self.read_json(limit=3_000_000)
        task = (req.get("task") or "").strip()
        if not task:
            return self.send_json({"error": "Describe what to build."}, 400)
        job_id = req.get("job") or secrets.token_hex(6)
        cmd = [sys.executable, "-u", str(HERE / "coder_team.py"), task, "--auto", "--out", str(OUTPUT_DIR),
               "--rounds", str(max(1, min(int(req.get("rounds") or 4), 8)))]
        if req.get("writer"):
            cmd += ["--writer", req["writer"]]
        if req.get("reviewer"):
            cmd += ["--reviewer", req["reviewer"]]
        if req.get("lang") in ct.LANGS:
            cmd += ["--lang", req["lang"]]
        if not req.get("web", True):
            cmd += ["--offline"]
        if not req.get("run", True):
            cmd += ["--no-run"]
        if req.get("file_text") is not None:
            UPLOAD_DIR.mkdir(exist_ok=True)
            fpath = UPLOAD_DIR / f"{int(time.time())}_{safe_name(req.get('file_name'))}"
            fpath.write_text(req["file_text"], encoding="utf-8")
            cmd += ["--file", str(fpath)]

        self.start_stream()
        self.emit({"job": job_id, "status": "Starting the coding team..."})
        env = dict(os.environ, PYTHONIOENCODING="utf-8", LAI_LESSONS=lessons_for("code", task))
        proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with JOBS_LOCK:
            JOBS[job_id] = proc
        final_path, approved = None, False
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("Final code:"):
                    final_path = line.split(":", 1)[1].strip()
                if "DONE: approved" in line:
                    approved = True
                stage = self.stage_of(line)
                self.emit({"line": line, **({"status": stage} if stage else {})})
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()
            return
        finally:
            proc.wait()
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
        result = {"done": True, "approved": approved, "stopped": proc.returncode not in (0, None) and not final_path}
        if final_path and Path(final_path).exists():
            result["code"] = Path(final_path).read_text(encoding="utf-8", errors="replace")
            if approved and CFG.get("learning", True):  # remember code that passed tests + review
                learn_add("code", {"question": task, "answer": result["code"][:2500], "rating": 1, "auto": True})
            result["path"] = final_path
            result["filename"] = Path(final_path).name
        self.emit(result)

    @staticmethod
    def stage_of(line):
        m = re.search(r"Round (\d+)/(\d+): (\S+) is (writing|fixing)", line)
        if m:
            return f"Round {m.group(1)} of {m.group(2)}: {m.group(3)} is {m.group(4)} the code..."
        if "Test run PASSED" in line:
            return "Test run passed."
        if "Test run FAILED" in line:
            return "Test run failed - sending the error back to fix..."
        if "is reviewing" in line:
            return "The reviewer is checking the code..."
        if "Researching on the web" in line:
            return "Researching on the web..."
        if "Looking up the error online" in line:
            return "Looking up the error online..."
        if "Switching to the CPU" in line:
            return "Graphics card busy - running on the CPU (slower)..."
        return None

    def stop(self):
        job = self.read_json().get("job")
        with JOBS_LOCK:
            proc = JOBS.get(job)
        if proc:
            proc.kill()
        return self.send_json({"ok": bool(proc)})


class AppServer(ThreadingHTTPServer):
    # On Windows, "reuse address" lets two programs grab the same port at once, so turn it off there.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------- startup

def ensure_icons():
    """Draw simple app icons (no image library needed)."""
    import struct
    import zlib

    def png(size, path):
        if path.exists():
            return
        rows = []
        c = size / 2
        for y in range(size):
            row = bytearray([0])
            for x in range(size):
                # rounded-square background
                r = size * 0.22
                dx = max(abs(x - c + 0.5) - (c - r), 0)
                dy = max(abs(y - c + 0.5) - (c - r), 0)
                inside = dx * dx + dy * dy <= r * r
                # bright ring + dot in the middle
                d = ((x - c + 0.5) ** 2 + (y - c + 0.5) ** 2) ** 0.5
                if not inside:
                    px = (15, 17, 21)
                elif size * 0.26 < d < size * 0.33:
                    px = (124, 196, 255)
                elif d < size * 0.12:
                    px = (255, 138, 101)
                else:
                    px = (24, 27, 34)
                row += bytes(px)
            rows.append(bytes(row))
        raw = zlib.compress(b"".join(rows), 9)

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
        path.write_bytes(data)

    png(512, HERE / "icon.png")
    png(192, HERE / "icon-192.png")


def make_desktop_shortcut():
    """Create a 'Local AI' desktop shortcut on Windows the first time."""
    if os.name != "nt" or CFG.get("shortcut_done"):
        return
    try:
        desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
        bat = HERE / "Local AI.bat"
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{desktop / 'Local AI.lnk'}');"
              f"$s.TargetPath='{bat}';$s.WorkingDirectory='{HERE}';$s.WindowStyle=7;"
              f"$s.IconLocation='{HERE / 'icon.ico'}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        CFG["shortcut_done"] = True
        CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
    except Exception:
        pass


def ensure_ico():
    """Windows shortcuts want a .ico; wrap the 192px PNG in one."""
    import struct
    ico, png = HERE / "icon.ico", HERE / "icon-192.png"
    if ico.exists() or not png.exists():
        return
    data = png.read_bytes()
    header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 192, 192, 0, 0, 1, 32, len(data), 22)
    ico.write_bytes(header + data)


def get_diag():
    """Snapshot of what the PC is doing (graphics card, memory, loaded models)."""
    info = {"version": APP_VERSION, "gpu_fallback_active": GPU_STATE["cpu"]}
    try:
        with urllib.request.urlopen(f"{ct.OLLAMA_URL}/api/ps", timeout=5) as r:
            info["loaded"] = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                               "on_gpu_gb": round(m.get("size_vram", 0) / 1e9, 1)} for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        info["loaded"] = f"error: {e}"
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info["gpu"] = (out.stdout or out.stderr).strip()
        apps = subprocess.run(["nvidia-smi", "--query-compute-apps=process_name,used_memory", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info["gpu_apps"] = [os.path.basename(l.split(",")[0]) + "," + l.split(",")[-1]
                            for l in apps.stdout.strip().splitlines() if l.strip()]
    except Exception as e:
        info["gpu"] = f"nvidia-smi not available: {e}"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "$o=Get-CimInstance Win32_OperatingSystem; $c=(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average;"
                              "'{0:N1} GB free of {1:N1} GB RAM, CPU {2}%' -f ($o.FreePhysicalMemory/1MB), ($o.TotalVisibleMemorySize/1MB), $c"],
                             capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info["ram_cpu"] = out.stdout.strip()
    except Exception as e:
        info["ram_cpu"] = str(e)
    return info


def warm_up():
    """Load the everyday model into memory in the background so the first reply is quick."""
    models, err = ollama_models()
    if err or not models:
        return
    body = json.dumps({"model": pick("daily", models), "keep_alive": KEEP_ALIVE}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"{ct.OLLAMA_URL}/api/generate", data=body,
                               headers={"Content-Type": "application/json"}), timeout=300).read()
    except Exception:
        pass


def is_local_ai(port):
    """Returns the running version if Local AI (not some other program) answers on this port, else None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
            if not r.headers.get("Server", "").startswith("LocalAI"):
                return None
            return json.loads(r.read()).get("version", "old")
    except Exception:
        return None


def stop_old(port):
    """Ask an older running copy to shut down so this newer one can take over."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}",
                               headers={"Content-Type": "application/json"}), timeout=3).read()
        time.sleep(1.5)
        return True
    except Exception:
        pass
    # Older versions had no shutdown command: end the process that owns the port (already verified
    # above to be Local AI, so this never touches other programs).
    if os.name == "nt":
        pids = set()
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                    pids.add(int(parts[4]))
        except Exception as e:
            print(f"  netstat failed: {e}")
        pids.discard(os.getpid())
        for pid in pids:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            print(f"  stopping old copy (pid {pid}): {(r.stdout or r.stderr).strip()}")
        time.sleep(1.5)
        return is_local_ai(port) is None
    return False


def open_window(url):
    """Open the app in its own window (Edge/Chrome 'app mode'), else the normal browser."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",       # preferred
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",  # fallback only
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if Path(exe).exists():
                subprocess.Popen([exe, f"--app={url}", "--window-size=1100,800"])
                return
    import webbrowser
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description="Local AI app server")
    ap.add_argument("--port", type=int, default=0, help="Port (default: remembered, else 8790+).")
    ap.add_argument("--no-phone", action="store_true", help="Only allow this PC.")
    ap.add_argument("--no-window", action="store_true", help="Don't open the PC window.")
    args = ap.parse_args()

    ensure_icons()
    ensure_ico()
    host = "127.0.0.1" if args.no_phone else "0.0.0.0"
    first = args.port or CFG.get("port", 8790)
    server = None
    for port in [first] + [p for p in range(8790, 8800) if p != first]:
        running = is_local_ai(port)
        if running:
            print(f"Port {port}: Local AI version {running} is running.")
        if running == APP_VERSION and not args.no_window:
            print("Local AI is already running - opening the window.")
            open_window(f"http://127.0.0.1:{port}")
            return
        if running:
            ok = stop_old(port)
            print(f"Replacing older Local AI ({running}) with {APP_VERSION}: {'done' if ok else 'FAILED'}")
        try:
            server = AppServer((host, port), Handler)
            break
        except OSError:
            continue  # used by some other program; try the next port
    if server is None:
        sys.exit("No free port found between 8790 and 8799.")
    args.port = server.server_address[1]
    if CFG.get("port") != args.port:  # remember it so phone links stay the same
        CFG["port"] = args.port
        CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
    server.phone = not args.no_phone
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 60)
    print("  Local AI is running.  Keep this window open (you can minimize it).")
    print(f"  On this PC:  {url}")
    if server.phone:
        lan, tail = local_ips()
        for ip in lan:
            print(f"  Phone on home Wi-Fi:   http://{ip}:{args.port}")
        for ip in tail:
            print(f"  Phone via Tailscale:   http://{ip}:{args.port}")
        print(f"  Phone PIN: {CFG['pin']}")
    print("=" * 60)
    make_desktop_shortcut()
    threading.Thread(target=warm_up, daemon=True).start()
    files.INDEX.refresh(CFG, force=True)
    if not args.no_window:
        threading.Timer(0.8, open_window, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


class _Tee:
    """Copy console output into app-log.txt so problems can be diagnosed later."""
    def __init__(self, stream, path):
        self.stream = stream
        try:
            self.f = open(path, "a", encoding="utf-8")
        except Exception:
            self.f = None

    def write(self, text):
        try:
            self.stream.write(text)
        except Exception:
            pass
        if self.f:
            self.f.write(text)
            self.f.flush()

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


if __name__ == "__main__":
    sys.stdout = _Tee(sys.stdout, HERE / "app-log.txt")
    sys.stderr = _Tee(sys.stderr, HERE / "app-log.txt")
    print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}  Local AI {APP_VERSION} starting (pid {os.getpid()})")
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
