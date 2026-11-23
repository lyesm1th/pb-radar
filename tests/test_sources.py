import tempfile
import unittest
from pathlib import Path
from pbradar.core import connect_db, read_contacts, save_contact, filter_contacts
from pbradar.sources import SOURCE_STYLES, group_source_label, source_label

class SourceColoursTests(unittest.TestCase):
    def test_domains(self):
        cases = {
            "https://lun.ua/uk/developer": "ЛУН",
            "https://list.in.ua/Львів/Архітектори": "List.in.ua",
            "https://novobudovy.com/kompanii-lvova": "NOVOBUDOVY",
            "https://prozorro.gov.ua/tender/abc": "Prozorro",
            "https://www.openstreetmap.org/node/123": "OSM",
            "https://example.com/contacts": "Сайт",
            "": "Без джерела",
            "https://notprozorro.gov.ua.example.com": "Сайт",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(source_label(url), expected)

    def test_groups(self):
        self.assertEqual(group_source_label([{"source_label": "ЛУН"}, {"source_label": "OSM"}]),
                         "Кілька джерел")
        self.assertEqual(group_source_label([{"source_url": "https://lun.ua/uk/foo"}]), "ЛУН")
        self.assertTrue({"ЛУН", "OSM", "List.in.ua", "Сайт"} <= set(SOURCE_STYLES))

    def test_no_migration_required_and_source_searchable(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "contacts.sqlite3") as con:
                save_contact(con, "ТОВ Архітектура", "Архітектори", "Львівська",
                             "+380671234567", "https://firm.example",
                             "https://lun.ua/uk/firm")
                rows = read_contacts(con)
                self.assertEqual(rows[0]["source_label"], "ЛУН")
                self.assertEqual(len(filter_contacts(rows, "ЛУН")), 1)

if __name__ == "__main__":
    unittest.main()
