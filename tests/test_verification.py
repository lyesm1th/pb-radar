import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from pbradar.core import connect_db, read_contacts, save_contact, set_phone_status
import pbradar.verification as verify

class DummyReader:
    def get(self, url):
        return url, BeautifulSoup(
            "<html><head><title>Лев Девелопмент</title></head><body>"
            "<p>Відділ продажу <a href='tel:+380671234567'>+380671234567</a></p>"
            "</body></html>", "html.parser")

class VerificationTests(unittest.TestCase):
    def test_identity_and_role(self):
        soup = BeautifulSoup("<p>Сервіс <a href='tel:+380671234567'>дзвінок</a></p>", "html.parser")
        self.assertEqual(verify.extract_roles(soup)["+380671234567"], "Сервіс")
        site = verify.SiteEvidence(True, {"+380671234567": "https://firm.test/contacts"},
                                   heading="Лев Девелопмент")
        row = dict(name="ТОВ Лев Девелопмент", phone="+380671234567",
                   edrpou="", website="https://firm.test", status="Не опрацьований")
        answer = verify.judge(row, site)
        self.assertEqual(answer["verification"], "Є на сайті фірми")
        self.assertEqual(verify.call_result("Не опрацьований"), "Не перевірено дзвінком")
        self.assertIn("діяльність не доведена", answer["company_state"])
        self.assertEqual(verify.judge(row, site, 2)["verification"],
                         "Спільний номер · потрібна звірка")

    def test_directory_is_not_company_website(self):
        for site in ("https://lun.ua/uk/company", "https://list.in.ua/company",
                     "https://prozorro.gov.ua/tender/123"):
            self.assertEqual(verify.official_site(site), "")

    def test_database_keeps_manager_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbfile = Path(tmp) / "contacts.db"
            with connect_db(dbfile) as db:
                save_contact(db, "ТОВ Лев Девелопмент", "Забудовники", "Львівська",
                             "+380671234567", "https://firm.test",
                             "https://lun.ua/uk/firm")
                set_phone_status(db, "+380671234567", "Не відповідає")
            with patch.object(verify, "connect_db", lambda: connect_db(dbfile)):
                r = verify.verify_saved(reader=DummyReader())
                self.assertEqual(r["checked"], 1)
                self.assertEqual(r["confirmed"], 1)
                second = verify.verify_saved(reader=DummyReader())
                self.assertEqual(second["skipped"], 1)
            with connect_db(dbfile) as db:
                row = read_contacts(db)[0]
                self.assertEqual(row["status"], "Не відповідає")
                self.assertEqual(row["verification"], "Є на сайті фірми")
                self.assertIn("Не відповідає", row["line_state"])
                self.assertTrue(row["verified_at"])

if __name__ == "__main__":
    unittest.main()
