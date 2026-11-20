"""Offline regression tests: exact phone-card association and scan continuation."""

import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from pbradar.catalogs import (CatalogLead, NOVO_ORIGIN, list_url, page_count,
                              parse_list_page, parse_novobudovy_page, scan_catalogs)
from pbradar.core import connect_db, read_contacts, save_contact, set_phone_status


class CatalogTests(unittest.TestCase):
    def test_list_phones_belong_to_cards_not_sidebar_and_generic_ids_do_not_merge(self):
        html = """
        <aside><a href="tel:+380991111111">Довідкова рекламодавця</a></aside>
        <div id="itemsList">
          <div class="item-search js-load-business-container" data-item="101">
            <h2 class="item-search__title"><a href="/101/build">Забудовник</a></h2>
            <a href="tel:+380671234567">067 123 45 67</a>
            <a href="tel:+380671234567">Подзвонити</a>
          </div>
          <div class="item-search js-load-business-container" data-item="102">
            <h2 class="item-search__title"><a href="/102/build">Забудовник</a></h2>
            <a href="tel:+380501234567">050 123 45 67</a>
          </div>
        </div>
        <a href="/Львів/Забудовники/page/2">2</a>
        <a href="/Львів/Забудовники/page/7">7</a>
        """
        url = list_url("Львів", "Забудовники")
        found = parse_list_page(html, url, "Львівська", "Забудовники")
        self.assertEqual(len(found), 2)
        self.assertEqual({lead.name for lead in found}, {"Забудовник · ID 101", "Забудовник · ID 102"})
        self.assertEqual({lead.phone for lead in found}, {"+380671234567", "+380501234567"})
        self.assertEqual(page_count(html, url, list_directory=True), 7)

    def test_novobudovy_only_own_organization_phones(self):
        html = """
        <footer>Телефон адміністратора: 067 999 88 77</footer>
        <div class="object_favour"><div class="comp">
          <h3><a href="/kompanii-lvova/lev">LEV Development</a></h3>
          <span itemprop="telephone">(067) 123 45 67, (050) 123 45 67</span>
        </div></div>
        <div class="object_favour"><div class="comp">
          <h3><a href="/kompanii-lvova/mono">Mono</a></h3>
          <span itemprop="telephone">(063) 123 45 67</span>
        </div></div>
        """
        found = parse_novobudovy_page(html, NOVO_ORIGIN + "/kompanii-lvova")
        self.assertEqual({lead.phone for lead in found},
                         {"+380671234567", "+380501234567", "+380631234567"})
        self.assertEqual({lead.name for lead in found}, {"LEV Development", "Mono"})

    def test_directory_pagination_treats_curly_and_straight_apostrophes_equally(self):
        url = list_url("Львів", "Дизайнери-інтер’єру")
        html = """<a href="/Львів/Дизайнери-інтер'єру/page/2">2</a>
                  <a href="/Львів/Дизайнери-інтер'єру/page/20">20</a>"""
        self.assertEqual(page_count(html, url, list_directory=True), 20)

    def test_same_company_multiple_categories_stay_searchable_and_status_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            with connect_db(Path(tmp) / "c.sqlite3") as con:
                phone = "+380671234567"
                save_contact(con, "ТОВ Девелопер", "Забудовники", "Львівська", phone)
                set_phone_status(con, phone, "Опрацьовано")
                save_contact(con, "ТОВ Девелопер", "Будівельні компанії", "Львівська", phone)
                self.assertEqual(len(read_contacts(con)), 1)
                a = read_contacts(con, categories={"Забудовники"})
                b = read_contacts(con, categories={"Будівельні компанії"})
                self.assertEqual(len(a), 1)
                self.assertEqual(len(b), 1)
                self.assertEqual(a[0]["status"], "Опрацьовано")
                self.assertEqual(b[0]["status"], "Опрацьовано")
                self.assertIn("Забудовники", b[0]["category"])

    def test_interrupted_pagination_resumes_without_losing_numbers(self):
        root = NOVO_ORIGIN + "/kompanii-lvova"

        def page(n):
            return (f'<a href="/kompanii-lvova/2">2</a>'
                    f'<a href="/kompanii-lvova/3">3</a>'
                    f'<div class="object_favour"><div class="comp">'
                    f'<h3><a href="/kompanii-lvova/company{n}">Company {n}</a></h3>'
                    f'<span itemprop="telephone">+38067123456{n}</span>'
                    f'</div></div>')

        class FakeReader:
            def __init__(self, fail_page):
                self.fail_page = fail_page

            def get(self, url, cancel=None):
                number = int(url.rsplit("/", 1)[1]) if url.rsplit("/", 1)[1].isdigit() else 1
                if number == self.fail_page:
                    return None
                return url, BeautifulSoup(page(number), "html.parser")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite3"
            first = scan_catalogs({"Забудовники"}, {"Львівська"}, db_path=path,
                                  reader=FakeReader(fail_page=2), max_pages_per_topic=3)
            self.assertEqual(first["new"], 1)
            with connect_db(path) as con:
                cursor = con.execute("SELECT offset FROM source_cursors WHERE source LIKE 'catalog:novo:%'").fetchone()
                self.assertEqual(cursor["offset"], "2")
            second = scan_catalogs({"Забудовники"}, {"Львівська"}, db_path=path,
                                   reader=FakeReader(fail_page=None), max_pages_per_topic=3)
            self.assertEqual(second["new"], 2)
            with connect_db(path) as con:
                self.assertEqual(len(read_contacts(con)), 3)
                cursor = con.execute("SELECT offset FROM source_cursors WHERE source LIKE 'catalog:novo:%'").fetchone()
                self.assertEqual(cursor["offset"], "1")


if __name__ == "__main__":
    unittest.main()
