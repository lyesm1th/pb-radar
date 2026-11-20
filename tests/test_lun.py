"""LUN parser uses public developer profiles, never disallowed phone APIs."""

import tempfile
import unittest
from pathlib import Path

from pbradar.core import connect_db, read_contacts, save_contact
from pbradar.lun import lun_lviv_profiles, parse_lun_developer, parse_lun_sitemap


class LunTests(unittest.TestCase):
    def test_company_sitemap_only_allows_ukrainian_profiles_on_lun(self):
        xml = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://lun.ua/uk/viking-development</loc></url>
          <url><loc>https://lun.ua/uk/viking-development</loc></url>
          <url><loc>https://lun.ua/uk/secret?phone=true</loc></url>
          <url><loc>https://other.example/uk/brand</loc></url>
          <url><loc>https://lun.ua/uk/ріел</loc></url>
        </urlset>"""
        self.assertEqual(parse_lun_sitemap(xml),
                         ["https://lun.ua/uk/viking-development", "https://lun.ua/uk/ріел"])

    def test_developer_phone_comes_from_profile_and_website_from_visible_label(self):
        html = """
          <title>Забудовник VIKING Development, новобудови від забудовника</title>
          <h1 class="DeveloperPremium-name">VIKING Development</h1>
          <div id="developer-external">
            <a data-testid="contacts-redirect" href="https://lun.ua/v2/go?company_id=1310">
              <div class="UITwoLinerButton-content">viking-development.com.ua</div>
            </a>
            <a data-testid="contacts-phone" href="tel:+380671202220">+38 067 ... Показати</a>
            <a href="https://www.facebook.com/viking.development">Офіційний Facebook</a>
          </div>
          <a href="https://lun.ua/new/lviv/viking-park">ЖК Viking Park</a>
          <footer><a href="tel:+380501234567">Служба підтримки сайту</a></footer>
        """
        lead = parse_lun_developer(html, "https://lun.ua/uk/viking-development")
        self.assertIsNotNone(lead)
        self.assertEqual(lead.name, "VIKING Development")
        self.assertEqual(lead.phones, ("+380671202220",))
        self.assertEqual(lead.website, "https://viking-development.com.ua")
        self.assertEqual(lead.regions, frozenset({"Львівська"}))
        self.assertTrue(lead.social.startswith("https://www.facebook.com/"))

    def test_multi_region_developer_is_visible_under_both_regions(self):
        html = """
          <h1>РІЕЛ</h1>
          <div id="developer-external">
            <a href="tel:+380632004410" data-testid="contacts-phone">Показати</a>
          </div>
          <a href="https://lun.ua/new/lviv/project-one">Львів</a>
          <a href="https://lun.ua/new/kyiv/project-two">Київ</a>
        """
        lead = parse_lun_developer(html, "https://lun.ua/uk/ріел")
        self.assertEqual(lead.regions, frozenset({"Львівська", "м. Київ"}))
        with tempfile.TemporaryDirectory() as tmp:
            with connect_db(Path(tmp) / "db.sqlite3") as con:
                for region in lead.regions:
                    save_contact(con, lead.name, "Забудовники", region, lead.phones[0],
                                 website="https://riel.ua", source_url=lead.source_url)
                self.assertEqual(len(read_contacts(con)), 1)
                self.assertEqual(len(read_contacts(con, regions={"Львівська"})), 1)
                self.assertEqual(len(read_contacts(con, regions={"м. Київ"})), 1)

    def test_brovary_is_not_mistaken_for_kyiv_city(self):
        html = """
           <title>Забудовник БЗБК в м. Бровари, новобудови</title>
           <h1>БЗБК</h1>
           <a href="tel:+380443324318" data-testid="contacts-phone">Показати</a>
           <a href="https://lun.ua/new/kyiv/brovary-savanna-city">ЖК</a>
        """
        lead = parse_lun_developer(html, "https://lun.ua/uk/бздк")
        self.assertEqual(lead.regions, frozenset({"Київська"}))

    def test_lviv_catalog_only_yields_developer_profiles(self):
        html = """
          <a class="PremiumDevelopersCard" href="https://lun.ua/uk/ріел">РІЕЛ</a>
          <a class="PremiumDevelopersCard" href="https://lun.ua/uk/ріел">РІЕЛ</a>
          <a class="PremiumDevelopersCard" href="https://evil.example/uk/bad">BAD</a>
          <a href="https://lun.ua/new/lviv/project">Не забудовник</a>
        """
        self.assertEqual(lun_lviv_profiles(html), ["https://lun.ua/uk/ріел"])


if __name__ == "__main__":
    unittest.main()
