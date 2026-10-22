#!/usr/bin/env python3
"""phone_info.py - look up public info about a phone and plan account recovery.

This tool does NOT bypass locks or pull data from a locked device. It only:
  * validates an IMEI (Luhn checksum) and splits out its parts (TAC / serial),
  * does a light sanity check on an Apple-style serial number,
  * prints official first-party account-recovery services,
  * prints step-by-step recovery checklists (--checklist),
  * explains where your backups likely live (--backups),
  * formats the details you have into an ownership-proof sheet (--organize),
  * explains the official, approval-based way to pull data off an UNLOCKED
    device (--connect).

The data on a locked phone is encrypted with the passcode. The path back to
your photos is: recover the ACCOUNT -> unlock/restore the device -> copy data
off with the device's own "Trust"/USB-approval prompt. A locked phone cannot
grant that approval, by design.

Examples:
    python phone_info.py --imei 490154203237518
    python phone_info.py --serial C39XY1234567 --platform apple
    python phone_info.py --checklist apple
    python phone_info.py --backups samsung
    python phone_info.py --organize --email old@mail.com --phone 555-0100 --save sheet.txt
    python phone_info.py --connect apple
"""

from __future__ import annotations

import argparse
import datetime as _dt

# ---------------------------------------------------------------------------
# Official, first-party services. Recover the ACCOUNT; the account gives you
# the device and its backups.
# ---------------------------------------------------------------------------
RECOVERY_SERVICES = {
    "apple": [
        ("Apple Account recovery", "https://iforgot.apple.com"),
        ("Check coverage / verify a device", "https://checkcoverage.apple.com"),
        ("iCloud (backups, photos, Find My)", "https://www.icloud.com"),
        ("Manage your Apple Account", "https://appleid.apple.com"),
    ],
    "samsung": [
        ("Samsung account recovery", "https://account.samsung.com"),
        ("Find My Mobile (remote unlock if enabled)", "https://findmymobile.samsung.com"),
        ("Google account recovery", "https://accounts.google.com/signin/recovery"),
        ("Google Find Hub (device + backups)", "https://www.google.com/android/find"),
    ],
}

# ---------------------------------------------------------------------------
# 1. Step-by-step recovery checklists.
# ---------------------------------------------------------------------------
CHECKLISTS = {
    "apple": [
        "On the locked iPhone: after several wrong tries, look for 'Forgot",
        "  Passcode?' / 'iPhone Unavailable' -> it can reset via Apple Account.",
        "Go to https://iforgot.apple.com and enter the Apple ID (often your",
        "  old email). You do NOT need to receive mail there if you have a",
        "  trusted phone number or another signed-in Apple device.",
        "Choose 'Can't access your trusted number/device?' to start Account",
        "  Recovery - Apple verifies ownership over a waiting period.",
        "Once in the Apple Account: visit https://www.icloud.com to see Photos",
        "  and whether an iCloud Backup exists.",
        "Confirm the device at https://checkcoverage.apple.com using the serial.",
        "Last resort / proof of ownership: Apple Store Genius Bar with receipt.",
    ],
    "samsung": [
        "Try Samsung account first: https://account.samsung.com -> 'Find ID'",
        "  / 'Reset password' using your phone number.",
        "If Find My Mobile was enabled, https://findmymobile.samsung.com can",
        "  remotely unlock the phone once you're back in the Samsung account.",
        "Recover the Google account at",
        "  https://accounts.google.com/signin/recovery - use the phone number",
        "  and a familiar device; the dead email is not required.",
        "Check backups: https://www.google.com/android/find and",
        "  https://photos.google.com (Google Photos backup).",
        "Proof of ownership / last resort: Samsung support with receipt + IMEI.",
    ],
}

# ---------------------------------------------------------------------------
# 2. Where backups usually live.
# ---------------------------------------------------------------------------
BACKUP_GUIDE = {
    "apple": [
        "iCloud Backup  -> https://www.icloud.com (Photos) + Settings once in.",
        "iCloud Photos  -> syncs automatically if it was ever turned on.",
        "Computer backup-> a Mac (Finder) or PC (iTunes/Apple Devices app) you",
        "                 synced with before may hold a full local backup.",
        "Old backups can live on any computer the phone was plugged into.",
    ],
    "samsung": [
        "Google Photos  -> https://photos.google.com (auto-backup, very common).",
        "Google Drive   -> https://drive.google.com (app data, some media).",
        "Samsung Cloud  -> via the Samsung account (varies by region/model).",
        "Computer       -> Samsung Smart Switch backups on a PC/Mac, if used.",
    ],
}

# ---------------------------------------------------------------------------
# 5. Official, approval-based data pull from an UNLOCKED device.
# ---------------------------------------------------------------------------
CONNECT_GUIDE = {
    "apple": [
        "REQUIRES the iPhone to be UNLOCKED - this is the on-device approval.",
        "Plug the iPhone into a Mac or PC with a cable.",
        "On the phone, tap 'Trust This Computer' and enter the passcode.",
        "  (A locked phone cannot show or accept this prompt - that's the point.)",
        "Mac: open Finder -> select the iPhone -> 'Back Up Now'.",
        "PC : open the Apple Devices app (or iTunes) -> Back Up.",
        "Photos can also be imported via the Photos app / 'Import' once trusted.",
    ],
    "samsung": [
        "REQUIRES the phone to be UNLOCKED - this is the on-device approval.",
        "Plug the phone into a computer with a cable.",
        "On the phone, tap the USB notification -> choose 'File Transfer (MTP)'.",
        "  (This prompt only appears once the phone is unlocked.)",
        "Copy DCIM/ and other folders off with your file explorer.",
        "Or use Samsung Smart Switch (desktop) for a full backup.",
        "Advanced: USB debugging + adb also needs an on-device 'Allow' tap.",
    ],
}


def luhn_is_valid(number: str) -> bool:
    """Return True if the digit string passes the Luhn checksum (used by IMEI)."""
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(number[::-1]):
        d = int(ch)
        if i % 2 == 1:  # every second digit from the right is doubled
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def analyze_imei(imei: str) -> dict:
    """Break an IMEI into its parts (TAC / serial / check digit) and validate."""
    cleaned = "".join(c for c in imei if c.isdigit())
    result = {
        "digits": cleaned,
        "length_ok": len(cleaned) == 15,
        "valid_checksum": False,
        "tac": None,
        "serial": None,
        "check_digit": None,
    }
    if len(cleaned) == 15:
        result["valid_checksum"] = luhn_is_valid(cleaned)
        result["tac"] = cleaned[:8]
        result["serial"] = cleaned[8:14]
        result["check_digit"] = cleaned[14]
    return result


def analyze_apple_serial(serial: str) -> dict:
    """Light structural check for an Apple serial number (10-12 alphanumerics)."""
    cleaned = serial.strip().upper()
    return {
        "normalized": cleaned,
        "length": len(cleaned),
        "plausible": 10 <= len(cleaned) <= 12 and cleaned.isalnum(),
    }


def _print_block(title: str, lines: list[str]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for line in lines:
        print(f"  {line}")


def print_services(platform: str | None) -> None:
    keys = [platform] if platform in RECOVERY_SERVICES else list(RECOVERY_SERVICES)
    print("\nOfficial recovery / lookup services")
    print("-" * 35)
    print("  Recover the ACCOUNT first; it unlocks the device and backups.")
    for key in keys:
        print(f"\n  {key.capitalize()}:")
        for name, url in RECOVERY_SERVICES[key]:
            print(f"    - {name}: {url}")


def build_ownership_sheet(args: argparse.Namespace) -> str:
    """Format whatever ownership details the user supplied into a clean sheet."""
    fields = [
        ("Owner name", args.name),
        ("Old / account email", args.email),
        ("Phone number on account", args.phone),
        ("Carrier", args.carrier),
        ("Approx. purchase date", args.purchase_date),
        ("Device model", args.model),
        ("IMEI", args.imei),
        ("Serial number", args.serial),
    ]
    out = [
        "DEVICE OWNERSHIP SHEET",
        f"Prepared: {_dt.date.today().isoformat()}",
        "(Hand this to Apple/Samsung support to help verify you own the device.)",
        "",
    ]
    for label, value in fields:
        out.append(f"  {label:<26}: {value if value else '____________________'}")
    out += [
        "",
        "  Proof of purchase (receipt/box) : [ ] attached  [ ] to find",
        "  Government photo ID             : [ ] ready",
    ]
    return "\n".join(out)


def recovery_as_dict() -> dict:
    """The account-recovery knowledge base as plain JSON-able data (for the app).

    Keeps the same content the CLI prints, so the app's Recovery tab and this
    script stay in step.
    """
    return {
        "services": {
            platform: [{"name": name, "url": url} for name, url in entries]
            for platform, entries in RECOVERY_SERVICES.items()
        },
        "checklists": CHECKLISTS,
        "backups": BACKUP_GUIDE,
        "connect": CONNECT_GUIDE,
        "ownership_fields": [
            "Owner name",
            "Old / account email",
            "Phone number on account",
            "Carrier",
            "Approx. purchase date",
            "Device model",
            "IMEI",
            "Serial number",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--imei", help="15-digit IMEI to validate and break down")
    parser.add_argument("--serial", help="device serial number to sanity-check")
    parser.add_argument(
        "--platform", choices=sorted(RECOVERY_SERVICES),
        help="limit the service list to apple or samsung",
    )
    parser.add_argument(
        "--checklist", choices=sorted(CHECKLISTS),
        help="print a step-by-step account-recovery checklist",
    )
    parser.add_argument(
        "--backups", choices=sorted(BACKUP_GUIDE),
        help="explain where your backups most likely live",
    )
    parser.add_argument(
        "--connect", choices=sorted(CONNECT_GUIDE),
        help="official approval-based data pull (needs an UNLOCKED device)",
    )
    # Ownership-sheet organizer:
    parser.add_argument("--organize", action="store_true",
                        help="format supplied details into an ownership sheet")
    parser.add_argument("--name")
    parser.add_argument("--email")
    parser.add_argument("--phone")
    parser.add_argument("--carrier")
    parser.add_argument("--purchase-date", dest="purchase_date")
    parser.add_argument("--model")
    parser.add_argument("--save", help="write the ownership sheet to this file too")

    args = parser.parse_args(argv)

    did_something = False

    if args.imei:
        did_something = True
        info = analyze_imei(args.imei)
        lines = [
            f"cleaned digits : {info['digits']}",
            f"length is 15   : {info['length_ok']}",
            f"checksum valid : {info['valid_checksum']}",
        ]
        if info["tac"]:
            lines += [
                f"TAC (make/model prefix) : {info['tac']}",
                f"device serial portion   : {info['serial']}",
                f"Luhn check digit        : {info['check_digit']}",
            ]
        if info["length_ok"] and not info["valid_checksum"]:
            lines.append("NOTE: 15 digits but checksum fails - likely mistyped.")
        _print_block("IMEI analysis", lines)

    if args.serial:
        did_something = True
        s = analyze_apple_serial(args.serial)
        _print_block("Serial analysis", [
            f"normalized : {s['normalized']}",
            f"length     : {s['length']}",
            f"plausible  : {s['plausible']}",
            "(Use checkcoverage.apple.com for authoritative Apple device info.)",
        ])

    if args.checklist:
        did_something = True
        _print_block(f"Recovery checklist ({args.checklist})", CHECKLISTS[args.checklist])

    if args.backups:
        did_something = True
        _print_block(f"Where your backups likely live ({args.backups})",
                     BACKUP_GUIDE[args.backups])

    if args.connect:
        did_something = True
        _print_block(f"Official data pull from an UNLOCKED device ({args.connect})",
                     CONNECT_GUIDE[args.connect])

    if args.organize:
        did_something = True
        sheet = build_ownership_sheet(args)
        print("\n" + sheet)
        if args.save:
            with open(args.save, "w", encoding="utf-8") as fh:
                fh.write(sheet + "\n")
            print(f"\n  (Saved to {args.save})")

    if not did_something:
        parser.error(
            "give at least one action: --imei, --serial, --checklist, "
            "--backups, --connect, or --organize"
        )

    # Always finish by pointing at the official services.
    print_services(args.platform or args.checklist or args.backups or args.connect)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
