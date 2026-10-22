#!/usr/bin/env python3
"""
assistant.py - one chat window with two "modes", like Claude's chat vs. code options.

  CHAT mode -> a general assistant model (Ajax when you have it installed, otherwise a fallback).
               It remembers the conversation and can search the web when you ask.
  CODE mode -> hands the request to coder_team.py (writer + reviewer + tests + web search).

By default it picks the mode for you from what you type. You can always force it:
  /code <task>                 send this to the coding team
  /code <task> @path\\file.py   clean up / fix / change an existing file
  /chat <message>              send this to the chat model
  /web on | /web off           let the chat model search the web before answering (default on)
  /models                      show which models are being used
  /clear                       forget the conversation
  /quit                        exit

Put this file in the SAME folder as coder_team.py, then run:
  python assistant.py
  python assistant.py --chat-model qwen3:8b       (choose the chat model yourself)

Ajax: when PewDiePie's model is released and you add it to Ollama under any name containing
"ajax", this script uses it for chat automatically. Only download it from his official source.
"""

import argparse
import json
import re
import subprocess
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import coder_team as ct
except ImportError:
    sys.exit("coder_team.py must be in the same folder as assistant.py")

# Chat model preference order: Ajax first, then good general models that fit a 6 GB GPU.
CHAT_PREFERENCES = ["ajax", "qwen3.5:9b", "qwen3:8b", "gemma4:e4b", "llama3.1:8b"]

CHAT_SYSTEM = """You are a helpful, friendly personal assistant running locally on the user's PC.
Answer clearly and concisely. If web results are provided, use them and mention the source sites.
If the user asks you to write or fix a program, tell them to type /code followed by the request,
which sends it to the dedicated coding team."""

# Words that suggest a message is a coding job.
CODE_HINTS = re.compile(
    r"```|\b(code|coding|script|program|function|class|method|bug|debug|error|exception|traceback|"
    r"refactor|compile|python|javascript|typescript|html|css|sql|powershell|batch file|api|regex|"
    r"variable|loop|syntax|module|library|import|\.py\b|\.js\b)\b", re.I)
CODE_VERBS = re.compile(r"\b(write|make|build|create|fix|clean|optimi[sz]e|convert|automate|generate)\b", re.I)


def installed_models():
    try:
        with urllib.request.urlopen(f"{ct.OLLAMA_URL}/api/tags", timeout=10) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except urllib.error.URLError:
        sys.exit(f"Can't reach Ollama at {ct.OLLAMA_URL}. Start Ollama first.")


def pick_chat_model(wanted, models):
    if wanted:
        return wanted
    for pref in CHAT_PREFERENCES:
        for m in models:
            if pref in m.lower():
                return m
    if models:
        return models[0]
    sys.exit("No models installed. Run:  ollama pull qwen3:8b")


def looks_like_code(text):
    """Guess whether a message is a coding job: needs a coding word AND a 'do something' verb, or a code block."""
    if "```" in text:
        return True
    return bool(CODE_HINTS.search(text)) and bool(CODE_VERBS.search(text))


USE_CPU = False


def chat_stream(model, messages, ctx=8192):
    """Stream a chat reply; if the graphics card fails, retry on the CPU."""
    global USE_CPU
    reply, err = _chat_stream(model, messages, ctx, USE_CPU)
    if err and not USE_CPU and ct.gpu_error(err):
        USE_CPU = True
        print("[The graphics card couldn't run the model (is a game using it?). "
              "Switching to the CPU - slower, but it works.]")
        reply, err = _chat_stream(model, messages, ctx, True)
    if err:
        print(f"\n[Ollama error: {err}]")
    return reply


def _chat_stream(model, messages, ctx, cpu):
    """Returns (visible_text, error_or_None), hiding any <think> section."""
    options = {"temperature": 0.6, "num_ctx": ctx}
    if cpu:
        options["num_gpu"] = 0
    body = json.dumps({"model": model, "messages": messages, "stream": True, "options": options,
                       **({"think": False} if "qwen3" in model.lower() else {})}).encode()
    req = urllib.request.Request(f"{ct.OLLAMA_URL}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    shown, buf, in_think = [], "", False
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    return "".join(shown), chunk["error"]
                buf += chunk.get("message", {}).get("content", "")
                # Print everything outside <think>...</think>.
                while buf:
                    if in_think:
                        end = buf.find("</think>")
                        if end == -1:
                            buf = buf[-10:]  # keep a little in case the tag is split
                            break
                        buf, in_think = buf[end + 8:], False
                    else:
                        start = buf.find("<think>")
                        if start == -1:
                            # Hold back a trailing "<..." in case it's the start of a split <think> tag.
                            lt = buf.rfind("<")
                            safe = lt if lt != -1 and len(buf) - lt < 7 else len(buf)
                            print(buf[:safe], end="", flush=True)
                            shown.append(buf[:safe])
                            buf = buf[safe:]
                            break
                        print(buf[:start], end="", flush=True)
                        shown.append(buf[:start])
                        buf, in_think = buf[start + 7:], True
                if chunk.get("done"):
                    break
        if buf and not in_think:
            print(buf, end="")
            shown.append(buf)
    except urllib.error.HTTPError as e:
        return "".join(shown), f"HTTP {e.code}: {e.read().decode(errors='ignore')}"
    except urllib.error.URLError as e:
        return "".join(shown), f"Can't reach Ollama: {e.reason}"
    print()
    return "".join(shown).strip(), None


def needs_web(text):
    """Questions about current things benefit from a web search."""
    return bool(re.search(r"\b(latest|current|today|news|price|202\d|release|who is|what is|how do|"
                          r"how to|where|when|search|look up|weather|best)\b", text, re.I))


def web_context(text, search_args):
    results = ct.web_search(text[:150], search_args, n=5)
    if not results:
        return ""
    return ct.notes_from_results(text[:150], results, fetch_pages=2, page_chars=1200)[:5000]


def run_coding_team(task, extra_args):
    """Hand a job to coder_team.py, letting it talk to you directly (it asks before running code)."""
    file_arg = []
    m = re.search(r"@(\"[^\"]+\"|\S+)", task)
    if m:
        path = m.group(1).strip('"')
        task = (task[:m.start()] + task[m.end():]).strip()
        file_arg = ["--file", path]
    cmd = [sys.executable, str(HERE / "coder_team.py"), task or "Clean up and improve this code"] + file_arg + extra_args
    print("\n>>> Sending this to the coding team...\n")
    # Run attached to your console so you see progress live and can answer its prompts.
    result = subprocess.run(cmd, cwd=Path.cwd())
    return result.returncode


def main():
    ap = argparse.ArgumentParser(description="Chat + coding assistant router for local Ollama models.")
    ap.add_argument("--chat-model", help="Model for normal chat (default: Ajax if installed, else best available).")
    ap.add_argument("--code-args", default="", help='Extra options for coder_team.py, e.g. "--rounds 6 --auto".')
    ap.add_argument("--engines", default=ct.DEFAULT_ENGINES, help="Search engines for chat web lookups.")
    ap.add_argument("--searx", help="Your SearXNG server URL, added as an extra engine.")
    args = ap.parse_args()

    models = installed_models()
    chat_model = pick_chat_model(args.chat_model, models)
    code_args = args.code_args.split()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()] + (["searx"] if args.searx else [])
    search_args = types.SimpleNamespace(engine_list=engines, searx=args.searx)
    web_on = True
    history = []

    print("=" * 70)
    print(f"  Local assistant.  Chat model: {chat_model}   |   Code: coder_team.py "
          f"({ct.DEFAULT_WRITER} + {ct.DEFAULT_REVIEWER})")
    if not any("ajax" in m.lower() for m in models):
        print("  (Ajax isn't installed; using the fallback chat model. It'll switch automatically once added.)")
    print("  Type normally, or use /code, /chat, /web on|off, /models, /clear, /quit")
    print("=" * 70)

    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not text:
            continue
        low = text.lower()

        if low in ("/quit", "/exit", "quit", "exit"):
            print("Bye!")
            break
        if low == "/clear":
            history = []
            print("(Conversation cleared.)")
            continue
        if low == "/models":
            print(f"Chat: {chat_model}\nCode writer: {ct.DEFAULT_WRITER}\nCode reviewer: {ct.DEFAULT_REVIEWER}\n"
                  f"Installed: {', '.join(models)}")
            continue
        if low in ("/web on", "/web off"):
            web_on = low.endswith("on")
            print(f"(Web search for chat is {'on' if web_on else 'off'}.)")
            continue

        # Decide the mode.
        if low.startswith("/code"):
            mode, text = "code", text[5:].strip()
        elif low.startswith("/chat"):
            mode, text = "chat", text[5:].strip()
        else:
            mode = "code" if looks_like_code(text) else "chat"

        if mode == "code":
            if not text:
                print("Tell me what to build, e.g.  /code a script that backs up my Documents folder")
                continue
            print("[CODE mode]  (type /chat in front of a message to talk to the chat model instead)")
            run_coding_team(text, code_args)
            history.append({"role": "user", "content": f"(Sent to the coding team) {text}"})
            history.append({"role": "assistant", "content": "The coding team worked on that; results are in the output folder."})
            continue

        # Chat mode.
        print(f"[CHAT mode: {chat_model}]")
        system = CHAT_SYSTEM
        if web_on and needs_web(text):
            print("  searching the web...")
            notes = web_context(text, search_args)
            if notes:
                system += f"\n\nWEB RESULTS FOR THE USER'S LATEST MESSAGE:\n{notes}"
        history.append({"role": "user", "content": text})
        history = history[-20:]  # keep the last 10 exchanges so it fits in memory
        print(f"\n{chat_model.split(':')[0].title()}: ", end="")
        reply = chat_stream(chat_model, [{"role": "system", "content": system}] + history)
        history.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
