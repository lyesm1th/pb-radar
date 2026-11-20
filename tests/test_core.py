import tempfile
import unittest
from pathlib import Path

from pbradar.core import (classify, company_key, connect_db, export_csv,
                          parse_ua_phones, read_contacts, save_contact, set_phone_status)
from pbradar.discovery import RegionIndex, inside_polygon, name_region, polygon_contains
from pbradar.launcher import safe_version


class CoreTests(unittest.TestCase):
    def test_ukrainian_numbers_only(self):
        text = "Тел.: +380 67 123 45 67; 0671234567; +48 221 234 567"
        self.assertEqual(parse_ua_phones(text), ["+380671234567"])

    def test_categories(self):
        self.assertIn("Архітектурні бюро", classify("Студія", {"office": "architect"}))
        self.assertIn("Будівельні компанії", classify("Компанія", {"office": "construction_company"}))
        self.assertNotIn("Відділи закупівель", classify("Компанія", {"office": "construction_company"}))

    def test_company_url_key(self):
        self.assertEqual(company_key("Студія", "https://www.sample.ua/contacts", "Львівська"),
                         "web:sample.ua")
        self.assertNotEqual(company_key("Студія A", "https://facebook.com/studio_a", "Львівська"),
                            company_key("Студія Б", "https://facebook.com/studio_b", "Львівська"))

    def test_database_preserves_status(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "contacts.sqlite3") as db:
                inserted = save_contact(db, "Студія", "Архітектори", "Львівська",
                                        "+380671234567", "https://sample.ua")
                self.assertTrue(inserted)
                db.execute("UPDATE contacts SET status='Не контактувати'")
                repeated = save_contact(db, "Студія", "Архітектори", "Львівська",
                                        "+380671234567", "https://sample.ua")
                self.assertFalse(repeated)
                rows = read_contacts(db)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "Не контактувати")

    def test_reviewed_number_hidden_after_second_search_and_new_company(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "contacts.sqlite3"
            phone = "+380671234567"
            with connect_db(db_path) as db:
                self.assertTrue(save_contact(db, "Бюро A", "Архітектори", "Львівська",
                                             phone, "https://one.example"))
                self.assertEqual(set_phone_status(db, phone, "Опрацьовано"), 1)
                # The scanner sees the same phone again next week.
                self.assertFalse(save_contact(db, "Бюро A", "Архітектори", "Львівська",
                                              phone, "https://one.example"))
                # It may also appear on another company's page.
                self.assertFalse(save_contact(db, "Підрядник Б", "Будівельні компанії",
                                              "Київська", phone, "https://two.example"))
                self.assertEqual(read_contacts(db, ("Не опрацьований",)), [])
                self.assertEqual(len(read_contacts(db)), 1)
                self.assertEqual(read_contacts(db)[0]["status"], "Опрацьовано")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM contacts WHERE status='Опрацьовано'").fetchone()[0], 2)
            # Reopen simulates closing the app or installing a new version.
            with connect_db(db_path) as db:
                self.assertEqual(len(read_contacts(db)), 1)
                self.assertEqual(read_contacts(db)[0]["status"], "Опрацьовано")
                set_phone_status(db, phone, "Не опрацьований")
                self.assertEqual(len(read_contacts(db, ("Не опрацьований",))), 1)

    def test_csv_preserves_status_as_text(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "db.sqlite3") as db:
                save_contact(db, "=SUM(1+1)", "Архітектори", "Львівська", "+380671234567")
                set_phone_status(db, "+380671234567", "Не контактувати")
                path = Path(temp) / "export.csv"
                export_csv(path, read_contacts(db))
                data = path.read_text(encoding="utf-8-sig")
                self.assertIn("'=SUM(1+1)", data)
                self.assertIn("Не контактувати", data)

    def test_geojson_polygon_and_hole(self):
        square = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
        hole = [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]
        self.assertTrue(inside_polygon(3, 3, square))
        self.assertFalse(polygon_contains(1.5, 1.5, [square, hole]))

    def test_region_index(self):
        data = {"features": [{"properties": {"shapeName": "Lvivska Oblast"},
                              "geometry": {"type": "Polygon", "coordinates": [
                                  [[20, 48], [26, 48], [26, 51], [20, 51], [20, 48]]
                              ]}}]}
        self.assertEqual(RegionIndex(data).at(24, 49), "Львівська")

    def test_release_tag_validation(self):
        self.assertEqual(safe_version("v0.1.0"), "v0.1.0")
        for danger in ("../other", "v0.1/../../", "latest", "v1.2-beta"):
            with self.assertRaises(ValueError):
                safe_version(danger)


if __name__ == "__main__":
    unittest.main()
