# Local AI

A private AI assistant that runs **entirely on your own PC** with [Ollama](https://ollama.com), and works from your phone too.

- **Chat**: everyday questions, answered fast by small models that fit a 6 GB GPU.
- **Search**: searches the web, reads the pages and answers with numbered sources.
- **Code**: a writer model drafts the code, the code is run and tested, and a reviewer model checks it and sends it back for fixes.
- **Learns**: thumbs up/down and your own rules for each mode are fed back into future answers.
- **Knows your PC**: asks Windows (and the bundled [Diag-Tool](https://github.com/RashestLynx/Diag-Tool)) about your CPU, GPU, disks, battery, temperatures and errors when your question needs it.
- **Files**: browse, search, preview and ask about files in the folders you share. Deleting needs your PIN and only moves files to the Recycle Bin.
- **Cleanup (debloat)**: lists every installed app with when you last opened it, suggests what you never use (plus Windows extras like News, Tips, Widgets, Phone Link), and removes what you pick after you enter your PIN. It can also turn off Start-menu ads, Bing in Start search and background apps. Drivers, Windows parts, game launchers, Ollama, Python and the like are protected. Ask in chat ("what apps can I remove?") or open Security → Cleanup.
- **Security**: its own malware scanner (startup items, running programs, file heuristics, signatures, Microsoft Defender, abuse.ch threat list) with quarantine and restore.
- **Phone**: PIN-protected access from iPhone and Android on home Wi-Fi, or anywhere through [Tailscale](https://tailscale.com).

Everything except Search works offline.

## Install on Windows

1. Install [Ollama](https://ollama.com/download) and [Python 3](https://www.python.org/downloads/).
2. Download this repo (Code → Download ZIP) and unzip it, for example to `Documents\LocalAI`.
3. Double-click **`INSTALL - double-click me.bat`**. It downloads the models (qwen3:4b, qwen3:8b, llama3.1:8b) and makes a **Local AI** desktop shortcut.
4. Optional: **`Add more models.bat`** adds coding and reasoning models (qwen2.5-coder, deepseek-r1).
5. Open **Local AI** from the desktop. Keep its small black window open, or minimize it.

PDF previews need `pip install pypdf`.

## Phone apps

Download them from the [latest release](../../releases/tag/latest):

- **Android**: `local-ai-android.apk`. Open it and allow installing from your browser.
- **iPhone**: `local-ai-ios-unsigned.ipa`. It isn't signed by Apple, so install it with [Sideloadly](https://sideloadly.io) or AltStore using your Apple ID.

To connect:

1. On the PC, open Local AI → ⚙ Settings → **Use on your phone**. Note the home address and PIN.
2. Open the phone app, enter the home address (and the Tailscale address if you want access away from home), then enter your PIN.

You can also skip the app and open the address in Safari or Chrome, then use *Add to Home Screen*.

The apps are built by GitHub Actions from [`mobile/`](mobile) (Flutter) on every push.

## Other tools

- `coder_team.py`: command-line version of the coding team (`py coder_team.py "task"`).
- `tools/pc-cleanup/`: the debloat script behind the Cleanup tab. You can also run it by itself: list apps in `remove-list.json` (copy the example) and double-click `Clean up apps.bat`.

## Privacy

Your chats, learning data, PIN, quarantine and logs stay on your PC and are excluded from git (see `.gitignore`). The phone apps only talk to your own PC.
