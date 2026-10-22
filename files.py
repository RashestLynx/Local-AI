"""
files.py - lets the Local AI app (and your phone, behind the PIN) see files on this PC.

Rules
  - Only folders listed in ROOTS (configurable) are visible. Everything else is off-limits.
  - Private areas are always blocked, even inside a root: AppData, .ssh, password/key files, etc.
  - Writes: "upload" saves into  Downloads\\From phone\\  (never overwrites), and "delete" - which needs the
    PIN every time - moves a file/folder to the Windows Recycle Bin, so it can always be restored.
  - Nothing is ever modified in place or permanently erased.
"""

import os
import re
import threading
import time
import zipfile
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
DEFAULT_ROOTS = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
UPLOAD_DIR = HOME / "Downloads" / "From phone"

BLOCKED_PARTS = {"appdata", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "$recycle.bin",
                 "system volume information", ".git", "node_modules", "__pycache__"}
BLOCKED_NAME = re.compile(r"(password|passwd|credential|secret|\.kdbx$|\.pem$|\.key$|\.pfx$|\.p12$|id_rsa|id_ed25519|"
                          r"wallet\.dat|\.env$|cookies|login data)", re.I)
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".csv", ".xml", ".yml", ".yaml", ".ini",
            ".cfg", ".log", ".bat", ".ps1", ".sh", ".c", ".cpp", ".h", ".cs", ".java", ".kt", ".dart", ".go", ".rs",
            ".lua", ".sql", ".toml", ".srt", ".nfo", ".gradle", ".swift", ".php", ".rb"}
MAX_READ = 200_000


def roots(cfg):
    names = cfg.get("file_roots") or DEFAULT_ROOTS
    out = []
    for n in names:
        p = Path(n) if os.path.isabs(n) else HOME / n
        if p.exists() and p.is_dir():
            out.append(p.resolve())
    return out


def blocked(path):
    parts = {p.lower() for p in Path(path).parts}
    return bool(parts & BLOCKED_PARTS) or bool(BLOCKED_NAME.search(Path(path).name))


def resolve(cfg, path):
    """Turn a requested path into a safe absolute path inside an allowed root, or raise PermissionError."""
    if not path:
        raise PermissionError("No path given.")
    p = Path(path)
    p = (p if p.is_absolute() else HOME / p).resolve()
    for r in roots(cfg):
        if p == r or r in p.parents:
            if blocked(p.relative_to(r)) if p != r else False:
                raise PermissionError("That location is private and blocked.")
            return p
    raise PermissionError("That folder isn't shared with Local AI. (Settings > Files lets you add folders.)")


def info(p):
    try:
        st = p.stat()
        return {"name": p.name, "path": str(p), "dir": p.is_dir(), "size": None if p.is_dir() else st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))}
    except OSError:
        return None


def list_dir(cfg, path=None):
    if not path:
        return {"path": None, "parent": None, "entries": [dict(info(r), name=r.name) for r in roots(cfg)]}
    p = resolve(cfg, path)
    if not p.is_dir():
        raise PermissionError("Not a folder.")
    entries = []
    try:
        for c in p.iterdir():
            if c.name.startswith(".") or c.name.lower() in ("desktop.ini", "thumbs.db") or blocked(c.name):
                continue
            i = info(c)
            if i:
                entries.append(i)
    except PermissionError:
        raise PermissionError("Windows doesn't allow reading that folder.")
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = None if p in roots(cfg) else str(p.parent)
    return {"path": str(p), "parent": parent, "entries": entries[:2000], "truncated": len(entries) > 2000}


# ---------------------------------------------------------------- search (background index)
class Index:
    def __init__(self):
        self.files, self.built, self.lock, self.building = [], 0, threading.Lock(), False

    def refresh(self, cfg, force=False):
        if self.building or (not force and time.time() - self.built < 600):
            return
        self.building = True
        threading.Thread(target=self._build, args=(cfg,), daemon=True).start()

    def _build(self, cfg):
        found = []
        try:
            for r in roots(cfg):
                for dirpath, dirnames, filenames in os.walk(r):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in BLOCKED_PARTS]
                    for fn in filenames:
                        if not fn.startswith(".") and not BLOCKED_NAME.search(fn):
                            found.append(os.path.join(dirpath, fn))
                    if len(found) > 400_000:
                        break
            with self.lock:
                self.files, self.built = found, time.time()
        finally:
            self.building = False

    def search(self, cfg, query, limit=40):
        self.refresh(cfg)
        words = [w for w in re.findall(r"[\w.-]+", query.lower()) if len(w) > 1]
        if not words:
            return []
        if not self.files and self.building:  # first search right after start: wait briefly for the index
            for _ in range(40):
                time.sleep(0.25)
                if self.files:
                    break
        with self.lock:
            files = list(self.files)
        scored = []
        for f in files:
            name = os.path.basename(f).lower()
            full = f.lower()
            hits = sum(2 if w in name else 1 if w in full else 0 for w in words)
            if hits and all(w in full for w in words[:1]):
                scored.append((hits, f))
        scored.sort(key=lambda t: (-t[0], len(t[1])))
        out = []
        for _, f in scored[:limit]:
            i = info(Path(f))
            if i:
                out.append(i)
        return out


INDEX = Index()

STOP = set("find my the a an where is are was file files folder please show me open look for search can you do i have "
           "called named on in from of to and with that this".split())


def search_terms(question):
    """Pull the useful words out of 'where's my 2025 tax return pdf?'"""
    words = re.findall(r"[\w.-]+", question.lower())
    return " ".join(w for w in words if w not in STOP and len(w) > 1)


FILE_QUESTION = re.compile(r"\b(find|where('?s| is| are)|locate|search)\b.*\b(file|files|folder|document|doc|pdf|photo|picture|"
                           r"video|song|resume|spreadsheet|script|project|my)\b|\bin my (documents|downloads|desktop|files)\b", re.I)


# ---------------------------------------------------------------- reading
def read_text(cfg, path):
    p = resolve(cfg, path)
    if p.is_dir():
        raise PermissionError("That's a folder.")
    ext = p.suffix.lower()
    if ext == ".docx":
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        return {"name": p.name, "text": text[:MAX_READ], "truncated": len(text) > MAX_READ, "kind": "docx"}
    if ext == ".pdf":
        try:
            import pypdf  # optional: pip install pypdf
            r = pypdf.PdfReader(str(p))
            text = "\n".join((pg.extract_text() or "") for pg in r.pages[:50])
            return {"name": p.name, "text": text[:MAX_READ], "truncated": len(text) > MAX_READ, "kind": "pdf"}
        except ImportError:
            return {"name": p.name, "text": "", "kind": "pdf",
                    "note": "PDF reading needs one extra piece: run  pip install pypdf  once on the PC."}
    if ext in TEXT_EXT or p.stat().st_size < 64_000:
        raw = p.read_bytes()[:MAX_READ]
        if b"\x00" in raw[:4000]:
            return {"name": p.name, "text": "", "kind": "binary", "note": "This isn't a text file - download it instead."}
        return {"name": p.name, "text": raw.decode("utf-8", errors="replace"), "truncated": p.stat().st_size > MAX_READ,
                "kind": "text"}
    return {"name": p.name, "text": "", "kind": "binary", "note": "Preview isn't available for this file type - download it instead."}


def to_recycle_bin(cfg, path):
    """Move a file or folder to the Recycle Bin (recoverable). Caller must have checked the PIN."""
    p = resolve(cfg, path)
    if p in roots(cfg):
        raise PermissionError("You can't delete a whole shared folder from here.")
    if not p.exists():
        raise PermissionError("It's already gone.")
    if os.name == "nt":
        import subprocess
        q = str(p).replace("'", "''")
        kind = "DeleteDirectory" if p.is_dir() else "DeleteFile"
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Add-Type -AssemblyName Microsoft.VisualBasic; "
                            f"[Microsoft.VisualBasic.FileIO.FileSystem]::{kind}('{q}', 'OnlyErrorDialogs', 'SendToRecycleBin')"],
                           capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.exists():
            raise PermissionError("Windows wouldn't delete it - it may be open in another program. " + (r.stderr or "").strip()[:200])
    else:  # (non-Windows: freedesktop trash folder)
        trash = HOME / ".local/share/Trash/files"
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / p.name
        n = 1
        while dest.exists():
            dest = trash / f"{p.stem} ({n}){p.suffix}"
            n += 1
        os.replace(p, dest)
    INDEX.refresh(cfg, force=True)
    return str(p)


def save_upload(name, data):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(name or "upload"))[:120] or "upload"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / name
    stem, ext = os.path.splitext(name)
    n = 1
    while dest.exists():  # never overwrite
        dest = UPLOAD_DIR / f"{stem} ({n}){ext}"
        n += 1
    dest.write_bytes(data)
    return str(dest)
