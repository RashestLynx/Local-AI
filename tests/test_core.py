"""Basic safety and rules tests for Local AI (run: python -m unittest discover -s tests)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import files  # noqa: E402
import pcinfo  # noqa: E402
import scanner  # noqa: E402


class FileSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "notes.txt").write_text("hello")
        (self.root / "passwords.txt").write_text("nope")
        (self.root / "AppData").mkdir()
        self.cfg = {"file_roots": [str(self.root)]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_inside_root_allowed(self):
        self.assertEqual(files.resolve(self.cfg, str(self.root / "notes.txt")), self.root / "notes.txt")

    def test_path_traversal_blocked(self):
        with self.assertRaises(PermissionError):
            files.resolve(self.cfg, str(self.root / ".." / ".." / "etc"))

    def test_private_names_blocked(self):
        with self.assertRaises(PermissionError):
            files.resolve(self.cfg, str(self.root / "passwords.txt"))
        with self.assertRaises(PermissionError):
            files.resolve(self.cfg, str(self.root / "AppData"))

    def test_listing_hides_private(self):
        names = [e["name"] for e in files.list_dir(self.cfg, str(self.root))["entries"]]
        self.assertIn("notes.txt", names)
        self.assertNotIn("passwords.txt", names)
        self.assertNotIn("AppData", names)

    def test_recycle_refuses_root(self):
        with self.assertRaises(Exception):
            files.to_recycle_bin(self.cfg, str(self.root))

    def test_upload_never_overwrites(self):
        old = files.UPLOAD_DIR
        files.UPLOAD_DIR = self.root / "up"
        try:
            a = files.save_upload("x.txt", b"1")
            b = files.save_upload("x.txt", b"2")
            self.assertNotEqual(str(a), str(b))
        finally:
            files.UPLOAD_DIR = old

    def test_file_question(self):
        self.assertTrue(files.FILE_QUESTION.search("where is my resume file"))


class ScannerRules(unittest.TestCase):
    def test_random_names(self):
        self.assertTrue(scanner.looks_random("x8kq2vz9pl3"))
        self.assertTrue(scanner.looks_random("a3f9c0d1e2b4c5d6e7f8"))
        self.assertFalse(scanner.looks_random("WidgetService"))
        self.assertFalse(scanner.looks_random("steamwebhelper"))

    def test_encoded_powershell(self):
        self.assertTrue(scanner.ENCODED_PS.search("powershell -enc " + "A" * 60))
        self.assertFalse(scanner.ENCODED_PS.search("powershell -File backup.ps1"))


class PcInfo(unittest.TestCase):
    def test_pick_checks(self):
        self.assertIn("gpu", pcinfo.pick_checks("how much vram does my gpu have"))
        self.assertIn("battery", pcinfo.pick_checks("is my battery charging"))
        self.assertEqual(pcinfo.pick_checks("write me a poem"), [])


if __name__ == "__main__":
    unittest.main()
