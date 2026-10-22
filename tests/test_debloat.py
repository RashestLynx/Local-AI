"""Safety tests for the debloat / app cleanup module."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import debloat  # noqa: E402

APPS = [
    {"name": "NVIDIA Graphics Driver", "kind": "uninstall", "id": "nv", "command": "nv.exe /u", "last_used": None, "protected": True},
    {"name": "Steam", "kind": "uninstall", "id": "steam", "command": "s.exe", "last_used": None, "protected": True},
    {"name": "Some Toolbar", "kind": "uninstall", "id": "tb", "command": "\"C:\\tb\\uninst.exe\" /S", "size_mb": 300,
     "last_used": None, "protected": False},
    {"name": "Windows Maps", "kind": "appx", "id": "Microsoft.WindowsMaps", "last_used": None, "protected": True},
    {"name": "Calculator", "kind": "appx", "id": "Microsoft.WindowsCalculator", "last_used": "2026-10-01", "protected": True},
    {"name": "Spotify", "kind": "appx", "id": "SpotifyAB.SpotifyMusic", "last_used": "2020-01-01", "protected": False},
    {"name": "VLC", "kind": "uninstall", "id": "vlc", "command": "vlc-uninst.exe", "last_used": "2099-01-01", "protected": False},
]


class DebloatSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = debloat.Debloat(self.tmp.name)
        self.d.apps_path.write_text(json.dumps(APPS), encoding="utf-8")
        self.apps = {a["id"]: a for a in self.d.apps()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_protected_never_suggested(self):
        for i in ("nv", "steam", "Microsoft.WindowsCalculator"):
            self.assertTrue(self.apps[i]["protected"], i)
            self.assertFalse(self.apps[i]["suggest"], i)

    def test_known_windows_extras_are_removable(self):
        maps = self.apps["Microsoft.WindowsMaps"]
        self.assertFalse(maps["protected"])
        self.assertTrue(maps["suggest"])
        self.assertNotIn("windows", maps["name"].lower())  # so cleanup.ps1's name check won't skip it

    def test_suggestions(self):
        self.assertTrue(self.apps["tb"]["suggest"])        # never opened desktop program
        self.assertTrue(self.apps["SpotifyAB.SpotifyMusic"]["suggest"])  # not opened in years
        self.assertFalse(self.apps["vlc"]["suggest"])      # used recently

    def test_plan_uses_only_local_scan_data(self):
        items, refused = self.d.plan(["uninstall:nv", "uninstall:tb", "appx:Microsoft.WindowsMaps",
                                      "uninstall:made-up", "tweak:ads-off"], local=True)
        kinds = {(i["kind"], i["id"]) for i in items}
        self.assertIn(("uninstall", "tb"), kinds)
        self.assertIn(("appx", "Microsoft.WindowsMaps"), kinds)
        self.assertIn(("tweak", "ads-off"), kinds)
        self.assertNotIn(("uninstall", "nv"), kinds)
        self.assertEqual(next(i for i in items if i["id"] == "tb")["command"], APPS[2]["command"])
        self.assertEqual(len(refused), 2)

    def test_desktop_uninstallers_need_the_pc(self):
        items, refused = self.d.plan(["uninstall:tb", "appx:SpotifyAB.SpotifyMusic"], local=False)
        self.assertEqual([i["id"] for i in items], ["SpotifyAB.SpotifyMusic"])
        self.assertEqual(len(refused), 1)

    def test_question_detection(self):
        for q in ("can you debloat my pc", "what apps can I remove", "uninstall apps i never use", "help me deblote windows"):
            self.assertTrue(debloat.CLEANUP_QUESTION.search(q), q)
        self.assertFalse(debloat.CLEANUP_QUESTION.search("write me a poem about apples"))

    def test_ai_summary(self):
        s = self.d.summary_for_ai(self.d.apps())
        self.assertIn("Some Toolbar", s)
        self.assertNotIn("NVIDIA", s.split("OTHER REMOVABLE")[0])


if __name__ == "__main__":
    unittest.main()
