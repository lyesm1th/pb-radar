import tempfile
import unittest
import datetime as dt
from pathlib import Path

from pbradar.core import (classify, classify_ownership, company_key, connect_db, export_csv,
                          copyable_contact_value, filter_contacts, filter_ownership,
                          group_contacts_by_company, visible_link,
                          mark_site_checked, parse_ua_phones,
                          read_column_widths, read_contacts, read_setting, save_contact,
                          set_company_ownership, set_phone_status, site_needs_check,
                          write_setting)
from pbradar.discovery import (RegionIndex, address_region, extract_business_contacts,
                               inside_polygon, name_region, polygon_contains)
from pbradar.launcher import safe_version


class CoreTests(unittest.TestCase):
    def test_saved_contacts_respect_selected_categories_and_regions_without_rescanning(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "contacts.sqlite3") as con:
                repeated_phone = "+380671234567"
                save_contact(con, "ТОВ Архітектурне бюро", "Архітектори", "Київська",
                             repeated_phone, "https://arch.example")
                save_contact(con, "ТОВ Генпідряд", "Генпідрядні організації", "Львівська",
                             repeated_phone, "https://general.example")
                save_contact(con, "ТОВ Будмонтаж", "Монтажні організації", "Львівська",
                             "+380501234567", "https://install.example")
                self.assertEqual(len(read_contacts(con)), 2)
                selected = read_contacts(con, categories={"Генпідрядні організації"},
                                         regions={"Львівська"})
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["name"], "ТОВ Генпідряд")
                self.assertEqual(selected[0]["phone"], repeated_phone)
                self.assertEqual(read_contacts(con, categories={"Архітектори"},
                                               regions={"Львівська"}), [])
                self.assertEqual(len(read_contacts(con, categories={"Монтажні організації"},
                                                   regions={"Львівська"})), 1)
                self.assertEqual(read_contacts(con, categories=set()), [])
                self.assertEqual(read_contacts(con, regions=set()), [])
                set_phone_status(con, repeated_phone, "Опрацьовано")
                self.assertEqual(read_contacts(con, ("Не опрацьований",),
                                               categories={"Генпідрядні організації"}), [])
                self.assertEqual(len(read_contacts(con, categories={"Архітектори"})), 1)

    def test_copyable_phone_and_visible_link_are_exact_cell_values(self):
        row = {"name": "ТОВ Будмонтаж", "phone": "+380671234567",
               "website": "https://budmontazh.ua", "social": "https://instagram.com/build",
               "source_url": "https://prozorro.gov.ua/tender/UA-2026-11-20-000001-a"}
        self.assertEqual(copyable_contact_value(row, "phone"), "+380671234567")
        self.assertEqual(copyable_contact_value(row, "website"), "https://budmontazh.ua")
        self.assertEqual(copyable_contact_value(row, "name"), "ТОВ Будмонтаж")
        self.assertEqual(copyable_contact_value({**row, "is_group": True}, "phone"), "")
        self.assertEqual(copyable_contact_value({**row, "is_group": True}, "website"),
                         "https://budmontazh.ua")
        self.assertEqual(visible_link({"website": "", "social": "https://instagram.com/build",
                                       "source_url": "https://prozorro.gov.ua/tender/x"}),
                         "https://instagram.com/build")
        self.assertEqual(visible_link({"source_url": "https://prozorro.gov.ua/tender/x"}),
                         "https://prozorro.gov.ua/tender/x")
        self.assertEqual(visible_link({}), "")

    def test_companies_group_phones_and_matching_edrpou_across_addresses(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "db.sqlite3") as con:
                save_contact(con, "ТОВ Альфа", "Будівельні компанії", "Львівська",
                             "+380671234567", edrpou="12345678")
                save_contact(con, "ТОВ Альфа", "Будівельні компанії", "Львівська",
                             "+380501234567", edrpou="12345678")
                save_contact(con, "ТОВ Альфа, відокремлений підрозділ", "Будівельні компанії",
                             "Київська", "+380931234567", edrpou="12345678")
                save_contact(con, "ТОВ Альфа", "Будівельні компанії", "Одеська",
                             "+380661234567", edrpou="99999999")
                groups = group_contacts_by_company(read_contacts(con))
                self.assertEqual(sorted(len(group) for group in groups), [1, 3])
                big_group = next(group for group in groups if len(group) == 3)
                self.assertEqual({row["region"] for row in big_group}, {"Львівська", "Київська"})
                self.assertEqual({row["edrpou"] for row in big_group}, {"12345678"})
                set_company_ownership(con, big_group[0]["company_id"], "Державна / комунальна")
                self.assertEqual({row["ownership"] for row in read_contacts(con)
                                  if row["edrpou"] == "12345678"}, {"Державна / комунальна"})
                # The manager's decision follows the legal entity into later scans.
                save_contact(con, "ТОВ Альфа інша філія", "Будівельні компанії", "Рівненська",
                             "+380991234567", edrpou="12345678", ownership="Приватна")
                self.assertEqual({row["ownership"] for row in read_contacts(con)
                                  if row["edrpou"] == "12345678"}, {"Державна / комунальна"})
                # Shared site does not justify combining different legal entities.
                save_contact(con, "ТОВ Спільний сайт 1", "Будівельні компанії", "Львівська",
                             "+380681234567", website="https://shared.example", edrpou="22222222")
                save_contact(con, "ТОВ Спільний сайт 2", "Будівельні компанії", "Львівська",
                             "+380971234567", website="https://shared.example", edrpou="33333333")
                by_phone = {r["phone"]: r for r in read_contacts(con)}
                self.assertEqual(by_phone["+380681234567"]["edrpou"], "22222222")
                self.assertEqual(by_phone["+380971234567"]["edrpou"], "33333333")

    def test_ownership_classification_is_conservative_and_manager_override_persists(self):
        self.assertEqual(classify_ownership("Обласна адміністрація", buyer_kind="authority"),
                         "Державна / комунальна")
        self.assertEqual(classify_ownership("КНП лікарня", buyer_kind="general"),
                         "Державна / комунальна")
        self.assertEqual(classify_ownership("Комунальне підприємство Світло"),
                         "Державна / комунальна")
        self.assertEqual(classify_ownership("ТОВ Будмонтаж"), "Приватна")
        self.assertEqual(classify_ownership("АТ Енерго", buyer_kind="special"), "Не визначено")
        self.assertEqual(classify_ownership("Бюро Світанок"), "Не визначено")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db.sqlite3"
            with connect_db(path) as con:
                save_contact(con, "ТОВ Будмонтаж", "Будівельні компанії", "Львівська",
                             "+380671234567")
                row = read_contacts(con)[0]
                self.assertEqual(row["ownership"], "Приватна")
                set_company_ownership(con, row["company_id"], "Державна / комунальна")
                save_contact(con, "ТОВ Будмонтаж", "Будівельні компанії", "Львівська",
                             "+380671234567")
                self.assertEqual(read_contacts(con)[0]["ownership"], "Державна / комунальна")
            with connect_db(path) as con:
                self.assertEqual(filter_ownership(read_contacts(con), "Без державних"), [])
                self.assertEqual(len(filter_ownership(read_contacts(con), "Державні")), 1)

    def test_prozorro_toggle_keeps_other_source_for_same_phone(self):
        with tempfile.TemporaryDirectory() as temp:
            with connect_db(Path(temp) / "db.sqlite3") as con:
                phone = "+380671234567"
                save_contact(con, "Відділ освіти", "Відділи закупівель", "Львівська", phone,
                             source_url="https://prozorro.gov.ua/tender/UA-2026-11-20-000001-a",
                             ownership="Державна / комунальна")
                save_contact(con, "ТОВ Будмонтаж", "Будівельні компанії", "Львівська", phone,
                             source_url="https://example.com/contacts")
                self.assertEqual(len(read_contacts(con)), 1)
                filtered = read_contacts(con, include_prozorro=False)
                self.assertEqual(len(filtered), 1)
                self.assertEqual(filtered[0]["name"], "ТОВ Будмонтаж")
                self.assertEqual(filtered[0]["ownership"], "Приватна")
                self.assertEqual(len(filter_ownership(filtered, "Тільки приватні")), 1)
                # A public buyer must not hide the same phone's private-company
                # association when that is the audience the user wants.
                self.assertEqual(len(filter_ownership(read_contacts(con), "Без державних")), 1)

    def test_table_widths_and_source_filters_persist_in_same_database(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db.sqlite3"
            with connect_db(path) as con:
                write_setting(con, "show_prozorro", "0")
                write_setting(con, "column_widths", '{"name": 320, "phone": 150, "bad": -6}')
            with connect_db(path) as con:
                self.assertEqual(read_setting(con, "show_prozorro"), "0")
                self.assertEqual(read_column_widths(con), {"name": 320, "phone": 150, "bad": 55})

    def test_old_database_migrates_ownership_without_losing_contacts(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db.sqlite3"
            with sqlite3.connect(path) as con:
                con.executescript("""
                    CREATE TABLE companies (
                        id INTEGER PRIMARY KEY, company_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                        category TEXT NOT NULL, region TEXT NOT NULL, website TEXT NOT NULL DEFAULT '',
                        social TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
                        checked_at TEXT NOT NULL DEFAULT '', activity TEXT NOT NULL DEFAULT 'Невідомо');
                    INSERT INTO companies(company_key,name,category,region)
                        VALUES('old1','ТОВ Архбюро','Архітектори','Львівська');
                    CREATE TABLE contacts (
                        id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, phone TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'Не визначено', status TEXT NOT NULL DEFAULT 'Не опрацьований',
                        found_at TEXT NOT NULL, checked_at TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
                        UNIQUE(company_id,phone));
                    INSERT INTO contacts(company_id,phone,status,found_at,checked_at)
                        VALUES(1,'+380671234567','Опрацьовано','2026-11-20','2026-11-20');
                """)
            with connect_db(path) as con:
                rows = read_contacts(con)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["ownership"], "Приватна")
                self.assertEqual(rows[0]["status"], "Опрацьовано")

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
