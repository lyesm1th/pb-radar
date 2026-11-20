import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pbradar.discovery import (OSM_MIRROR_URL, RegionIndex, download_file,
                               download_ukraine_osm, ensure_regions, name_region, resolve_osm_url)
from pbradar.osm_index import (build_osm_index, count_osm_objects,
                               find_indexed_leads, index_is_current)
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
    def test_exact_total_counts_nodes_ways_and_relations(self):
        sample = OSM_SAMPLE.replace("</osm>", '''<way id="3" version="1">
          <nd ref="1"/><nd ref="2"/>
          <tag k="highway" v="service"/>
          </way>
          <relation id="4" version="1">
          <member type="way" ref="3" role="outer"/>
          <tag k="type" v="multipolygon"/>
          </relation></osm>''')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "ukraine.osm"
            source.write_text(sample, encoding="utf-8")
            self.assertEqual(count_osm_objects(source), (2, 1, 1))
            updates = []
            db = Path(folder) / "index.sqlite3"
            count = build_osm_index(source, RegionIndex(ensure_regions()), db_path=db,
                                    on_progress=lambda done, total: updates.append((done, total)))
            self.assertEqual(count, 2)
            self.assertEqual(updates[0], (0, 4))
            self.assertEqual(updates[-1], (4, 4))
            import sqlite3
            with sqlite3.connect(db) as con:
                metadata = dict(con.execute("SELECT key, value FROM meta"))
            self.assertEqual(metadata["total_objects"], "4")
            self.assertEqual(metadata["relevant_candidates"], "2")

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
    def test_unavailable_geofabrik_switches_to_ukraine_wide_mirror(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ukraine.osm.pbf"
            path.write_bytes(b"previous")
            attempted = []

            def fake_download(url, destination, **kwargs):
                attempted.append(url)
                if len(attempted) == 1:
                    raise ValueError("Джерело зациклює перенаправлення")
                destination.write_bytes(b"valid mirror data")

            with patch("pbradar.discovery.resolve_osm_url",
                       return_value="https://download.geofabrik.de/europe/ukraine/ukraine-261119.osm.pbf"), \
                 patch("pbradar.discovery.download_file", side_effect=fake_download):
                selected = download_ukraine_osm(path)
            self.assertEqual(selected, "OSM France")
            self.assertEqual(attempted[-1], OSM_MIRROR_URL)
            self.assertEqual(path.read_bytes(), b"valid mirror data")

    def test_safe_single_redirect_is_supported(self):
        class Redirect:
            status_code = 302
            headers = {"Location": "https://download.geofabrik.de/europe/ukraine/ukraine-261119.osm.pbf"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        class Download:
            status_code = 200
            headers = {"Content-Length": "3"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def raise_for_status(self):
                pass
            def iter_content(self, _):
                yield b"new"

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ukraine.osm.pbf"
            with patch("pbradar.discovery.requests.get", side_effect=[Redirect(), Download()]) as get:
                download_file("https://download.geofabrik.de/old", target)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(get.call_count, 2)

    def test_direct_geofabrik_archive_is_selected_instead_of_redirecting_latest(self):
        class Listing:
            status_code = 200
            text = '''<a href="https://download.geofabrik.de/europe/ukraine/ukraine-latest.osm.pbf">latest</a>
                      <a href="ukraine-261118.osm.pbf">older</a>
                      <a href="ukraine-261119.osm.pbf">newer</a>'''

        with patch("pbradar.discovery.requests.get", return_value=Listing()) as get:
            selected = resolve_osm_url()
        self.assertEqual(selected,
                         "https://download.geofabrik.de/europe/ukraine/ukraine-261119.osm.pbf")
        self.assertIn("index.html", get.call_args.args[0])
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_geofabrik_listing_fallback_probes_direct_dated_file(self):
        class Unavailable:
            status_code = 503

        class File:
            status_code = 200
            headers = {"Content-Length": "885221667"}

        today = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
        with patch("pbradar.discovery.requests.get", return_value=Unavailable()), \
             patch("pbradar.discovery.requests.head", side_effect=[Unavailable(), File()]):
            selected = resolve_osm_url(now=today)
        self.assertEqual(selected,
                         "https://download.geofabrik.de/europe/ukraine/ukraine-261119.osm.pbf")

    def test_redirect_is_rejected_without_losing_existing_data(self):
        class Redirect:
            status_code = 308
            headers = {"Location": "https://download.geofabrik.de/europe/ukraine/ukraine/"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "ukraine-latest.osm.pbf"
            data.write_bytes(b"previous")
            with patch("pbradar.discovery.requests.get", return_value=Redirect()) as get:
                with self.assertRaisesRegex(ValueError, "перенаправл"):
                    download_file("https://example.org/data", data)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            self.assertEqual(data.read_bytes(), b"previous")

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
