import tempfile
import unittest
import datetime as dt
from pathlib import Path

from pbradar.core import (classify, company_key, connect_db, export_csv, filter_contacts,
                          mark_site_checked, parse_ua_phones, read_contacts, save_contact,
                          set_phone_status, site_needs_check)
from pbradar.discovery import (RegionIndex, address_region, extract_business_contacts,
                               inside_polygon, name_region, polygon_contains)
from pbradar.launcher import safe_version


class CoreTests(unittest.TestCase):
    def test_search_matches_all_contact_fields_and_local_number_format(self):
        rows = [{"name": "АрхБюро Львів", "category": "Архітектори", "region": "Львівська",
                 "phone": "+380671234567", "status": "Опрацьовано",
                 "website": "https://design.example", "social": "https://instagram.com/design",
                 "role": "Керівник", "activity": "Публікація за 180 днів",
                 "checked_at": "2026-11-20", "source_url": "https://www.openstreetmap.org/node/7"},
                {"name": "Будівельна Фірма", "category": "Будівельні компанії",
                 "region": "Київська", "phone": "+380501234567", "status": "Не опрацьований"}]
        for term in ("архбюро", "ЛЬВІВСЬКА", "0671234567", "+380 6712", "архітектори",
                     "опрацьовано", "instagram.com", "design.example", "керівник",
                     "2026-11-20", "openstreetmap.org/node/7", "львівська 067"):
            with self.subTest(term=term):
                self.assertEqual(filter_contacts(rows, term), rows[:1])
        self.assertEqual(filter_contacts(rows, "неіснуюча компанія"), [])
        self.assertEqual(filter_contacts(rows, ""), rows)

    def test_ukrainian_numbers_only(self):
        text = "Тел.: +380 67 123 45 67; 0671234567; +48 221 234 567"
        self.assertEqual(parse_ua_phones(text), ["+380671234567"])

    def test_categories(self):
        self.assertIn("Архітектурні бюро", classify("Студія", {"office": "architect"}))
        self.assertIn("Будівельні компанії", classify("Компанія", {"office": "construction_company"}))
        self.assertNotIn("Відділи закупівель", classify("Компанія", {"office": "construction_company"}))

    def test_expanded_osm_business_tags_without_false_buildings(self):
        checks = (
            ({"company": "construction"}, "Будівельні компанії"),
            ({"office": "engineer"}, "Проєктні організації"),
            ({"craft": "window_construction"}, "Монтажні організації"),
            ({"craft": "metal_construction"}, "Металоконструкції"),
            ({"craft": "interior_designer"}, "Дизайнери інтер'єру"),
            ({"office": "property_developer"}, "Забудовники"),
        )
        for tags, category in checks:
            with self.subTest(tags=tags):
                self.assertIn(category, classify("ТОВ Фірма", tags))
        for tags in ({"landuse": "industrial"}, {"building": "warehouse"},
                     {"landuse": "construction"}, {"office": "company"}):
            with self.subTest(negative=tags):
                self.assertEqual(classify("Звичайна фірма", tags), set())
        self.assertNotIn("Підрядники приватного будівництва",
                         classify("Приватний дитячий садок", {}))

    def test_osm_phone_variants_social_and_capital_regions(self):
        phones, website, social = extract_business_contacts({
            "contact:mobile": "+380 67 123 45 67",
            "phone:2": "050 123 45 67",
            "contact:phone": "044 123 45 67",
            "contact:fax": "044 123 45 68",
            "website": "https://example.ua",
            "contact:instagram": "@build_studio",
        })
        self.assertEqual(set(phones), {"+380671234567", "+380501234567", "+380441234567"})
        self.assertEqual(website, "https://example.ua")
        self.assertEqual(social, "https://instagram.com/build_studio")
        self.assertEqual(address_region({"addr:city": "Львів"}), "Львівська")
        self.assertEqual(address_region({"addr:city": "Київ"}), "м. Київ")
        self.assertEqual(address_region({"addr:city": "Біла Церква"}), "Не визначено")

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

    def test_websites_are_recorded_once_and_checked_again_on_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "contacts.sqlite3") as db:
                url = "https://architecture.example"
                self.assertTrue(site_needs_check(db, url))
                mark_site_checked(db, url, 3)
                self.assertFalse(site_needs_check(db, url))
                week_later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
                self.assertTrue(site_needs_check(db, url, now=week_later))
                mark_site_checked(db, url, 0)
                two_days = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
                self.assertTrue(site_needs_check(db, url, now=two_days))

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
