"""Public business-directory leads, with source links and conservative crawling.

These directories provide actual business telephone listings; unlike OSM,
their map-object count is not mistaken for a company/contact count.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .core import CATEGORIES, REGIONS, connect_db, parse_ua_phones, save_contact
from .discovery import USER_AGENT

LIST_ORIGIN = "https://list.in.ua"
NOVO_ORIGIN = "https://novobudovy.com"

# List.in.ua has city-based directories, not a universal oblast directory.
# A regional search covers the oblast capital, not every town in the oblast.
CITIES = {
    "Вінницька": "Вінниця", "Волинська": "Луцьк",
    "Дніпропетровська": "Дніпро", "Донецька": "Донецьк",
    "Житомирська": "Житомир", "Закарпатська": "Ужгород",
    "Запорізька": "Запоріжжя", "Івано-Франківська": "Івано-Франківськ",
    "Київська": "Біла Церква", "Кіровоградська": "Кропивницький",
    "Луганська": "Луганськ", "Львівська": "Львів",
    "Миколаївська": "Миколаїв", "Одеська": "Одеса",
    "Полтавська": "Полтава", "Рівненська": "Рівне",
    "Сумська": "Суми", "Тернопільська": "Тернопіль",
    "Харківська": "Харків", "Херсонська": "Херсон",
    "Хмельницька": "Хмельницький", "Черкаська": "Черкаси",
    "Чернівецька": "Чернівці", "Чернігівська": "Чернігів",
    "м. Київ": "Київ", "АР Крим": "Сімферополь", "м. Севастополь": "Севастополь",
}

# Directory paths are queries, not ground-truth verification of each firm's
# specialty. Non-existent category pages are rejected (redirect/home page).
LIST_TOPICS = {
    "Архітектори": "Архітектори",
    "Архітектурні бюро": "Архітектурні-бюро",
    "Дизайнери інтер'єру": "Дизайнери-інтер’єру",
    "Дизайнери екстер'єру": "Дизайнери-екстер’єру",
    "Ландшафтні дизайнери": "Ландшафтні-дизайнери",
    "Забудовники": "Забудовники",
    "Генпідрядні організації": "Генпідрядники",
    "Будівельні компанії": "Будівельні-компанії",
    "Виконроби": "Виконроби",
    "Проєктні організації": "Проектні-організації",
    "Монтажні організації": "Монтажні-організації",
    "Підрядники приватного будівництва": "Будівельні-фірми",
    "Промислове будівництво": "Промислове-будівництво",
    "Будівництво складів": "Будівництво-складів",
    "Металоконструкції": "Металоконструкції",
    "Протипожежні системи": "Протипожежні-системи",
}

GENERIC_NAMES = frozenset({
    "забудовник", "архітектор", "дизайнер", "дизайнер інтер'єрів",
    "будівельна компанія", "будівельні компанії", "генпідрядник",
    "проектна організація", "проєктна організація", "виконроб",
})


@dataclass(frozen=True)
class CatalogLead:
    name: str
    category: str
    region: str
    phone: str
    source_url: str


def list_url(city: str, topic: str, page: int = 1) -> str:
    path = f"/{quote(city, safe='')}/{quote(topic, safe='')}"
    return LIST_ORIGIN + path + (f"/page/{page}" if page > 1 else "")


def parse_list_page(html: str, url: str, region: str, category: str) -> list[CatalogLead]:
    """Read only a business card's phone; never scrape a page-wide phone blob."""
    soup = BeautifulSoup(html, "html.parser")
    leads = []
    for card in soup.select(".item-search.js-load-business-container"):
        title = card.select_one("h2.item-search__title a")
        if not title:
            continue
        name = " ".join(title.get_text(" ", strip=True).split())
        profile = urljoin(url, (title.get("href") or "").strip())
        if not name or urlparse(profile).hostname != "list.in.ua":
            continue
        # Anonymous category-only titles refer to different business IDs.
        # Distinguish them instead of merging unrelated companies by name.
        if name.casefold().replace("’", "'") in GENERIC_NAMES and card.get("data-item"):
            name += " · ID " + card["data-item"]
        phones = set()
        for anchor in card.select("a[href^='tel:']"):
            for phone in parse_ua_phones(anchor.get("href", "")[4:]):
                if phone not in phones:
                    leads.append(CatalogLead(name, category, region, phone, profile))
                    phones.add(phone)
    return leads


def parse_novobudovy_page(html: str, url: str) -> list[CatalogLead]:
    """Extract developer name and its own phone from each schema.org card."""
    soup = BeautifulSoup(html, "html.parser")
    leads = []
    for card in soup.select(".object_favour"):
        title = card.select_one(".comp h3 a")
        if not title:
            continue
        name = " ".join(title.get_text(" ", strip=True).split())
        profile = urljoin(url, title.get("href", ""))
        if not name or urlparse(profile).hostname != "novobudovy.com":
            continue
        phones = set()
        for field in card.select("[itemprop='telephone']"):
            phones.update(parse_ua_phones(field.get_text(" ", strip=True)))
        for number in sorted(phones):
            leads.append(CatalogLead(name, "Забудовники", "Львівська", number, profile))
    return leads


def page_count(html: str, url: str, *, list_directory: bool, cap: int = 500) -> int:
    """Inspect public pagination links; bound requests in case of bad markup."""
    soup = BeautifulSoup(html, "html.parser")
    base = unquote(urlparse(url).path).replace("’", "'").rstrip("/")
    prefix = base + ("/page/" if list_directory else "/")
    maximum = 1
    for anchor in soup.select("a[href]"):
        target = urljoin(url, anchor.get("href", "").strip())
        parsed = urlparse(target)
        if parsed.netloc != urlparse(url).netloc:
            continue
        path = unquote(parsed.path).replace("’", "'")
        if path.startswith(prefix) and (tail := path[len(prefix):]).isdigit():
            maximum = max(maximum, int(tail))
    return min(maximum, cap)


class PoliteCatalogReader:
    """Respect robots and wait between requests to the same directory host."""

    def __init__(self, delay: float = 1.6):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = delay
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        # Catalog URLs are built internally; user-supplied URLs are never
        # fetched here. Restrict both redirects and robots to known sources.
        if host not in {"list.in.ua", "novobudovy.com", "lun.ua"}:
            return False
        if host not in self.robots:
            try:
                resp = self.session.get(f"https://{host}/robots.txt", timeout=(5, 14),
                                        allow_redirects=False)
                if resp.status_code == 404:
                    self.robots[host] = None
                elif resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    self.robots[host] = parser
                else:
                    return False
            except requests.RequestException:
                return False
        policy = self.robots.get(host)
        return policy is None or policy.can_fetch(USER_AGENT, url)

    def get(self, url: str, cancel: threading.Event | None = None, *, xml: bool = False):
        host = urlparse(url).hostname or ""
        gap = self.delay - (time.monotonic() - self.last_request.get(host, -1e9))
        if gap > 0:
            if cancel and cancel.wait(gap):
                raise InterruptedError("Пошук зупинено")
            if not cancel:
                time.sleep(gap)
        if cancel and cancel.is_set():
            raise InterruptedError("Пошук зупинено")
        self.last_request[host] = time.monotonic()
        if urlparse(url).scheme != "https" or not self._allowed(url):
            return None
        try:
            response = self.session.get(url, timeout=(7, 20), allow_redirects=False,
                                        stream=True, headers={"Accept": "application/xml" if xml else "text/html"})
            with response:
                if response.status_code != 200:
                    return None
                media_type = response.headers.get("Content-Type", "").lower()
                if not any(t in media_type for t in (("text/xml", "application/xml") if xml else ("text/html",))):
                    return None
                data = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    data.extend(chunk)
                    if len(data) > 2_500_000:
                        return None
                response._content = bytes(data)
                response.encoding = response.apparent_encoding or response.encoding
                return url, response.text if xml else BeautifulSoup(response.text, "html.parser")
        except requests.RequestException:
            return None


def scan_catalogs(categories: set[str], regions: set[str], *,
                  cancel: threading.Event | None = None, progress=None, refresh=None,
                  reader: PoliteCatalogReader | None = None,
                  max_pages_per_topic: int = 30, db_path=None) -> dict[str, int]:
    """Scan independently accessible categories, persist per page, preserve statuses.

    The scanner is resumable: if stopped after 10 of 30 pages in a directory,
    the next run starts with page 11. At the end it wraps to page 1 so a later
    scan can detect changed public information.
    """
    catalog = reader or PoliteCatalogReader()
    regions = set(regions or REGIONS)
    tasks = []
    if "Забудовники" in categories and "Львівська" in regions:
        tasks.append(("novo", "Львівська", "Забудовники", NOVO_ORIGIN + "/kompanii-lvova"))
    # Put high-value construction categories and Lviv first. A nationwide run
    # can take a long time; useful leads should appear before its final page.
    priority_categories = ("Забудовники", "Будівельні компанії", "Генпідрядні організації",
                           "Проєктні організації", "Архітектори", "Архітектурні бюро")
    categories_order = priority_categories + tuple(c for c in CATEGORIES if c not in priority_categories)
    regions_order = (("Львівська",) + tuple(r for r in REGIONS if r != "Львівська")
                     if "Львівська" in regions else REGIONS)
    for region in regions_order:
        if region not in regions:
            continue
        for category in categories_order:
            if category in categories and category in LIST_TOPICS:
                tasks.append(("list", region, category,
                              list_url(CITIES[region], LIST_TOPICS[category])))
    result = {"groups": 0, "pages": 0, "listed": 0, "new": 0, "unavailable": 0, "failed": 0}
    if not tasks:
        return result
    with connect_db(db_path) if db_path is not None else connect_db() as con:
        for task_idx, (kind, region, category, url) in enumerate(tasks, 1):
            if cancel and cancel.is_set():
                raise InterruptedError("Пошук зупинено")
            key = f"catalog:{kind}:{region}:{category}"
            row = con.execute("SELECT offset FROM source_cursors WHERE source=?", (key,)).fetchone()
            start_page = max(1, int(row["offset"])) if row and row["offset"].isdigit() else 1
            if progress:
                progress(f"Каталоги {task_idx}/{len(tasks)} · {region} · {category} · починаю зі стор. {start_page} · нових {result['new']}")
            # A deep scan uses page 1 to discover current page count; that page
            # is only saved again when the cursor wraps around.
            first = catalog.get(url, cancel)
            if not first:
                result["failed"] += 1
                continue
            actual, soup = first
            wanted_path = unquote(urlparse(url).path).rstrip("/")
            if unquote(urlparse(actual).path).rstrip("/") != wanted_path:
                result["unavailable"] += 1
                continue
            first_html = str(soup)
            page_parser = parse_list_page if kind == "list" else parse_novobudovy_page
            parse = ((lambda html, link: page_parser(html, link, region, category))
                     if kind == "list" else (lambda html, link: page_parser(html, link)))
            first_leads = parse(first_html, actual)
            # Missing category on List redirects to its generic homepage (no
            # cards), while a genuine zero-result category may have no cards.
            if kind == "list" and not soup.select("#itemsList"):
                result["unavailable"] += 1
                continue
            maximum = page_count(first_html, url, list_directory=kind == "list", cap=500)
            start_page = min(start_page, maximum)
            last_page = min(maximum, start_page + max_pages_per_topic - 1)
            for page in range(start_page, last_page + 1):
                if cancel and cancel.is_set():
                    raise InterruptedError("Пошук зупинено")
                if page == 1:
                    leads = first_leads
                else:
                    page_url = (list_url(CITIES[region], LIST_TOPICS[category], page)
                                if kind == "list" else url + f"/{page}")
                    response = catalog.get(page_url, cancel)
                    if not response:
                        result["failed"] += 1
                        break
                    actual_page, html_soup = response
                    if unquote(urlparse(actual_page).path) != unquote(urlparse(page_url).path):
                        result["failed"] += 1
                        break
                    leads = parse(str(html_soup), actual_page)
                for lead in leads:
                    result["new"] += save_contact(con, lead.name, lead.category, lead.region,
                                                   lead.phone, source_url=lead.source_url,
                                                   role="Публічний номер компанії",
                                                   activity="Каталог · чинність номера не перевірено")
                    result["listed"] += 1
                con.execute("""INSERT INTO source_cursors(source,offset,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                               ON CONFLICT(source) DO UPDATE SET offset=excluded.offset,
                               updated_at=excluded.updated_at""",
                            (key, str(page + 1 if page < maximum else 1)))
                con.commit()
                result["pages"] += 1
                if refresh and result["pages"] % 5 == 0:
                    refresh()
                if progress:
                    progress(f"Каталоги {task_idx}/{len(tasks)} · {region} · {category}: "
                             f"стор. {page}/{maximum} · телефонів у картках {result['listed']} "
                             f"· нових номерів {result['new']}")
            result["groups"] += 1
    if refresh:
        refresh()
    return result
