import datetime as dt
import tempfile
import unittest
from pathlib import Path

from pbradar.core import (
    connect_db, read_contacts, read_work_contacts, save_contact,
    save_work_result, set_phone_status, set_work_selected,
)


class WorkQueueTests(unittest.TestCase):
    def test_move_one_number_without_changing_status_or_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite3"
            with connect_db(path) as con:
                save_contact(con, "ТОВ Будова", "Забудовники", "Львівська",
                             "+380671234567", "https://firm.example")
                self.assertEqual(len(read_work_contacts(con)), 0)
                set_work_selected(con, "+380671234567", True)
                rows = read_work_contacts(con)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "Не опрацьований")
                self.assertTrue(rows[0]["in_work"])
                self.assertEqual(len(read_contacts(con)), 1)
                set_work_selected(con, "+380671234567", False)
                self.assertEqual(read_work_contacts(con), [])
                self.assertEqual(len(read_contacts(con)), 1)

    def test_result_survives_repeated_scraping_and_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite3"
            with connect_db(path) as con:
                phone = "+380671234567"
                save_contact(con, "ТОВ Будова", "Забудовники", "Львівська", phone)
                set_work_selected(con, phone, True)
                save_work_result(con, phone, "Потенційний партнер",
                                 "Надіслати КП, передзвонити у п'ятницю", "2026-11-20")
                save_contact(con, "ТОВ Будова", "Забудовники", "Львівська", phone,
                             source_url="https://lun.ua/uk/developer")
                saved = read_work_contacts(con)[0]
                self.assertEqual(saved["contact_result"], "Надіслати КП, передзвонити у п'ятницю")
                self.assertEqual(saved["last_contact_date"], "2026-11-20")
                self.assertEqual(saved["status"], "Потенційний партнер")
                set_work_selected(con, phone, False)
                set_work_selected(con, phone, True)
                self.assertEqual(read_work_contacts(con)[0]["contact_result"],
                                 "Надіслати КП, передзвонити у п'ятницю")

    def test_deduplicates_same_phone_in_different_companies(self):
        with tempfile.TemporaryDirectory() as tmp:
            with connect_db(Path(tmp) / "db.sqlite3") as con:
                for name in ("ТОВ Будова", "ТОВ Інша"):
                    save_contact(con, name, "Будівельні компанії", "Львівська",
                                 "+380671234567")
                set_work_selected(con, "+380671234567", True)
                self.assertEqual(len(read_work_contacts(con)), 1)
                save_work_result(con, "+380671234567", "Зателефонували", "Потребує уточнення")
                self.assertEqual({r["status"] for r in con.execute(
                    "SELECT status FROM contacts")}, {"Зателефонували"})

    def test_legacy_checked_contacts_backfill_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite3"
            with connect_db(path) as con:
                save_contact(con, "ТОВ Будова", "Забудовники", "Львівська", "+380671234567")
                set_phone_status(con, "+380671234567", "Опрацьовано")
                con.execute("DELETE FROM app_settings WHERE key='work_queue_backfilled'")
            with connect_db(path) as con:
                self.assertEqual(len(read_work_contacts(con)), 1)
                set_work_selected(con, "+380671234567", False)
            with connect_db(path) as con:
                self.assertEqual(read_work_contacts(con), [])

    def test_invalid_status_or_date_does_not_write_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with connect_db(Path(tmp) / "db.sqlite3") as con:
                save_contact(con, "ТОВ Будова", "Забудовники", "Львівська", "+380671234567")
                set_work_selected(con, "+380671234567", True)
                with self.assertRaises(ValueError):
                    save_work_result(con, "+380671234567", "Вигаданий статус", "Примітка")
                with self.assertRaises(ValueError):
                    save_work_result(con, "+380671234567", "Опрацьовано", "Примітка", "3000-01-01")
                row = read_work_contacts(con)[0]
                self.assertEqual(row["contact_result"], "")


if __name__ == "__main__":
    unittest.main()
