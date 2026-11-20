import tempfile
import unittest
from pathlib import Path

from pbradar.discovery import RegionIndex, find_osm_leads

OSM_FIXTURE = '''<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="101" lat="49.84" lon="24.03" version="1">
    <tag k="name" v="Львівська архітектурна студія"/>
    <tag k="office" v="architect"/>
    <tag k="phone" v="+380671234567"/>
    <tag k="website" v="https://example.org"/>
  </node>
  <node id="102" lat="49.84" lon="24.03" version="1">
    <tag k="name" v="Кав'ярня"/>
    <tag k="phone" v="+380671234567"/>
  </node>
  <way id="103" version="1">
    <nd ref="101"/><nd ref="102"/>
    <tag k="name" v="Будівельна компанія"/>
    <tag k="office" v="construction_company"/>
    <tag k="addr:region" v="Львівська область"/>
    <tag k="phone" v="+380671234568"/>
  </way>
</osm>'''


class DiscoveryTests(unittest.TestCase):
    def test_osm_nodes_and_ways(self):
        geo = RegionIndex({"features": [{
            "properties": {"shapeName": "Lvivska Oblast"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[20, 48], [26, 48], [26, 51], [20, 51], [20, 48]]
            ]}
        }]})
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "test.osm"
            file.write_text(OSM_FIXTURE, encoding="utf-8")
            contacts = find_osm_leads(file, {"Архітектурні бюро", "Будівельні компанії"},
                                      {"Львівська"}, geo)
            self.assertEqual(len(contacts), 2)
            self.assertEqual({lead["region"] for lead in contacts}, {"Львівська"})
            self.assertIn("+380671234567", contacts[0]["phones"])
            self.assertTrue(contacts[0]["source_url"].startswith("https://www.openstreetmap.org/node/"))


if __name__ == "__main__":
    unittest.main()
