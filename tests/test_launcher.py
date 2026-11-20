import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pbradar import launcher


class ResponseStub:
    def __init__(self, value: bytes):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.value


def archive_with_file(filename="PB-Radar.exe", contents=b"demo binary"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(filename, contents)
    return out.getvalue()


class LauncherTests(unittest.TestCase):
    def test_installed_launcher_uses_persistent_user_data_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "PB-Radar"
            with patch.object(launcher, "BASE_DIR", base), \
                 patch.object(launcher, "VERSIONS", base / "versions"), \
                 patch.object(launcher, "OSM_FILE", base / "cache" / "ukraine-latest.osm.pbf"):
                launcher.ensure_data_directories()
                self.assertTrue((base / "cache").is_dir())
                self.assertTrue((base / "versions").is_dir())
                osm = base / "cache" / "ukraine-latest.osm.pbf"
                osm.write_bytes(b"already downloaded nationwide OSM")
                launcher.ensure_data_directories()
                self.assertEqual(osm.read_bytes(), b"already downloaded nationwide OSM")

    def test_install_verify_and_preserve_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            data = archive_with_file()
            info = {"version": "v0.1.0", "sha256": hashlib.sha256(data).hexdigest(),
                    "url": "https://github.com/user/repo/releases/download/v0.1.0/PB-Radar-App.zip"}
            with patch.object(launcher, "BASE_DIR", base), \
                    patch.object(launcher, "VERSIONS", base / "versions"), \
                    patch.object(launcher, "CURRENT", base / "current.json"), \
                    patch.object(launcher.requests, "get", return_value=ResponseStub(data)):
                app = launcher.install_release(info)
                self.assertEqual(app.read_bytes(), b"demo binary")
                self.assertEqual(launcher.read_current(), "v0.1.0")
                self.assertEqual(launcher.stored_versions(), ["v0.1.0"])

    def test_reject_tampered_package(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            data = archive_with_file()
            info = {"version": "v0.1.0", "sha256": "0" * 64,
                    "url": "https://github.com/user/repo/releases/download/v0.1.0/PB-Radar-App.zip"}
            with patch.object(launcher, "BASE_DIR", base), \
                    patch.object(launcher, "VERSIONS", base / "versions"), \
                    patch.object(launcher, "CURRENT", base / "current.json"), \
                    patch.object(launcher.requests, "get", return_value=ResponseStub(data)):
                with self.assertRaisesRegex(ValueError, "не збігається"):
                    launcher.install_release(info)
                self.assertFalse((base / "current.json").exists())

    def test_reject_unexpected_zip_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            data = archive_with_file("../../evil.exe")
            info = {"version": "v0.1.0", "sha256": hashlib.sha256(data).hexdigest(),
                    "url": "https://github.com/user/repo/releases/download/v0.1.0/PB-Radar-App.zip"}
            with patch.object(launcher, "BASE_DIR", base), \
                    patch.object(launcher, "VERSIONS", base / "versions"), \
                    patch.object(launcher, "CURRENT", base / "current.json"), \
                    patch.object(launcher.requests, "get", return_value=ResponseStub(data)):
                with self.assertRaisesRegex(ValueError, "вміст"):
                    launcher.install_release(info)
                self.assertFalse((base / "evil.exe").exists())


if __name__ == "__main__":
    unittest.main()
