#!/usr/bin/env python3
"""diag_tool.py - figure out what's wrong with an electronic device and how to fix it.

Three modes:

  scan      Inspect the computer this script is running on: what kind of
            device it is (laptop / desktop / VM / ...), plus live checks on
            disk, memory, CPU load, battery and temperature. Every problem it
            finds comes with a suggested fix.

  diagnose  Describe a problem in plain words ("my phone won't charge",
            "laptop is overheating and slow"). The tool works out which kind
            of device you mean and which symptoms you have, then lists the
            likely causes (most likely first) and the steps to fix each one.

  identify  Work out what device an IMEI or serial number belongs to
            (IMEI checksum + TAC breakdown; Apple serial sanity check).

Standard library only. If `psutil` is installed, `scan` uses it for better
results on Windows and macOS.

Examples:
    python diag_tool.py scan
    python diag_tool.py diagnose "my phone won't charge and gets hot"
    python diag_tool.py diagnose --device laptop --symptom slow --symptom overheating
    python diag_tool.py diagnose            # interactive questions
    python diag_tool.py identify --imei 490154203237518
    python diag_tool.py list                # show known devices and symptoms
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from phone_info import analyze_apple_serial, analyze_imei

# ---------------------------------------------------------------------------
# Device kinds. Keywords are matched against free-text problem descriptions.
# ---------------------------------------------------------------------------
DEVICES = {
    "phone": ["phone", "iphone", "android", "samsung", "galaxy", "pixel", "cell", "mobile", "smartphone"],
    "tablet": ["tablet", "ipad", "kindle", "fire hd", "galaxy tab", "surface go"],
    "laptop": ["laptop", "notebook", "macbook", "chromebook", "thinkpad", "ultrabook"],
    "desktop": ["desktop", "pc", "computer", "tower", "imac", "gaming rig"],
    "console": ["console", "playstation", "ps4", "ps5", "xbox", "switch", "nintendo", "steam deck"],
    "tv": ["tv", "television", "monitor", "screen tv", "smart tv", "roku", "fire stick", "chromecast"],
    "router": ["router", "modem", "wifi box", "mesh", "access point", "internet box"],
    "headphones": ["headphones", "earbuds", "airpods", "headset", "earphones", "speaker", "bluetooth speaker"],
    "printer": ["printer", "scanner", "inkjet", "laser printer"],
    "smartwatch": ["watch", "smartwatch", "apple watch", "fitbit", "galaxy watch"],
}

# Which device kinds have a battery / a screen. Used to filter symptoms.
HAS_BATTERY = {"phone", "tablet", "laptop", "console", "headphones", "smartwatch"}
HAS_SCREEN = {"phone", "tablet", "laptop", "desktop", "tv", "smartwatch", "console"}


@dataclass
class Cause:
    """One possible reason for a symptom, and how to fix it."""
    name: str
    likelihood: int  # 1 (rare) .. 5 (very common)
    fixes: list[str]
    devices: set[str] | None = None  # None = applies to every device kind
    warning: str | None = None  # safety note shown in bold-ish caps


@dataclass
class Symptom:
    key: str
    title: str
    keywords: list[str]
    causes: list[Cause]
    devices: set[str] | None = None  # None = every device kind


# ---------------------------------------------------------------------------
# Symptom knowledge base.
# ---------------------------------------------------------------------------
SYMPTOMS: list[Symptom] = [
    Symptom(
        "no_power", "Won't turn on / completely dead",
        ["won't turn on", "wont turn on", "doesn't turn on", "not turning on", "dead",
         "no power", "won't boot", "wont boot", "won't start", "black and dead", "nothing happens"],
        [
            Cause("Battery fully drained", 5, [
                "Plug it into a known-good charger and wall outlet (not a PC USB port).",
                "Leave it charging 30 minutes before trying to power on - a deeply "
                "drained battery shows nothing at first.",
            ], devices=HAS_BATTERY),
            Cause("Device is on but frozen with screen off", 4, [
                "Force restart. iPhone: Vol Up, Vol Down, then hold Side until the logo. "
                "Android: hold Power + Vol Down ~15 s. Laptop/console: hold power 15 s.",
            ]),
            Cause("Bad power cable, adapter or outlet", 4, [
                "Try a different cable, adapter and wall socket.",
                "Check the charger/adapter light (if it has one).",
                "Desktop: confirm the switch on the back of the power supply is ON.",
            ]),
            Cause("Dirty or damaged charging port", 3, [
                "Shine a light into the port; remove lint gently with a wooden "
                "toothpick or plastic pick. Never metal.",
            ], devices={"phone", "tablet", "laptop", "headphones", "smartwatch", "console"}),
            Cause("Failed power supply (PSU)", 3, [
                "Listen for fans / look for lights when you press power.",
                "Test with a PSU tester or swap in a known-good PSU.",
            ], devices={"desktop", "console", "tv", "router", "printer"}),
            Cause("Dead battery or failed board component", 2, [
                "If nothing above works, get a repair quote - battery replacement "
                "is cheap; board repair may not be worth it on older devices.",
            ]),
        ],
    ),
    Symptom(
        "not_charging", "Won't charge / charges slowly",
        ["won't charge", "wont charge", "not charging", "doesn't charge", "charges slow",
         "slow charging", "charging slowly", "stuck at", "charger not working"],
        [
            Cause("Lint in the charging port", 5, [
                "Power off, then gently clear the port with a wooden toothpick.",
                "Compressed air in short bursts, held upright.",
            ], devices={"phone", "tablet", "headphones", "smartwatch", "laptop"}),
            Cause("Faulty cable or adapter", 5, [
                "Swap cable and adapter one at a time to find the bad part.",
                "Use the manufacturer's charger or a certified one (USB-IF / MFi).",
            ]),
            Cause("Optimized / adaptive charging holding at 80%", 3, [
                "iPhone: Settings > Battery > Charging. Android: Settings > Battery "
                "> Adaptive/Protect battery. macOS: Battery > Optimized charging.",
                "This is normal behaviour, not a fault.",
            ], devices={"phone", "tablet", "laptop", "smartwatch"}),
            Cause("Too hot or too cold", 3, [
                "Let it reach room temperature; charging pauses outside ~0-35 C.",
                "Remove thick cases while charging.",
            ], devices=HAS_BATTERY),
            Cause("Software charging glitch", 2, [
                "Restart the device while plugged in.",
                "Install pending system updates.",
            ]),
            Cause("Worn-out battery", 3, [
                "Check battery health (iPhone: Settings > Battery > Battery Health; "
                "Windows: `powercfg /batteryreport`; macOS: System Settings > Battery).",
                "Below ~80% capacity: replace the battery.",
            ], devices=HAS_BATTERY),
        ],
        devices=HAS_BATTERY,
    ),
    Symptom(
        "battery_drain", "Battery drains fast",
        ["battery drain", "drains fast", "battery dies", "battery dying", "battery life",
         "dies quickly", "battery low", "battery goes", "doesn't hold charge", "won't hold charge"],
        [
            Cause("App or process running in the background", 5, [
                "Open the battery usage screen and find the top consumer.",
                "Phone: Settings > Battery. Windows: Settings > System > Power & battery. "
                "macOS: Activity Monitor > Energy.",
                "Force-quit or uninstall the offender; update it if it's an app bug.",
            ]),
            Cause("Screen brightness / always-on display", 4, [
                "Turn on auto-brightness, shorten screen timeout, disable always-on display.",
            ], devices=HAS_SCREEN & HAS_BATTERY),
            Cause("Weak signal (phone searching for network)", 3, [
                "In poor-signal areas switch to Wi-Fi calling or Airplane mode + Wi-Fi.",
            ], devices={"phone", "tablet", "smartwatch"}),
            Cause("Battery is old / degraded", 4, [
                "Check battery health / cycle count (see 'Won't charge').",
                "Lithium batteries last ~500-1000 cycles; replacement is the fix.",
            ]),
            Cause("Recent update re-indexing", 2, [
                "After a major update, drain is normal for 1-2 days while it re-indexes.",
            ]),
        ],
        devices=HAS_BATTERY,
    ),
    Symptom(
        "swollen_battery", "Swollen battery / bulging case",
        ["swollen", "bulging", "puffy", "battery swelling", "case lifting", "screen lifting",
         "back popping", "trackpad won't click", "pillow"],
        [
            Cause("Lithium battery swelling (gas build-up)", 5, [
                "STOP using and charging it now. Power it off.",
                "Do not press on it, puncture it, or try to flatten it.",
                "Keep it on a non-flammable surface away from anything that can burn.",
                "Take it to a repair shop for battery replacement and dispose of the old "
                "battery at a battery recycling point - never in household trash.",
            ], warning="FIRE RISK - a swollen battery can catch fire. Stop using it."),
        ],
        devices=HAS_BATTERY,
    ),
    Symptom(
        "overheating", "Overheating / gets hot",
        ["overheat", "overheating", "hot", "too warm", "burning", "heats up", "fan loud",
         "fan noise", "loud fan", "thermal", "temperature"],
        [
            Cause("Blocked vents / dust build-up", 5, [
                "Use it on a hard flat surface, never a bed or lap blanket.",
                "Blow out vents with compressed air (hold fans still while doing it).",
                "Desktop/console: clean dust filters and heatsinks every 6-12 months.",
            ], devices={"laptop", "desktop", "console", "router", "tv"}),
            Cause("Heavy load (games, video, many apps)", 4, [
                "Close apps you're not using; check Task Manager / Activity Monitor "
                "for anything using lots of CPU.",
            ]),
            Cause("Charging while doing heavy work, or in a thick case", 3, [
                "Remove the case and avoid gaming while charging.",
            ], devices={"phone", "tablet", "laptop"}),
            Cause("Hot environment / direct sunlight", 3, [
                "Move it out of the sun and away from heaters; let it cool before use.",
            ]),
            Cause("Dried-out thermal paste", 2, [
                "On machines 3+ years old, re-pasting the CPU/GPU often drops temps "
                "10-20 C. Doable at home on desktops; laptops/consoles - repair shop.",
            ], devices={"laptop", "desktop", "console"}),
            Cause("Failing / swelling battery", 2, [
                "If the case is bulging see 'Swollen battery' - stop using it.",
            ], devices=HAS_BATTERY),
        ],
    ),
    Symptom(
        "slow", "Slow / laggy",
        ["slow", "laggy", "lag", "sluggish", "takes forever", "freezes", "freezing",
         "hangs", "unresponsive", "stutter"],
        [
            Cause("Storage almost full", 5, [
                "Keep at least 10-15% of storage free.",
                "Delete big unused apps, old downloads, duplicate photos/videos; "
                "move media to cloud or external storage.",
                "Run `python diag_tool.py scan` on a computer to check disk space.",
            ]),
            Cause("Too many apps / tabs / startup programs", 5, [
                "Restart the device - it clears memory and stuck processes.",
                "Windows: Task Manager > Startup apps, disable what you don't need. "
                "macOS: System Settings > General > Login Items.",
                "Close browser tabs; remove unneeded browser extensions.",
            ]),
            Cause("Overheating causing throttling", 3, [
                "See 'Overheating' - hot chips slow themselves down on purpose.",
            ]),
            Cause("Malware or adware", 3, [
                "Run a full scan (Windows Security / Malwarebytes).",
                "Uninstall apps you don't recognise; reset browser settings.",
            ], devices={"laptop", "desktop", "phone", "tablet"}),
            Cause("Outdated software", 3, [
                "Install system and app updates.",
            ]),
            Cause("Old hard drive (HDD) or failing drive", 2, [
                "Upgrading an HDD to an SSD is the single biggest speed-up for old PCs.",
                "Check drive health: Windows `wmic diskdrive get status`, or CrystalDiskInfo; "
                "Linux `smartctl -a /dev/sda`.",
            ], devices={"laptop", "desktop", "console"}),
            Cause("Not enough RAM for what you run", 2, [
                "If memory is constantly >90% used, add RAM (desktops, some laptops).",
            ], devices={"laptop", "desktop"}),
        ],
    ),
    Symptom(
        "crashing", "Crashes / restarts / blue screen",
        ["crash", "crashes", "crashing", "restarts", "reboots", "blue screen", "bsod",
         "keeps restarting", "boot loop", "bootloop", "kernel panic", "apps close"],
        [
            Cause("A buggy app or driver", 5, [
                "Note what you were doing when it crashed - one app? Update or reinstall it.",
                "Windows: update GPU/chipset drivers; check Event Viewer > Windows Logs > System.",
                "macOS: check Console app > Crash Reports.",
            ]),
            Cause("Overheating", 3, [
                "See 'Overheating'. Crashes under load + heat point here.",
            ]),
            Cause("Corrupt system update", 3, [
                "Phone stuck in boot loop: boot to recovery and clear cache partition "
                "(Android) or update via computer (iPhone - Finder/Apple Devices app, "
                "choose 'Update' not 'Restore' to keep data).",
                "Windows: Settings > System > Recovery > 'Fix problems using Windows Update'.",
            ]),
            Cause("Faulty RAM", 2, [
                "Run Windows Memory Diagnostic or MemTest86.",
                "Reseat RAM sticks; test one stick at a time.",
            ], devices={"desktop", "laptop"}),
            Cause("Weak / failing power supply", 2, [
                "Random restarts under load on a desktop often mean the PSU.",
            ], devices={"desktop", "console"}),
        ],
    ),
    Symptom(
        "screen", "Screen problems (black, flickering, lines, dead touch)",
        ["screen", "display", "flicker", "flickering", "black screen", "lines on",
         "touch not working", "touchscreen", "ghost touch", "cracked", "dim", "no picture",
         "no signal", "dead pixels", "green line"],
        [
            Cause("Display asleep / brightness at zero", 4, [
                "Press a key / tap / raise brightness. TV: check the input/source button.",
            ]),
            Cause("Loose or bad video cable / wrong input", 4, [
                "Reseat HDMI/DisplayPort at both ends; try another cable and port.",
                "TV/monitor: cycle through inputs with the Source button.",
            ], devices={"desktop", "tv", "console"}),
            Cause("Graphics driver problem", 3, [
                "Windows: press Win+Ctrl+Shift+B to reset the graphics driver.",
                "Update or reinstall the GPU driver.",
            ], devices={"laptop", "desktop"}),
            Cause("Screen protector / dirt causing bad touch", 3, [
                "Clean the screen; remove a cracked or bubbled screen protector.",
            ], devices={"phone", "tablet", "smartwatch"}),
            Cause("Physical damage (crack, pressure, liquid)", 3, [
                "Lines, colour blotches or black spots after a drop = panel damage. "
                "Screen replacement needed - get a quote before deciding.",
            ]),
            Cause("Loose display cable inside", 2, [
                "Flicker that changes when you move a laptop lid points to the hinge "
                "cable - repair shop job.",
            ], devices={"laptop"}),
        ],
        devices=HAS_SCREEN,
    ),
    Symptom(
        "no_internet", "No Wi-Fi / internet / Bluetooth",
        ["wifi", "wi-fi", "internet", "no connection", "won't connect", "wont connect",
         "disconnects", "drops", "offline", "network", "bluetooth", "pairing", "won't pair",
         "no signal", "no service", "slow internet"],
        [
            Cause("Router / modem needs a restart", 5, [
                "Unplug the router AND modem for 30 seconds, plug the modem in first, "
                "wait for it to settle, then the router.",
            ]),
            Cause("Device network glitch", 5, [
                "Toggle Wi-Fi/Bluetooth off and on; toggle Airplane mode.",
                "'Forget' the network or Bluetooth device and reconnect.",
                "Restart the device.",
            ]),
            Cause("Internet provider outage", 3, [
                "Check from another device. If nothing works, check the provider's "
                "outage page / app using mobile data.",
            ]),
            Cause("Too far from router / interference", 3, [
                "Move closer; use the 2.4 GHz band for range, 5 GHz for speed.",
                "Consider a mesh system for big or multi-floor homes.",
            ]),
            Cause("Network settings corrupted", 2, [
                "iPhone: Settings > General > Transfer or Reset > Reset Network Settings. "
                "Android: Settings > System > Reset options > Reset Wi-Fi & Bluetooth. "
                "Windows: Settings > Network > Advanced > Network reset.",
                "This forgets saved Wi-Fi passwords.",
            ]),
            Cause("Router firmware out of date / overheating", 2, [
                "Log into the router admin page and update firmware; keep it ventilated.",
            ], devices={"router"}),
            Cause("Bluetooth device still paired to something else", 3, [
                "Turn off Bluetooth on the other phone/PC, or put the device into "
                "pairing mode (usually hold the button until the light flashes).",
            ], devices={"headphones", "smartwatch", "phone", "tablet", "laptop", "console"}),
        ],
    ),
    Symptom(
        "no_sound", "No sound / bad audio / mic not working",
        ["no sound", "no audio", "sound", "audio", "speaker", "crackling", "muffled",
         "mic", "microphone", "can't hear", "cant hear", "one side", "one earbud", "volume"],
        [
            Cause("Muted or wrong output device selected", 5, [
                "Check volume, mute switch (iPhone side switch), and Do Not Disturb.",
                "Computer: pick the right output in the sound settings / tray icon.",
                "Phone: make sure it isn't still connected to Bluetooth headphones/car.",
            ]),
            Cause("Dirt in speaker / mic / earbud mesh", 4, [
                "Brush gently with a soft dry toothbrush; for earbuds, a dry cotton swab.",
            ], devices={"phone", "tablet", "headphones", "smartwatch", "laptop"}),
            Cause("App permission for microphone denied", 3, [
                "Settings > Privacy > Microphone - allow the app.",
            ], devices={"phone", "tablet", "laptop", "desktop"}),
            Cause("Audio driver problem", 3, [
                "Windows: Device Manager > Sound > uninstall device, then restart.",
            ], devices={"laptop", "desktop"}),
            Cause("Earbuds out of sync / unbalanced", 3, [
                "Check the left/right balance in accessibility settings.",
                "Reset the earbuds (put in case, hold the case button until the light "
                "flashes) and re-pair.",
            ], devices={"headphones"}),
            Cause("Blown / damaged speaker", 2, [
                "Crackling at every volume after a drop or water = hardware repair.",
            ]),
        ],
    ),
    Symptom(
        "water", "Water / liquid damage",
        ["water", "wet", "liquid", "dropped in", "spilled", "spill", "toilet", "pool",
         "rain", "coffee", "soda", "moisture"],
        [
            Cause("Liquid inside the device", 5, [
                "Power it off immediately. Do NOT charge it or plug it in.",
                "Remove case, SIM tray, removable battery and accessories.",
                "Wipe it dry, shake out gently with ports facing down.",
                "Let it air-dry in a dry, ventilated place for 24-48 h (rice does NOT "
                "help and adds dust).",
                "Sugary or salty liquid: get it to a repair shop soon - residue corrodes "
                "boards even after drying.",
                "Laptop spill: turn it upside down in a tent shape immediately.",
            ], warning="Do not charge or power on a wet device - it can short circuit."),
        ],
    ),
    Symptom(
        "storage_full", "Storage full",
        ["storage full", "out of space", "no space", "storage almost full", "disk full",
         "not enough storage", "memory full", "can't download"],
        [
            Cause("Photos, videos and messages filling storage", 5, [
                "Back up photos to cloud/computer, then delete local copies.",
                "Delete old message threads with big attachments.",
                "iPhone: Settings > General > iPhone Storage. Android: Settings > Storage.",
            ]),
            Cause("Large apps / caches / downloads", 4, [
                "Uninstall unused apps; clear app caches; empty Downloads and Trash.",
                "Windows: Settings > System > Storage > Storage Sense.",
            ]),
        ],
    ),
    Symptom(
        "buttons_ports", "Buttons, keys or ports not working",
        ["button", "keyboard", "key", "keys", "trackpad", "touchpad", "mouse", "usb",
         "port", "joystick", "drift", "controller", "stick drift", "hdmi port", "headphone jack"],
        [
            Cause("Dirt / debris under keys or in ports", 4, [
                "Compressed air at an angle; clean ports with a plastic pick.",
            ]),
            Cause("Software / driver glitch", 3, [
                "Restart; reconnect the accessory; update drivers/firmware.",
                "Controller: re-pair and update controller firmware.",
            ]),
            Cause("Controller stick drift (worn sensor)", 4, [
                "Recalibrate in the console's settings.",
                "Persistent drift = stick module replacement (cheap part, fiddly job) "
                "or warranty claim.",
            ], devices={"console"}),
            Cause("Physical wear or damage", 2, [
                "Use accessibility shortcuts (on-screen keyboard / AssistiveTouch) "
                "until repaired.",
            ]),
        ],
    ),
    Symptom(
        "printer_issues", "Printer won't print / jams / streaks",
        ["won't print", "wont print", "paper jam", "jam", "streaks", "faded", "offline printer",
         "printer offline", "ink", "toner", "spooler"],
        [
            Cause("Printer offline / wrong printer selected", 5, [
                "Turn the printer off and on; make sure it's on the same Wi-Fi.",
                "Windows: Settings > Bluetooth & devices > Printers - set the right default, "
                "remove duplicate copies.",
                "Restart the print spooler (Windows: services.msc > Print Spooler > Restart).",
            ]),
            Cause("Paper jam / scrap of paper inside", 4, [
                "Power off, open all doors, pull paper out in the direction it travels.",
            ]),
            Cause("Clogged print head / low ink or toner", 4, [
                "Run the print-head cleaning utility from the printer menu.",
                "Replace low cartridges; shake laser toner gently.",
            ]),
        ],
        devices={"printer"},
    ),
]

SYMPTOM_BY_KEY = {s.key: s for s in SYMPTOMS}


# ---------------------------------------------------------------------------
# Matching free text -> device kind + symptoms.
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    return " " + re.sub(r"\s+", " ", text) + " "


def _contains(text: str, keyword: str) -> bool:
    """Whole-word / whole-phrase match, so 'pc' doesn't match 'pcie'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text) is not None


def detect_device(text: str) -> str | None:
    """Return the device kind mentioned in the text (longest keyword wins)."""
    text = _normalize(text)
    best: tuple[int, str] | None = None
    for device, keywords in DEVICES.items():
        for kw in keywords:
            if _contains(text, kw) and (best is None or len(kw) > best[0]):
                best = (len(kw), device)
    return best[1] if best else None


def detect_symptoms(text: str) -> list[Symptom]:
    """Return symptoms mentioned in the text, strongest match first."""
    text = _normalize(text)
    scored = []
    for s in SYMPTOMS:
        hits = [kw for kw in s.keywords if _contains(text, kw)]
        if hits:
            scored.append((sum(len(h) for h in hits), s))
    scored.sort(key=lambda pair: -pair[0])
    return [s for _, s in scored]


def causes_for(symptom: Symptom, device: str | None) -> list[Cause]:
    """Causes that apply to this device kind, most likely first."""
    causes = [c for c in symptom.causes if device is None or c.devices is None or device in c.devices]
    return sorted(causes, key=lambda c: -c.likelihood)


# ---------------------------------------------------------------------------
# Output helpers.
# ---------------------------------------------------------------------------
LIKELIHOOD_LABEL = {5: "very likely", 4: "likely", 3: "possible", 2: "less likely", 1: "rare"}


def _heading(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


def print_diagnosis(device: str | None, symptoms: list[Symptom]) -> None:
    _heading("Diagnosis")
    print(f"  Device type : {device or 'not sure (showing advice for all devices)'}")
    print(f"  Symptoms    : {', '.join(s.title for s in symptoms)}")

    for symptom in symptoms:
        if device and symptom.devices is not None and device not in symptom.devices:
            print(f"\n  ({symptom.title} doesn't usually apply to a {device} - skipped.)")
            continue
        causes = causes_for(symptom, device)
        print(f"\n{symptom.title}")
        print("-" * len(symptom.title))
        for i, cause in enumerate(causes, 1):
            print(f"\n  {i}. {cause.name}  [{LIKELIHOOD_LABEL[cause.likelihood]}]")
            if cause.warning:
                print(f"     !! {cause.warning}")
            for step in cause.fixes:
                print(f"     - {step}")

    print("\nTip: work through the causes in order - the most likely and easiest "
          "fixes come first. If none help, contact the manufacturer or a repair shop.")


def _ask(prompt: str, options: list[str]) -> str | None:
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}. {opt}")
    raw = input("Choose a number (or press Enter to skip): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return None


def interactive_diagnose() -> int:
    print("Describe the problem in your own words, e.g. 'my laptop is slow and hot'.")
    text = input("> ").strip()
    device = detect_device(text)
    symptoms = detect_symptoms(text)

    if device is None:
        device = _ask("What kind of device is it?", list(DEVICES))
    if not symptoms:
        titles = [s.title for s in SYMPTOMS if device is None or s.devices is None or device in s.devices]
        chosen = _ask("Which of these best matches the problem?", titles)
        symptoms = [s for s in SYMPTOMS if s.title == chosen]
    if not symptoms:
        print("No symptom chosen - run `python diag_tool.py list` to see what's covered.")
        return 1
    print_diagnosis(device, symptoms)
    return 0


# ---------------------------------------------------------------------------
# Live scan of the machine this script runs on.
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    level: str  # "OK", "WARN", "PROBLEM", "INFO"
    title: str
    detail: str = ""
    fixes: list[str] = field(default_factory=list)


# SMBIOS chassis type codes -> human device kind.
CHASSIS_TYPES = {
    3: "desktop", 4: "desktop (low profile)", 5: "desktop (pizza box)", 6: "desktop (mini tower)",
    7: "desktop (tower)", 8: "laptop (portable)", 9: "laptop", 10: "laptop (notebook)",
    11: "handheld", 13: "all-in-one", 14: "laptop (sub-notebook)", 15: "desktop (space-saving)",
    17: "server (main server chassis)", 23: "server (rack mount)", 24: "desktop (sealed-case)",
    30: "tablet", 31: "laptop (convertible)", 32: "laptop (detachable)", 35: "mini PC",
    36: "stick PC",
}

VM_HINTS = ["virtualbox", "vmware", "kvm", "qemu", "hyper-v", "virtual machine", "xen",
            "parallels", "bochs", "amazon ec2", "google compute", "firecracker"]


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except ImportError:
        return None


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def identify_host() -> dict:
    """Work out what kind of electronic this machine is."""
    system = platform.system()
    info = {
        "os": f"{system} {platform.release()}".strip(),
        "arch": platform.machine(),
        "cpu": platform.processor() or None,
        "cpu_cores": os.cpu_count(),
        "vendor": None,
        "model": None,
        "kind": None,
    }

    if system == "Linux":
        info["vendor"] = _read("/sys/class/dmi/id/sys_vendor")
        info["model"] = _read("/sys/class/dmi/id/product_name")
        chassis = _read("/sys/class/dmi/id/chassis_type")
        if chassis and chassis.isdigit():
            info["kind"] = CHASSIS_TYPES.get(int(chassis))
        cpuinfo = _read("/proc/cpuinfo") or ""
        m = re.search(r"model name\s*:\s*(.+)", cpuinfo)
        if m:
            info["cpu"] = m.group(1).strip()
        if _read("/sys/firmware/devicetree/base/model"):  # Raspberry Pi etc.
            info["model"] = _read("/sys/firmware/devicetree/base/model").strip("\x00")
            info["kind"] = info["kind"] or "single-board computer"
        if os.path.exists("/.dockerenv") or "docker" in (_read("/proc/1/cgroup") or ""):
            info["kind"] = "container (Docker)"
        if os.environ.get("ANDROID_ROOT"):
            info["kind"] = "phone/tablet (Android, e.g. Termux)"
    elif system == "Darwin":
        model = _run(["sysctl", "-n", "hw.model"])
        info["vendor"] = "Apple"
        info["model"] = model
        info["cpu"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or info["cpu"]
        if model:
            info["kind"] = "laptop" if "book" in model.lower() else "desktop"
    elif system == "Windows":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "$c=Get-CimInstance Win32_ComputerSystem; "
                    "$e=Get-CimInstance Win32_SystemEnclosure; "
                    "\"$($c.Manufacturer)|$($c.Model)|$($e.ChassisTypes[0])\""])
        if out and out.count("|") == 2:
            vendor, model, chassis = out.split("|")
            info["vendor"], info["model"] = vendor or None, model or None
            if chassis.isdigit():
                info["kind"] = CHASSIS_TYPES.get(int(chassis))

    hint = " ".join(str(info[k] or "") for k in ("vendor", "model")).lower()
    if any(v in hint for v in VM_HINTS) or (system == "Linux" and _hypervisor_flag()):
        if not (info["kind"] or "").startswith("container"):
            info["kind"] = "virtual machine / cloud server"

    if info["kind"] is None:
        info["kind"] = "laptop (has a battery)" if _battery() else "unknown (probably a desktop or server)"
    return info


def _hypervisor_flag() -> bool:
    return " hypervisor " in f" {_read('/proc/cpuinfo') or ''} ".replace("\n", " ")


def _battery() -> dict | None:
    """Return {'percent', 'plugged', 'health'} or None if there's no battery."""
    ps = _psutil()
    if ps and hasattr(ps, "sensors_battery"):
        try:
            b = ps.sensors_battery()
        except Exception:
            b = None
        if b:
            return {"percent": b.percent, "plugged": b.power_plugged, "health": None}

    base = "/sys/class/power_supply"
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if _read(f"{base}/{name}/type") != "Battery":
                continue
            cap = _read(f"{base}/{name}/capacity")
            status = _read(f"{base}/{name}/status")
            full = _read(f"{base}/{name}/energy_full") or _read(f"{base}/{name}/charge_full")
            design = (_read(f"{base}/{name}/energy_full_design")
                      or _read(f"{base}/{name}/charge_full_design"))
            health = None
            if full and design and full.isdigit() and design.isdigit() and int(design):
                health = round(100 * int(full) / int(design))
            return {
                "percent": int(cap) if cap and cap.isdigit() else None,
                "plugged": status in ("Charging", "Full", "Not charging") if status else None,
                "health": health,
            }

    if platform.system() == "Darwin":
        out = _run(["pmset", "-g", "batt"]) or ""
        m = re.search(r"(\d+)%", out)
        if m and "InternalBattery" in out:
            return {"percent": int(m.group(1)), "plugged": "AC Power" in out, "health": None}
    return None


def _memory() -> tuple[int, int] | None:
    """Return (total, available) bytes."""
    ps = _psutil()
    if ps:
        vm = ps.virtual_memory()
        return vm.total, vm.available
    meminfo = _read("/proc/meminfo")
    if meminfo:
        vals = {}
        for line in meminfo.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                vals[key] = int(parts[0]) * 1024
        if "MemTotal" in vals and "MemAvailable" in vals:
            return vals["MemTotal"], vals["MemAvailable"]
    return None


def _temperatures() -> list[tuple[str, float]]:
    temps = []
    ps = _psutil()
    if ps and hasattr(ps, "sensors_temperatures"):
        try:
            for name, entries in (ps.sensors_temperatures() or {}).items():
                for e in entries:
                    if e.current:
                        temps.append((e.label or name, float(e.current)))
        except Exception:
            pass
    if not temps and os.path.isdir("/sys/class/thermal"):
        for zone in sorted(os.listdir("/sys/class/thermal")):
            if not zone.startswith("thermal_zone"):
                continue
            raw = _read(f"/sys/class/thermal/{zone}/temp")
            if raw and raw.lstrip("-").isdigit():
                temps.append((_read(f"/sys/class/thermal/{zone}/type") or zone, int(raw) / 1000))
    return temps


def _uptime_seconds() -> float | None:
    ps = _psutil()
    if ps:
        return time.time() - ps.boot_time()
    raw = _read("/proc/uptime")
    if raw:
        return float(raw.split()[0])
    return None


def run_scan() -> list[Finding]:
    findings: list[Finding] = []

    # Disk
    root = os.path.abspath(os.sep) if os.name != "nt" else os.environ.get("SystemDrive", "C:") + "\\"
    try:
        du = shutil.disk_usage(root)
        pct = 100 * du.used / du.total
        detail = f"{pct:.0f}% used ({_fmt_bytes(du.free)} free of {_fmt_bytes(du.total)}) on {root}"
        if pct >= 95:
            findings.append(Finding("PROBLEM", "Disk almost full", detail,
                                    SYMPTOM_BY_KEY["storage_full"].causes[1].fixes
                                    + ["A nearly full disk makes everything slow and can stop updates."]))
        elif pct >= 85:
            findings.append(Finding("WARN", "Disk getting full", detail,
                                    ["Free up space soon - aim for at least 15% free."]))
        else:
            findings.append(Finding("OK", "Disk space", detail))
    except OSError as exc:
        findings.append(Finding("INFO", "Disk space", f"could not read: {exc}"))

    # Memory
    mem = _memory()
    if mem:
        total, avail = mem
        pct = 100 * (total - avail) / total
        detail = f"{pct:.0f}% used ({_fmt_bytes(avail)} free of {_fmt_bytes(total)})"
        if pct >= 90:
            findings.append(Finding("PROBLEM", "Memory (RAM) nearly full", detail, [
                "Close programs and browser tabs you're not using.",
                "Restart to clear leaked memory.",
                "If it's always this high, the machine needs more RAM.",
            ]))
        elif pct >= 75:
            findings.append(Finding("WARN", "Memory (RAM) fairly busy", detail,
                                    ["Close unused apps if things feel slow."]))
        else:
            findings.append(Finding("OK", "Memory (RAM)", detail))
    else:
        findings.append(Finding("INFO", "Memory (RAM)", "could not read (install psutil for this OS)"))

    # CPU load
    cores = os.cpu_count() or 1
    load = None
    if hasattr(os, "getloadavg"):
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = None
    ps = _psutil()
    if load is None and ps:
        pct = ps.cpu_percent(interval=0.5)
        load = pct / 100 * cores
    if load is not None:
        ratio = load / cores
        detail = f"load {load:.2f} on {cores} cores ({ratio * 100:.0f}% busy)"
        if ratio >= 1.5:
            findings.append(Finding("PROBLEM", "CPU overloaded", detail, [
                "Find the culprit: Task Manager (Windows), Activity Monitor (macOS), "
                "`top` (Linux) - sort by CPU.",
                "Close or update it; a stuck process may need to be ended.",
            ]))
        elif ratio >= 0.9:
            findings.append(Finding("WARN", "CPU busy", detail,
                                    ["Something is working hard - check your task manager."]))
        else:
            findings.append(Finding("OK", "CPU load", detail))

    # Temperature
    temps = _temperatures()
    if temps:
        hottest_label, hottest = max(temps, key=lambda t: t[1])
        detail = f"hottest sensor {hottest_label}: {hottest:.0f} C"
        overheat_fixes = [step for c in causes_for(SYMPTOM_BY_KEY["overheating"], "laptop")[:2]
                          for step in c.fixes]
        if hottest >= 90:
            findings.append(Finding("PROBLEM", "Running very hot", detail, overheat_fixes))
        elif hottest >= 80:
            findings.append(Finding("WARN", "Running warm", detail, overheat_fixes[:2]))
        else:
            findings.append(Finding("OK", "Temperature", detail))

    # Battery
    batt = _battery()
    if batt:
        parts = []
        if batt["percent"] is not None:
            parts.append(f"{batt['percent']}% charged")
        if batt["plugged"] is not None:
            parts.append("plugged in" if batt["plugged"] else "on battery")
        if batt["health"] is not None:
            parts.append(f"health {batt['health']}% of original capacity")
        detail = ", ".join(parts)
        if batt["health"] is not None and batt["health"] < 80:
            findings.append(Finding("WARN", "Battery worn out", detail, [
                "Below 80% health, battery life is noticeably shorter.",
                "Replace the battery (manufacturer or repair shop).",
                "If the case bulges at all, stop using it - see 'Swollen battery'.",
            ]))
        elif batt["percent"] is not None and batt["percent"] <= 15 and not batt["plugged"]:
            findings.append(Finding("WARN", "Battery low", detail, ["Plug in the charger."]))
        else:
            findings.append(Finding("OK", "Battery", detail))

    # Uptime
    up = _uptime_seconds()
    if up is not None:
        days = up / 86400
        detail = f"running for {days:.1f} days since last restart"
        if days >= 14:
            findings.append(Finding("WARN", "Hasn't restarted in a while", detail, [
                "Restart - it clears memory leaks and finishes pending updates.",
            ]))
        else:
            findings.append(Finding("OK", "Uptime", detail))

    return findings


def print_scan() -> int:
    host = identify_host()
    _heading("What device is this?")
    print(f"  Device type : {host['kind']}")
    if host["vendor"] or host["model"]:
        print(f"  Make/model  : {' '.join(x for x in (host['vendor'], host['model']) if x)}")
    print(f"  System      : {host['os']} ({host['arch']})")
    if host["cpu"]:
        print(f"  Processor   : {host['cpu']} - {host['cpu_cores']} cores")

    findings = run_scan()
    _heading("What's going on")
    marks = {"OK": "[ OK ]", "WARN": "[WARN]", "PROBLEM": "[FAIL]", "INFO": "[INFO]"}
    for f in findings:
        print(f"  {marks[f.level]} {f.title}: {f.detail}")

    problems = [f for f in findings if f.fixes]
    _heading("How to fix it")
    if not problems:
        print("  Nothing wrong found. If something still feels off, describe it:")
        print('    python diag_tool.py diagnose "my laptop is slow"')
    for f in problems:
        print(f"\n  {f.title}:")
        for step in f.fixes:
            print(f"    - {step}")
    if _psutil() is None and platform.system() != "Linux":
        print("\n  (Install `psutil` - `pip install psutil` - for fuller results on this OS.)")
    return 1 if any(f.level == "PROBLEM" for f in findings) else 0


# ---------------------------------------------------------------------------
# identify: IMEI / serial.
# ---------------------------------------------------------------------------
def print_identify(imei: str | None, serial: str | None) -> int:
    ok = True
    if imei:
        info = analyze_imei(imei)
        _heading("IMEI")
        print(f"  Digits         : {info['digits']}")
        print(f"  15 digits      : {'yes' if info['length_ok'] else 'NO'}")
        print(f"  Checksum valid : {'yes' if info['valid_checksum'] else 'NO'}")
        if info["tac"]:
            print(f"  TAC (model ID) : {info['tac']}  <- first 8 digits identify make & model")
            print(f"  Device kind    : phone, tablet, smartwatch or cellular modem")
            print(f"  Look up the TAC: https://www.imei.info/?imei={info['digits']} "
                  "or your carrier's device checker")
        if not info["valid_checksum"]:
            ok = False
            print("  -> Doesn't look like a valid IMEI. Dial *#06# on the phone to show the "
                  "real one, or check Settings > About / the SIM tray.")
    if serial:
        s = analyze_apple_serial(serial)
        _heading("Serial number")
        print(f"  Normalized : {s['normalized']} ({s['length']} characters)")
        print(f"  Apple-style serial: {'plausible' if s['plausible'] else 'unlikely'}")
        print("  Apple: https://checkcoverage.apple.com  |  Samsung: https://www.samsung.com/us/support/warranty/")
        print("  Other brands: search '<brand> warranty check' and enter the serial there.")
        ok = ok and s["plausible"]
    return 0 if ok else 1


def knowledge_as_dict() -> dict:
    """The device + symptom knowledge base as plain JSON-able data (used by the app)."""
    def devs(d):
        return sorted(d) if d is not None else None
    return {
        "devices": DEVICES,
        "symptoms": [
            {
                "key": s.key, "title": s.title, "keywords": s.keywords, "devices": devs(s.devices),
                "causes": [
                    {"name": c.name, "likelihood": c.likelihood, "fixes": c.fixes,
                     "devices": devs(c.devices), "warning": c.warning}
                    for c in s.causes
                ],
            }
            for s in SYMPTOMS
        ],
    }


def print_list() -> None:
    _heading("Device types")
    for device, kws in DEVICES.items():
        print(f"  {device:<11} e.g. {', '.join(kws[:4])}")
    _heading("Symptoms")
    for s in SYMPTOMS:
        print(f"  {s.key:<15} {s.title}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose problems with electronic devices and suggest fixes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run `diag_tool.py <command> -h` for details on each command.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scan", help="check the computer this runs on")

    p_diag = sub.add_parser("diagnose", help="describe a problem, get causes and fixes")
    p_diag.add_argument("description", nargs="*", help="the problem in your own words")
    p_diag.add_argument("--device", choices=list(DEVICES), help="what kind of device it is")
    p_diag.add_argument("--symptom", action="append", choices=list(SYMPTOM_BY_KEY),
                        help="a known symptom (repeatable); see `list`")

    p_id = sub.add_parser("identify", help="identify a device from its IMEI or serial")
    p_id.add_argument("--imei")
    p_id.add_argument("--serial")

    sub.add_parser("list", help="show known device types and symptoms")

    p_export = sub.add_parser("export-json", help="write the knowledge base as JSON (for the app)")
    p_export.add_argument("path", nargs="?", default="app/assets/knowledge.json")

    args = parser.parse_args(argv)

    if args.command == "scan":
        return print_scan()

    if args.command == "diagnose":
        text = " ".join(args.description)
        if not text and not args.symptom:
            if not sys.stdin.isatty():
                parser.error("describe the problem, or use --symptom")
            return interactive_diagnose()
        device = args.device or (detect_device(text) if text else None)
        symptoms = [SYMPTOM_BY_KEY[k] for k in (args.symptom or [])]
        for s in detect_symptoms(text) if text else []:
            if s not in symptoms:
                symptoms.append(s)
        if not symptoms:
            print("Couldn't tell what the problem is from that description.")
            print("Try different words, or pick one with --symptom. Known symptoms:")
            for s in SYMPTOMS:
                print(f"  --symptom {s.key:<15} {s.title}")
            return 1
        print_diagnosis(device, symptoms)
        return 0

    if args.command == "identify":
        if not (args.imei or args.serial):
            parser.error("identify needs --imei and/or --serial")
        return print_identify(args.imei, args.serial)

    if args.command == "list":
        print_list()
        return 0

    if args.command == "export-json":
        with open(args.path, "w", encoding="utf-8") as fh:
            json.dump(knowledge_as_dict(), fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        print(f"Wrote {args.path}")
        # Also emit the account-recovery data (from phone_info) next to it.
        recovery_path = os.path.join(os.path.dirname(args.path) or ".", "recovery.json")
        from phone_info import recovery_as_dict
        with open(recovery_path, "w", encoding="utf-8") as fh:
            json.dump(recovery_as_dict(), fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        print(f"Wrote {recovery_path}")
        # Device-free reference data (model maps, car codes) for the app.
        reference_path = os.path.join(os.path.dirname(args.path) or ".", "reference.json")
        from device_export import reference_as_dict
        with open(reference_path, "w", encoding="utf-8") as fh:
            json.dump(reference_as_dict(), fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        print(f"Wrote {reference_path}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
