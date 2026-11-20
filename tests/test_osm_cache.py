import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pbradar.discovery import RegionIndex, download_file, ensure_regions, name_region
from pbradar.osm_index import build_osm_index, find_indexed_leads, index_is_current
from pbradar.osm_storage import cleanup_abandoned_download, inspect_osm


OSM_SAMPLE = '''<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="tests">
  <node id="1" lat="49.84" lon="24.03" version="1">
    <tag k="name" v="Львівське архітектурне бюро"/>
    <tag k="office" v="architect"/>
    <tag k="phone" v="+380671234567"/>
  </node>
  <node id="2" lat="50.45" lon="30.52" version="1">
    <tag k="name" v="Київське будівництво"/>
    <tag k="office" v="construction_company"/>
    <tag k="website" v="https://example.org"/>
  </node>
</osm>'''


class OfflineBoundariesTests(unittest.TestCase):
    def test_all_ukrainian_regions_are_bundled_without_http(self):
        from pbradar.core import REGIONS

        with patch("pbradar.discovery.requests.get", side_effect=AssertionError("HTTP forbidden")):
            idx = RegionIndex(ensure_regions())
        self.assertEqual({item[0] for item in idx.items}, set(REGIONS))
        self.assertEqual(idx.at(24.03, 49.84), "Львівська")
        self.assertEqual(idx.at(30.52, 50.45), "м. Київ")
        self.assertEqual(name_region("Kyiv"), "м. Київ")


class IndexTests(unittest.TestCase):
    def test_reuse_and_region_filter_without_rescan(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "ukraine.osm"
            db = Path(folder) / "osm-index.sqlite3"
            source.write_text(OSM_SAMPLE, encoding="utf-8")
            index = RegionIndex(ensure_regions())
            self.assertEqual(build_osm_index(source, index, db_path=db), 2)
            self.assertTrue(index_is_current(source, db))
            lviv = find_indexed_leads(source, {"Архітектурні бюро"}, {"Львівська"}, db)
            self.assertEqual(len(lviv), 1)
            self.assertEqual(lviv[0]["phones"], ["+380671234567"])
            self.assertEqual(find_indexed_leads(source, {"Архітектурні бюро"}, {"м. Київ"}, db), [])
            kyiv = find_indexed_leads(source, {"Будівельні компанії"}, {"м. Київ"}, db)
            self.assertEqual(len(kyiv), 1)
            self.assertEqual(kyiv[0]["website"], "https://example.org")
            with patch("osmium.SimpleHandler.apply_file", side_effect=AssertionError("rescan")):
                self.assertEqual(build_osm_index(source, index, db_path=db), 2)
            source.write_text(OSM_SAMPLE + "\n", encoding="utf-8")
            self.assertFalse(index_is_current(source, db))
            self.assertEqual(build_osm_index(source, index, db_path=db), 2)
            self.assertTrue(index_is_current(source, db))
            self.assertFalse(db.with_name(db.name + ".partial").exists())

    def test_cancelled_rebuild_does_not_replace_previous_index(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "ukraine.osm"
            db = Path(folder) / "osm-index.sqlite3"
            source.write_text(OSM_SAMPLE, encoding="utf-8")
            index = RegionIndex(ensure_regions())
            build_osm_index(source, index, db_path=db)
            old_content = db.read_bytes()
            source.write_text(OSM_SAMPLE + "\n", encoding="utf-8")
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(InterruptedError):
                build_osm_index(source, index, cancel=cancelled, db_path=db)
            self.assertEqual(db.read_bytes(), old_content)


class StorageTests(unittest.TestCase):
    def test_fresh_ready_and_stale(self):
        now = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "ukraine-latest.osm.pbf"
            self.assertEqual(inspect_osm(data, now).status, "Потребує оновлення")
            data.write_bytes(b"pbf")
            import os
            for days, expected in [(0, "Оновлений"), (2, "Готовий"), (8, "Потребує оновлення")]:
                timestamp = (now - dt.timedelta(days=days)).timestamp()
                os.utime(data, (timestamp, timestamp))
                self.assertEqual(inspect_osm(data, now).status, expected)

    def test_cancelled_download_preserves_previous_copy(self):
        class Response:
            headers = {"Content-Length": "10"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def raise_for_status(self):
                pass
            def iter_content(self, _):
                yield b"new"
                yield b"failed"

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "ukraine-latest.osm.pbf"
            data.write_bytes(b"previous")
            cancelled = threading.Event()
            cancelled.set()
            with patch("pbradar.discovery.requests.get", return_value=Response()):
                with self.assertRaises(InterruptedError):
                    download_file("https://example.org/data", data, cancel=cancelled)
            self.assertEqual(data.read_bytes(), b"previous")
            self.assertFalse(data.with_suffix(".pbf.partial").exists())

    def test_old_abandoned_partial_only_is_removed(self):
        now = dt.datetime.now(dt.timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "ukraine-latest.osm.pbf"
            partial = data.with_suffix(".pbf.partial")
            data.write_bytes(b"good")
            partial.write_bytes(b"old")
            import os
            old = (now - dt.timedelta(days=2)).timestamp()
            os.utime(partial, (old, old))
            self.assertTrue(cleanup_abandoned_download(data, now))
            self.assertEqual(data.read_bytes(), b"good")
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
