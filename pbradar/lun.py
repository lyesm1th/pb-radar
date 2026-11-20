"""LUN developer profiles: publicly rendered business telephones only.

LUN's robots.txt disallows /phone/, /api/ and generic query pagination.
We use its published company sitemap and ordinary public developer pages;
we never call those disallowed phone or tracking endpoints.
"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .catalogs import PoliteCatalogReader
from .core import REGIONS, connect_db, normalize_url, parse_ua_phones, save_contact
from .discovery import CAPITAL_CITIES

LUN_COMPANIES = "https://lun.ua/sitemaps/company_uk.xml"
LUN_LVIV_DEVELOPERS = "https://lun.ua/uk/забудовники-львова"

PROJECT_REGIONS = {
    "lviv": "Львівська", "kyiv": "м. Київ", "odesa": "Одеська",
    "dnipro": "Дніпропетровська", "kharkiv": "Харківська",
    "vinnytsia": "Вінницька", "vinnitsa": "Вінницька",
    "lutsk": "Волинська", "zhytomyr": "Житомирська",
    "uzhhorod": "Закарпатська", "uzhgorod": "Закарпатська",
    "zaporizhzhia": "Запорізька", "ivano-frankivsk": "Івано-Франківська",
    "kropyvnytskyi": "Кіровоградська", "mykolaiv": "Миколаївська",
    "poltava": "Полтавська", "rivne": "Рівненська", "sumy": "Сумська",
    "ternopil": "Тернопільська", "kherson": "Херсонська",
    "khmelnytskyi": "Хмельницька", "cherkasy": "Черкаська",
    "chernivtsi": "Чернівецька", "chernihiv": "Чернігівська",
    "donetsk": "Донецька", "luhansk": "Луганська",
}

# New-building pages around Kyiv may use /new/kyiv even when the actual
# location is a suburb in Kyiv oblast, not the city municipality.
KYIV_OBLAST_TOWNS = (
    "бровар", "ірпен", "буч", "вишнев", "борисп", "гостомел", "петропавлівськ",
    "софіївськ", "білогород", "біла церкв", "обухів", "brovary", "irpin",
)


@dataclass(frozen=True)
class LunDeveloper:
    name: str
    phones: tuple[str, ...]
    regions: frozenset[str]
    website: str
    social: str
    source_url: str


def parse_lun_sitemap(xml: str) -> list[str]:
    """Read the official company sitemap; discard all off-domain or query URLs."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("Некоректна карта профілів ЛУН") from exc
    urls = []
    seen = set()
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "loc" or not item.text:
            continue
        url = item.text.strip()
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "lun.ua" or not parsed.path.startswith("/uk/"):
            continue
        if parsed.query or parsed.fragment or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def lun_lviv_profiles(html: str) -> list[str]:
    """The first visible Lviv catalog page is public; query pagination is not."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for anchor in soup.select("a.PremiumDevelopersCard[href]"):
        url = anchor.get("href", "")
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname == "lun.ua" and parsed.path.startswith("/uk/"):
            if url not in urls:
                urls.append(url)
    return urls


def _developer_regions(soup: BeautifulSoup, url: str) -> frozenset[str]:
    regions = set()
    for anchor in soup.select("a[href]"):
        link = anchor.get("href", "")
        match = re.search(r"(?:https://lun\.ua)?/(?:uk/)?new/([a-z-]+)/", link)
        if match and (region := PROJECT_REGIONS.get(match.group(1))):
            regions.add(region)
    # Some region-specific company pages have no active project cards.
    suffix = unquote(urlparse(url).path).rstrip("/").split("/")[-1].casefold()
    if suffix in CAPITAL_CITIES:
        regions.add(CAPITAL_CITIES[suffix])
    # A city in the title is more precise than the map's /new/kyiv grouping.
    title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
    if "м. " in title and any(term in title for term in KYIV_OBLAST_TOWNS):
        regions.discard("м. Київ")
        regions.add("Київська")
    return frozenset(regions or {"Не визначено"})


def parse_lun_developer(html: str, url: str) -> LunDeveloper | None:
    """Read the developer's own tel: and visible official-domain field.

    A project sales number is not treated as the developer's central number;
    only the developer profile's `contacts-phone` is used here.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.DeveloperPremium-name") or soup.select_one("h1")
    if not heading:
        return None
    name = " ".join(heading.get_text(" ", strip=True).split())
    if not name:
        return None
    phones = []
    for anchor in soup.select("a[data-testid='contacts-phone'][href^='tel:']"):
        for phone in parse_ua_phones(anchor.get("href", "")[4:]):
            if phone not in phones:
                phones.append(phone)
    website = ""
    redirect_link = soup.select_one("a[data-testid='contacts-redirect'] .UITwoLinerButton-content")
    if redirect_link:
        visible = redirect_link.get_text(" ", strip=True)
        if re.fullmatch(r"[\w.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?", visible, re.UNICODE):
            candidate = normalize_url(visible)
            if urlparse(candidate).hostname not in {"lun.ua", "www.lun.ua"}:
                website = candidate
    social = ""
    for anchor in soup.select("#developer-external a[href]"):
        link = anchor.get("href", "")
        if urlparse(link).hostname in {"www.facebook.com", "facebook.com", "www.instagram.com", "instagram.com"}:
            social = link
            break
    return LunDeveloper(name, tuple(phones), _developer_regions(soup, url), website, social, url)


def scan_lun(regions: set[str], *, cancel: threading.Event | None = None,
             progress=None, refresh=None, reader: PoliteCatalogReader | None = None,
             db_path=None, max_profiles: int | None = None) -> dict[str, int]:
    """Process LUN's public company sitemap incrementally and resume from cursor."""
    regions = set(regions or REGIONS)
    catalog = reader or PoliteCatalogReader()
    result = {"profiles": 0, "with_phone": 0, "listed": 0, "new": 0,
              "off_region": 0, "failed": 0, "total": 0, "remaining": 0}
    profiles = []
    if "Львівська" in regions:
        response = catalog.get(LUN_LVIV_DEVELOPERS, cancel)
        if response:
            profiles = lun_lviv_profiles(str(response[1]))
    sitemap = catalog.get(LUN_COMPANIES, cancel, xml=True)
    if not sitemap:
        raise RuntimeError("Карта профілів ЛУН недоступна. Зачекай і повтори пошук.")
    urls = parse_lun_sitemap(sitemap[1])
    if not urls:
        raise RuntimeError("ЛУН не повернув профілі забудовників у карті сайту")
    result["total"] = len(urls)
    with connect_db(db_path) if db_path is not None else connect_db() as con:
        row = con.execute("SELECT offset FROM source_cursors WHERE source='lun:company_uk'").fetchone()
        start = min(max(0, int(row["offset"])) if row and row["offset"].isdigit() else 0, len(urls) - 1)
        # For a Lviv-only run, discover the known local developer profiles first.
        # These can be collected immediately without waiting for the sitemap's
        # countrywide ordering (Kyiv profiles commonly appear first).
        for url in profiles:
            if cancel and cancel.is_set():
                raise InterruptedError("Пошук зупинено")
            response = catalog.get(url, cancel)
            if not response:
                result["failed"] += 1
                continue
            lead = parse_lun_developer(str(response[1]), url)
            if lead:
                _store_developer(con, lead, regions, result)
            con.commit()
        if profiles and refresh:
            refresh()
        remaining_limit = min(max_profiles if max_profiles is not None else len(urls), len(urls) - start)
        for index in range(start, start + remaining_limit):
            if cancel and cancel.is_set():
                raise InterruptedError("Пошук зупинено")
            url = urls[index]
            response = catalog.get(url, cancel)
            result["profiles"] += 1
            if response:
                lead = parse_lun_developer(str(response[1]), url)
                if lead:
                    _store_developer(con, lead, regions, result)
            else:
                result["failed"] += 1
            con.execute("""INSERT INTO source_cursors(source,offset,updated_at)
                           VALUES('lun:company_uk',?,CURRENT_TIMESTAMP)
                           ON CONFLICT(source) DO UPDATE SET offset=excluded.offset,
                           updated_at=excluded.updated_at""", (str(index + 1 if index + 1 < len(urls) else 0),))
            con.commit()
            result["remaining"] = len(urls) - (index + 1)
            if refresh and result["profiles"] % 10 == 0:
                refresh()
            if progress and (result["profiles"] % 3 == 0 or index == start):
                progress(f"ЛУН · профілі {index+1:,}/{len(urls):,} · залишилось {result['remaining']:,} "
                         f"· з телефонами {result['with_phone']:,} · нових {result['new']:,}")
    if refresh:
        refresh()
    return result


def _store_developer(con, lead: LunDeveloper, selected: set[str], result: dict[str, int]) -> None:
    if not lead.phones:
        return
    selected_regions = lead.regions & selected
    if not selected_regions:
        if lead.regions == frozenset({"Не визначено"}) and len(selected) == len(REGIONS):
            selected_regions = {"Не визначено"}
        else:
            result["off_region"] += 1
            return
    result["with_phone"] += 1
    for region in sorted(selected_regions):
        for phone in lead.phones:
            result["new"] += save_contact(
                con, lead.name, "Забудовники", region, phone,
                website=lead.website, social=lead.social, source_url=lead.source_url,
                role="Телефон девелопера · ЛУН", activity="ЛУН · чинність номера не перевірено")
            result["listed"] += 1
