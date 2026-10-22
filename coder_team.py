#!/usr/bin/env python3
"""
coder_team.py - a local "writer + reviewer" coding team that runs on Ollama, with multi-engine web search.

How it works:
  0. RESEARCH: the writer model picks a few search queries. Each query is sent to several
     search engines AT THE SAME TIME (DuckDuckGo, Bing, Mojeek, Stack Overflow, GitHub, ...).
     Results are merged: pages found by more engines rank higher. The best pages are opened
     and short excerpts go into the notes. (Skip with --offline.)
  Then each round:
  1. The WRITER model writes (or fixes) the code, using the research notes.
  2. The script saves it and runs it with a self-test (you approve each run unless --auto).
  3. If it crashes, the error (plus web results about that error) goes back to the writer.
  4. If it runs, the REVIEWER model (a different model family) checks it for bugs.
  5. Repeat until the reviewer approves or --rounds is reached.

Privacy: only short search queries and error messages go to the search engines.
Your code itself never leaves your PC, and the AI models run locally.

Search engines (pick with --engines, comma separated):
  duckduckgo     general web                       (default)
  bing           general web                       (default)
  mojeek         general web, independent index
  stackoverflow  programming Q&A, official API     (default)
  github         code repositories, official API   (default; set GITHUB_TOKEN env var for higher limits)
  brave          general web (rate-limited, may block quickly)
  wikipedia      concepts, algorithms, file formats
  npm            JavaScript packages               (added automatically for --lang javascript)
  searx          your own SearXNG server: covers Google and dozens more (use --searx URL)

Examples (PowerShell):
  python coder_team.py "Write a script that renames all .jpg files in a folder by date taken"
  python coder_team.py "Clean up this code, fix bugs, add comments" --file old_script.py
  python coder_team.py "Make a to-do list web server" --rounds 6 --auto
  python coder_team.py "Parse a CSV and make a chart" --engines duckduckgo,stackoverflow
  python coder_team.py "..." --searx http://localhost:8888
  python coder_team.py --test-search "python read exif date"     (check which engines work)

Only standard-library Python is used, so there's nothing extra to install.
"""

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DEFAULT_WRITER = "qwen3:8b"          # ~5 GB, strongest small coder that fits a 6 GB GPU
DEFAULT_REVIEWER = "gemma4:e4b"      # ~4 GB, different model family = catches different mistakes
DEFAULT_ENGINES = "duckduckgo,bing,stackoverflow,github"

LANGS = {
    # language: (file extension, command to run it, or None if it can't be run directly)
    "python": (".py", [sys.executable]),
    "javascript": (".js", ["node"]),
    "html": (".html", None),
    "powershell": (".ps1", ["powershell", "-ExecutionPolicy", "Bypass", "-File"]),
    "batch": (".bat", ["cmd", "/c"]),
    "other": (".txt", None),
}

WRITER_SYSTEM = """You are an expert {lang} programmer.
Rules:
- Reply with ONE complete, runnable {lang} program in a single fenced code block, then at most 3 short lines of notes.
- Never use placeholders like "..." or "your code here". Write every line.
- Handle errors sensibly and add brief comments where the logic is not obvious.
- If reference notes from a web search are given, use them for correct, current library usage,
  but the TASK always comes first.
{selftest}"""

SELFTEST_RULE = """- The program MUST support a --selftest command-line flag. When run with --selftest it runs
  assert-based tests of its own main logic (no user input, no network, no real file changes outside a
  temp folder), prints ALL TESTS PASSED, and exits. Without the flag it behaves normally."""

REVIEWER_SYSTEM = """You are a strict senior code reviewer. You check whether code correctly does the task.
Reply in exactly this format:
VERDICT: APPROVED
or
VERDICT: CHANGES NEEDED
1. <concrete bug or missing requirement, and how to fix it>
2. ...
Only list real bugs, crashes, security problems, or missing requirements. Style preferences do NOT count.
If the code works and meets the task, approve it."""

QUERY_SYSTEM = """You plan web searches for a programmer. Given a coding task, decide what
documentation would help (library APIs, exact function names, file formats, current versions).
Reply with ONLY a JSON array of 0 to 3 short search queries, for example:
["python pathlib rename file", "Pillow read EXIF date taken"]
Reply [] if the task needs no lookup. Never put code, file contents or personal data in a query."""


# ---------------------------------------------------------------- Ollama

GPU_FAILED = False  # set to True after a GPU (CUDA) error; later calls go straight to the CPU


def gpu_error(text):
    return any(k in text for k in ("CUDA", "cuda", "llama-server process has terminated", "out of memory"))


def chat(model, system, user, temperature=0.2, show=True, ctx=8192):
    """Send one request to Ollama and stream the reply. Falls back to the CPU if the GPU fails."""
    global GPU_FAILED
    try:
        return _chat(model, system, user, temperature, show, ctx, cpu=GPU_FAILED)
    except RuntimeError as e:
        if GPU_FAILED or not gpu_error(str(e)):
            sys.exit(f"\nOllama error: {e}")
        GPU_FAILED = True
        print("\n[The graphics card couldn't run the model (is a game using it?). "
              "Switching to the CPU - slower, but it works. Close games for full speed.]")
        return _chat(model, system, user, temperature, show, ctx, cpu=True)


def _chat(model, system, user, temperature, show, ctx, cpu=False):
    options = {"temperature": temperature, "num_ctx": ctx}
    if cpu:
        options["num_gpu"] = 0  # 0 layers on the GPU = run entirely on the CPU
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": True,
        "options": options,
        **({"think": False} if "qwen3" in model.lower() else {}),  # skip slow hidden "thinking"
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    out = []
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(chunk["error"])
                piece = chunk.get("message", {}).get("content", "")
                out.append(piece)
                if show:
                    print(piece, end="", flush=True)
                if chunk.get("done"):
                    break
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        if e.code == 404:
            sys.exit(f"\nModel '{model}' isn't downloaded. Run:  ollama pull {model}\n({detail})")
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"\nCan't reach Ollama at {OLLAMA_URL}. Is it running? ({e.reason})")
    if show:
        print()
    # Some models (e.g. Qwen3) "think out loud" first; drop that part so only the answer is used.
    return re.sub(r"(?s)<think>.*?</think>", "", "".join(out)).strip()


# ---------------------------------------------------------------- web helpers

def http_get(url, timeout=15, headers=None):
    """Download a URL and return (content_type, text). Handles gzip."""
    h = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
         "Accept-Encoding": "gzip"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        charset = r.headers.get_content_charset() or "utf-8"
        raw = r.read(3_000_000)
        if r.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        return ctype, raw.decode(charset, errors="replace")


def html_to_text(page):
    """Very small HTML-to-text converter: drops scripts, menus and tags."""
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)\b[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?is)<br\s*/?>|</p>|</li>|</h\d>|</pre>|</tr>", "\n", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    page = re.sub(r"[ \t\r\f\v]+", " ", page)
    return re.sub(r"\n\s*\n+", "\n", page).strip()


def one_line(fragment):
    return " ".join(html_to_text(fragment).split())


def anchors_with_class(page, cls):
    """Find <a> tags that have a given CSS class, whatever order their attributes are in."""
    out = []
    for attrs, text in re.findall(r"<a\b([^>]*)>(.*?)</a>", page, re.S):
        if re.search(r'class="[^"]*(?<![\w-])%s(?![\w-])[^"]*"' % re.escape(cls), attrs):
            m = re.search(r'href="([^"]+)"', attrs)
            if m:
                out.append((html.unescape(m.group(1)), text))
    return out


def outbound_links(page, own_domains, n):
    """Fallback parser for when a site changes its layout: any outside links with real titles."""
    out, seen = [], set()
    for href, text in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href = html.unescape(href)
        host = urllib.parse.urlparse(href).netloc.lower()
        title = one_line(text)
        if any(d in host for d in own_domains) or href in seen or len(title) < 15:
            continue
        seen.add(href)
        out.append({"title": title, "url": href, "snippet": ""})
        if len(out) >= n:
            break
    return out


def normalize_url(u):
    p = urllib.parse.urlparse(u)
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


# ---------------------------------------------------------------- search engines

def search_duckduckgo(query, n):
    _, page = http_get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}))
    snippets = [t for _, t in anchors_with_class(page, "result__snippet")]
    results = []
    for i, (href, title) in enumerate(anchors_with_class(page, "result__a")):
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if "uddg" in q:
            href = q["uddg"][0]
        results.append({"title": one_line(title), "url": href,
                        "snippet": one_line(snippets[i]) if i < len(snippets) else ""})
        if len(results) >= n:
            break
    return results


def _bing_real_url(href):
    """Bing sometimes wraps result links in a tracking redirect; unwrap it."""
    if "bing.com/ck/a" in href:
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            b = u[2:] + "=" * (-len(u[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(b).decode()
            except Exception:
                pass
    return href


def search_bing(query, n):
    _, page = http_get("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"}))
    results = []
    for chunk in page.split('class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        p = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        results.append({"title": one_line(m.group(2)), "url": _bing_real_url(html.unescape(m.group(1))),
                        "snippet": one_line(p.group(1)) if p else ""})
        if len(results) >= n:
            break
    return results or outbound_links(page, ["bing.com", "microsoft.com", "msn.com", "live.com"], n)


def search_mojeek(query, n):
    _, page = http_get("https://www.mojeek.com/search?" + urllib.parse.urlencode({"q": query}))
    snippets = re.findall(r'<p class="s"[^>]*>(.*?)</p>', page, re.S)
    results = []
    for i, (href, title) in enumerate(anchors_with_class(page, "title")):
        results.append({"title": one_line(title), "url": href,
                        "snippet": one_line(snippets[i]) if i < len(snippets) else ""})
        if len(results) >= n:
            break
    return results or outbound_links(page, ["mojeek"], n)


def search_brave(query, n):
    _, page = http_get("https://search.brave.com/search?" + urllib.parse.urlencode({"q": query}))
    return outbound_links(page, ["brave.com", "brave.app"], n)


def search_stackoverflow(query, n):
    url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode(
        {"order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow", "pagesize": n * 2})
    _, body = http_get(url)
    items = json.loads(body).get("items", [])
    items.sort(key=lambda i: (not i.get("is_answered"), -i.get("score", 0) // 10))  # answered first
    return [{"title": html.unescape(i.get("title", "")), "url": i.get("link", ""),
             "snippet": f"Stack Overflow, score {i.get('score', 0)}, "
                        f"{'answered' if i.get('is_answered') else 'no accepted answer'}, "
                        f"tags: {', '.join(i.get('tags', [])[:5])}"}
            for i in items[:n]]


def search_github(query, n):
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": query, "per_page": n})
    _, body = http_get(url, headers=headers)
    return [{"title": r.get("full_name", ""), "url": r.get("html_url", ""),
             "snippet": f"GitHub repo, {r.get('stargazers_count', 0)} stars. {r.get('description') or ''}"}
            for r in json.loads(body).get("items", [])[:n]]


def search_wikipedia(query, n):
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n})
    _, body = http_get(url)
    return [{"title": r["title"],
             "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_")),
             "snippet": one_line(r.get("snippet", ""))}
            for r in json.loads(body).get("query", {}).get("search", [])]


def search_npm(query, n):
    url = "https://registry.npmjs.org/-/v1/search?" + urllib.parse.urlencode({"text": query, "size": n})
    _, body = http_get(url)
    out = []
    for o in json.loads(body).get("objects", []):
        p = o.get("package", {})
        out.append({"title": f"npm: {p.get('name', '')}",
                    "url": p.get("links", {}).get("npm", ""),
                    "snippet": f"v{p.get('version', '?')}. {p.get('description') or ''}"})
    return out


def search_searx(base, query, n):
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    _, body = http_get(url)
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in json.loads(body).get("results", [])[:n]]


ENGINES = {
    "duckduckgo": search_duckduckgo,
    "bing": search_bing,
    "mojeek": search_mojeek,
    "brave": search_brave,
    "stackoverflow": search_stackoverflow,
    "github": search_github,
    "wikipedia": search_wikipedia,
    "npm": search_npm,
}


def run_engine(name, query, args, n):
    """Returns (name, results, error_or_None, seconds)."""
    start = time.time()
    try:
        if name == "searx":
            results = search_searx(args.searx, query, n)
        else:
            results = ENGINES[name](query, n)
        return name, [r for r in results if r.get("url")], None, time.time() - start
    except Exception as e:  # blocked, offline, captcha, layout change...
        return name, [], e, time.time() - start


def web_search(query, args, n=6):
    """Ask every engine at once, merge the results, rank by agreement. Never crashes."""
    names = args.engine_list
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        outcomes = list(pool.map(lambda nm: run_engine(nm, query, args, 5), names))

    scores, merged, status = {}, {}, []
    for name, results, err, _ in outcomes:
        status.append(f"{name} x" if err else f"{name} {len(results)}")
        for rank, r in enumerate(results):
            key = normalize_url(r["url"])
            scores[key] = scores.get(key, 0) + 1.0 / (rank + 1)  # found by more engines = higher
            if key not in merged:
                merged[key] = dict(r, engines=[name])
            else:
                merged[key]["engines"].append(name)
                if not merged[key]["snippet"] and r["snippet"]:
                    merged[key]["snippet"] = r["snippet"]
    ranked = sorted(merged, key=lambda k: -scores[k])[:n]
    print(f"  searched: {query!r} -> {len(merged)} unique results  ({', '.join(status)})")
    return [merged[k] for k in ranked]


def page_excerpt(url, limit):
    """Fetch a page and return the start of its readable text."""
    try:
        ctype, page = http_get(url)
    except Exception as e:
        return f"(could not open page: {e})"
    if "html" not in ctype and "text" not in ctype:
        return "(not a text page)"
    text = html_to_text(page)
    return text[:limit] + ("..." if len(text) > limit else "")


def notes_from_results(query, results, fetch_pages, page_chars):
    lines = [f"### Search: {query}"]
    for j, r in enumerate(results):
        lines.append(f"- {r['title']} ({r['url']}) [found by: {', '.join(r.get('engines', []))}]\n"
                     f"  {r['snippet']}")
        if j < fetch_pages:
            lines.append(f"  Page excerpt: {page_excerpt(r['url'], page_chars)}")
    return "\n".join(lines)


def plan_queries(task_text, lang, args):
    """Ask the writer model what to look up."""
    short_task = task_text[:1500]  # don't send big code files to the planner
    reply = chat(args.writer, QUERY_SYSTEM, f"Language: {lang}\nTASK:\n{short_task}",
                 temperature=0.1, show=False, ctx=args.ctx)
    m = re.search(r"\[.*?\]", reply, re.S)
    queries = []
    if m:
        try:
            queries = [q for q in json.loads(m.group(0)) if isinstance(q, str)]
        except json.JSONDecodeError:
            pass
    return [q.strip()[:120] for q in queries if q.strip()][:3]


def research(task_text, lang, args):
    """Returns (notes_text, list_of_source_urls)."""
    print("  planning searches...")
    queries = plan_queries(task_text, lang, args)
    if not queries:
        print("  the model decided no lookup is needed.")
        return "", []
    budget = args.notes_chars // len(queries)  # share the notes space fairly between queries
    blocks, sources = [], []
    for q in queries:
        results = web_search(q, args)
        if results:
            blocks.append(notes_from_results(q, results, fetch_pages=2, page_chars=1000)[:budget])
            sources += [r["url"] for r in results]
    return "\n\n".join(blocks), sources


def error_lookup(output, lang, args):
    """Search the web for the most telling error line from a failed run."""
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    errs = [l for l in lines if re.search(r"(Error|Exception|error:|not found|Traceback)", l)
            and not l.startswith("Traceback")]
    if not errs:
        return "", []
    err = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+/[^\s'\"]+", "", errs[-1])  # strip local file paths
    query = f"{lang} {err}"[:150]
    results = web_search(query, args, n=4)
    if not results:
        return "", []
    return notes_from_results(query, results, fetch_pages=1, page_chars=1200), [r["url"] for r in results]


def test_search(query, args):
    """--test-search: show which engines work from this PC."""
    banner(f"Testing search engines with: {query!r}")
    for name in args.engine_list:
        name, results, err, secs = run_engine(name, query, args, 3)
        if err:
            print(f"  {name:14} FAILED  ({secs:.1f}s)  {err}")
        elif not results:
            print(f"  {name:14} 0 results ({secs:.1f}s)  (may be blocked or layout changed)")
        else:
            print(f"  {name:14} OK {len(results)} results ({secs:.1f}s)  top: {results[0]['title'][:60]}")
    print("\nUse the working ones with  --engines name1,name2,...")


# ---------------------------------------------------------------- code helpers

def extract_code(text):
    """Pull the largest fenced code block out of a model reply."""
    blocks = re.findall(r"```[^\n`]*\n(.*?)```", text, flags=re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


def run_program(path, lang, use_selftest, timeout, auto):
    """Run the saved program. Returns (ran, ok, output)."""
    cmd = LANGS[lang][1]
    if cmd is None:
        return False, True, f"(Not run: {lang} files can't be executed directly.)"
    if shutil.which(cmd[0]) is None and cmd[0] != sys.executable:
        return False, True, f"(Not run: '{cmd[0]}' isn't installed or isn't on PATH.)"

    full = cmd + [str(path)] + (["--selftest"] if use_selftest else [])
    if not auto:
        ans = input(f"\nRun this AI-written code now?  {' '.join(full)}  [y/N] ").strip().lower()
        if ans != "y":
            return False, True, "(You chose not to run it.)"
    try:
        p = subprocess.run(full, cwd=path.parent, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return True, False, f"Program timed out after {timeout} seconds (possible infinite loop or waiting for input)."
    output = (p.stdout + "\n" + p.stderr).strip()
    output = output[-4000:]  # keep the tail, which has the actual error
    ok = p.returncode == 0
    if use_selftest and ok and "ALL TESTS PASSED" not in p.stdout:
        ok = False
        output += "\n(The program exited normally but never printed ALL TESTS PASSED.)"
    return True, ok, output or "(no output)"


def banner(title):
    print("\n" + "=" * 70 + f"\n  {title}\n" + "=" * 70)


# ---------------------------------------------------------------- main loop

def main():
    ap = argparse.ArgumentParser(description="Local writer + reviewer coding team (Ollama) with multi-engine web search.")
    ap.add_argument("task", nargs="?", help="What you want built or done, in plain English.")
    ap.add_argument("--file", help="Existing code file to clean up / fix / extend.")
    ap.add_argument("--lang", choices=LANGS.keys(), help="Language (default: guessed from --file, else python).")
    ap.add_argument("--writer", default=DEFAULT_WRITER, help=f"Writer model (default {DEFAULT_WRITER}).")
    ap.add_argument("--reviewer", default=DEFAULT_REVIEWER, help=f"Reviewer model (default {DEFAULT_REVIEWER}).")
    ap.add_argument("--rounds", type=int, default=4, help="Max write/fix rounds (default 4).")
    ap.add_argument("--timeout", type=int, default=30, help="Seconds a test run may take (default 30).")
    ap.add_argument("--no-selftest", action="store_true", help="Don't require a --selftest mode.")
    ap.add_argument("--auto", action="store_true", help="Run generated code without asking each time.")
    ap.add_argument("--no-run", action="store_true", help="Never run the generated code (reviewer only).")
    ap.add_argument("--out", default="output", help="Folder for results (default ./output).")
    ap.add_argument("--offline", action="store_true", help="Turn off all web searching.")
    ap.add_argument("--engines", help=f"Comma-separated search engines (default {DEFAULT_ENGINES}). "
                                      f"Options: {', '.join(ENGINES)}, searx")
    ap.add_argument("--searx", help="URL of your own SearXNG server; adds it as an extra engine.")
    ap.add_argument("--test-search", metavar="QUERY", help="Just test which search engines work, then exit.")
    ap.add_argument("--ctx", type=int, default=8192, help="Model context size in tokens (default 8192).")
    ap.add_argument("--notes-chars", type=int, default=7000, help="Max characters of web notes (default 7000).")
    args = ap.parse_args()
    if not args.task and not args.test_search:
        ap.error("give a task in quotes, or use --test-search \"some query\"")

    # Work out the language.
    lang = args.lang
    existing = ""
    if args.file:
        src = Path(args.file)
        if not src.exists():
            sys.exit(f"File not found: {src}")
        existing = src.read_text(encoding="utf-8", errors="replace")
        if not lang:
            ext_map = {v[0]: k for k, v in LANGS.items()}
            lang = ext_map.get(src.suffix.lower(), "other")
    lang = lang or "python"
    ext, _ = LANGS[lang]
    use_selftest = (lang == "python") and not args.no_selftest and not args.no_run

    # Work out which search engines to use.
    names = [e.strip().lower() for e in (args.engines or DEFAULT_ENGINES).split(",") if e.strip()]
    if not args.engines and lang == "javascript":
        names.append("npm")
    if args.searx and "searx" not in names:
        names.append("searx")
    bad = [e for e in names if e not in ENGINES and e != "searx"]
    if bad:
        ap.error(f"unknown engine(s): {', '.join(bad)}. Options: {', '.join(ENGINES)}, searx")
    if "searx" in names and not args.searx:
        ap.error("the searx engine needs --searx http://your-server:port")
    args.engine_list = list(dict.fromkeys(names))

    if args.test_search:
        test_search(args.test_search, args)
        return

    # Output folder for this job.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir = (Path(args.out) / stamp).resolve()  # absolute, so running the code works from any folder
    job_dir.mkdir(parents=True, exist_ok=True)
    final_path = job_dir / f"solution{ext}"
    log = [f"# Task\n{args.task}\n",
           f"Writer: {args.writer} | Reviewer: {args.reviewer} | Language: {lang} | "
           f"Web search: {'off' if args.offline else ', '.join(args.engine_list)}\n"]
    all_sources = []

    writer_system = WRITER_SYSTEM.format(lang=lang, selftest=SELFTEST_RULE if use_selftest else "")
    if os.environ.get("LAI_LESSONS"):  # feedback memory passed in by the Local AI app
        writer_system += "\n" + os.environ["LAI_LESSONS"][:4000]
    task_text = args.task
    if existing:
        task_text += f"\n\nHere is the existing code to work on ({Path(args.file).name}):\n```{lang}\n{existing}\n```"

    # 0. Research.
    notes = ""
    if not args.offline:
        banner(f"Researching on the web ({', '.join(args.engine_list)})")
        notes, sources = research(task_text, lang, args)
        all_sources += sources
        if notes:
            log.append(f"\n## Research notes\n{notes}\n")
    notes_block = (f"\n\nREFERENCE NOTES FROM A WEB SEARCH (may be incomplete or outdated; "
                   f"the task comes first):\n{notes}") if notes else ""

    code, feedback, approved = "", "", False

    for rnd in range(1, args.rounds + 1):
        # 1. Writer writes or fixes.
        banner(f"Round {rnd}/{args.rounds}: {args.writer} is {'writing' if rnd == 1 else 'fixing'} the code")
        if rnd == 1:
            prompt = f"TASK:\n{task_text}{notes_block}"
        else:
            prompt = (f"TASK:\n{task_text}{notes_block}\n\nYOUR CURRENT CODE:\n```{lang}\n{code}```\n\n"
                      f"PROBLEMS TO FIX:\n{feedback}\n\nReturn the complete corrected program.")
        reply = chat(args.writer, writer_system, prompt, ctx=args.ctx)
        code = extract_code(reply)
        round_path = job_dir / f"round{rnd}{ext}"
        round_path.write_text(code, encoding="utf-8")
        final_path.write_text(code, encoding="utf-8")
        log.append(f"\n## Round {rnd}\nSaved {round_path.name}\n")

        # 2. Run it.
        if args.no_run:
            ran, ok, output = False, True, "(Test-running is turned off.)"
        else:
            ran, ok, output = run_program(final_path, lang, use_selftest, args.timeout, args.auto)
        if ran:
            banner("Test run " + ("PASSED" if ok else "FAILED"))
            print(output)
            log.append(f"Run {'passed' if ok else 'failed'}:\n```\n{output}\n```\n")
        else:
            print("\n" + output)
        if ran and not ok:
            feedback = f"Running the program failed with this output:\n{output}"
            if not args.offline:
                print("\nLooking up the error online...")
                err_notes, err_sources = error_lookup(output, lang, args)
                if err_notes:
                    feedback += f"\n\nWeb results about this error:\n{err_notes}"
                    all_sources += err_sources
                    log.append(f"Error lookup:\n{err_notes}\n")
            continue  # straight back to the writer, no need to ask the reviewer yet

        # 3. Reviewer checks it.
        banner(f"{args.reviewer} is reviewing")
        review_prompt = (f"TASK:\n{task_text}\n\nCODE ({lang}):\n```{lang}\n{code}```\n\n"
                         f"TEST RUN OUTPUT:\n{output if ran else '(not run)'}")
        review = chat(args.reviewer, REVIEWER_SYSTEM, review_prompt, temperature=0.1, ctx=args.ctx)
        log.append(f"Review:\n{review}\n")
        if re.search(r"VERDICT:\s*APPROVED", review, re.IGNORECASE):
            approved = True
            break
        feedback = f"A code reviewer found these problems:\n{review}"

    # Wrap up.
    if all_sources:
        log.append("\n## Web sources used\n" + "\n".join(f"- {u}" for u in dict.fromkeys(all_sources)))
    (job_dir / "log.md").write_text("\n".join(log), encoding="utf-8")
    banner("DONE: " + ("approved by reviewer" if approved else f"stopped after {args.rounds} rounds, check the log"))
    print(f"Final code:  {final_path.resolve()}")
    print(f"Full log:    {(job_dir / 'log.md').resolve()}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
