"""Run after `flutter create` in CI: lets the app talk to the PC over plain
http on the home network / Tailscale (Android cleartext, iOS ATS + local network)."""
import re, sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent

man = root / "android/app/src/main/AndroidManifest.xml"
if man.exists():
    t = man.read_text()
    if "android.permission.INTERNET" not in t:
        t = t.replace("<application", '<uses-permission android:name="android.permission.INTERNET"/>\n    <application', 1)
    if "usesCleartextTraffic" not in t:
        t = t.replace("<application", '<application android:usesCleartextTraffic="true"', 1)
    t = re.sub(r'android:label="[^"]*"', 'android:label="Local AI"', t, count=1)
    man.write_text(t); print("patched", man)

plist = root / "ios/Runner/Info.plist"
if plist.exists():
    t = plist.read_text()
    add = """	<key>NSAppTransportSecurity</key>
	<dict>
		<key>NSAllowsArbitraryLoads</key><true/>
		<key>NSAllowsLocalNetworking</key><true/>
	</dict>
	<key>NSLocalNetworkUsageDescription</key>
	<string>Local AI connects to the AI running on your PC over your home network.</string>
"""
    if "NSAppTransportSecurity" not in t:
        i = t.rindex("</dict>")
        t = t[:i] + add + t[i:]
    t = re.sub(r"(<key>CFBundleDisplayName</key>\s*<string>)[^<]*", r"\1Local AI", t)
    plist.write_text(t); print("patched", plist)
