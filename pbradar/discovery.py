"""Conservative public-source discovery; no login, bypasses, or map scraping."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import random
import re
import socket
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .core import BASE_DIR, REGIONS, classify, normalize_url, parse_ua_phones

USER_AGENT = "PB-Radar/0.1 (business contact source verification; respectful crawler)"
OSM_URL = "https://download.geofabrik.de/europe/ukraine-latest.osm.pbf"
OSM_INDEX_URL = "https://download.geofabrik.de/europe/ukraine/index.html"
OSM_DATED_URL = "https://download.geofabrik.de/europe/ukraine/ukraine-{}.osm.pbf"
OSM_MIRROR_URL = "https://download.openstreetmap.fr/extracts/europe/ukraine-latest.osm.pbf"
OSM_DOWNLOAD_HOSTS = {"download.geofabrik.de", "download.openstreetmap.fr"}

# OSM uses both `phone=*` and `contact:phone=*`, with several field variants.
# Fax and SMS-only fields are deliberately excluded from callable numbers.
BUSINESS_PHONE_FIELDS = frozenset((
    "phone", "contact:phone", "mobile", "contact:mobile", "telephone",
    "contact:telephone", "phone:mobile", "contact:whatsapp", "whatsapp",
    "contact:viber", "viber",
))
BUSINESS_WEB_FIELDS = ("contact:website", "website", "website:1", "contact:url", "url")
BUSINESS_SOCIAL_FIELDS = (
    ("contact:facebook", "facebook.com"), ("facebook", "facebook.com"),
    ("contact:instagram", "instagram.com"), ("instagram", "instagram.com"),
    ("contact:linkedin", "linkedin.com"), ("linkedin", "linkedin.com"),
    ("contact:telegram", "t.me"), ("telegram", "t.me"),
)
SOCIAL_HOSTS = {"facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
                "linkedin.com", "www.linkedin.com", "t.me", "x.com", "twitter.com"}


def extract_business_contacts(tags: dict[str, str]) -> tuple[list[str], str, str]:
    """Normalize public business phone, official website and social OSM tags."""
    phones = []
    for key, value in tags.items():
        if (key.lower() in BUSINESS_PHONE_FIELDS or
                re.fullmatch(r"(?:contact:)?(?:phone|mobile):[1-9]\d*", key.lower())):
            # Separate tag values: the phone parser can otherwise mistake two
            # adjacent formatted numbers for one impossible long number.
            for part in re.split(r"[;|\n]+", value):
                for number in parse_ua_phones(part):
                    if number not in phones:
                        phones.append(number)
    website = ""
    social = ""
    for field in BUSINESS_WEB_FIELDS:
        candidate = normalize_url(tags.get(field, ""))
        if not candidate:
            continue
        if urlparse(candidate).hostname.lower() in SOCIAL_HOSTS:
            social = social or candidate
        else:
            website = candidate
            break
    for field, host in BUSINESS_SOCIAL_FIELDS:
        raw = tags.get(field, "").strip()
        if not raw:
            continue
        if raw.startswith("@"):
            candidate = normalize_url(f"https://{host}/{raw[1:]}")
        elif "://" not in raw and "." not in raw and "/" not in raw:
            candidate = normalize_url(f"https://{host}/{raw}")
        else:
            candidate = normalize_url(raw)
        if candidate and urlparse(candidate).hostname.lower() in SOCIAL_HOSTS:
            social = social or candidate
    return phones, website, social


def osm_business_name(tags: dict[str, str], osm_id: int) -> str:
    """Retain explicitly tagged commercial POIs even if the `name` field is absent."""
    for key in ("name", "name:uk", "brand", "operator", "name:en", "name:ru"):
        if value := tags.get(key, "").strip():
            return value
    office = tags.get("office", "")
    craft = tags.get("craft", "")
    company = tags.get("company", "")
    if office not in {"", "company"} or craft or company:
        return f"Без назви · {office or craft or company} (OSM #{osm_id})"
    return ""


def resolve_osm_url(progress=None, now: dt.datetime | None = None) -> str:
    """Get a direct, dated Geofabrik URL without following its broken latest alias.

    At present the `ukraine-latest.osm.pbf` alias returns a recursive HTTP 308:
    `/europe/ukraine/ukraine/ukraine/...`. The dated download URLs work.
    """
    try:
        response = requests.get(OSM_INDEX_URL, timeout=(10, 18), allow_redirects=False,
                                headers={"User-Agent": USER_AGENT})
        if response.status_code == 200:
            candidates = []
            for link in BeautifulSoup(response.text, "html.parser").select("a[href]"):
                url = urljoin(OSM_INDEX_URL, link.get("href", ""))
                match = re.fullmatch(
                    r"https://download\.geofabrik\.de/europe/ukraine/ukraine-(\d{6})\.osm\.pbf", url
                )
                if match:
                    candidates.append((match.group(1), url))
            if candidates:
                date, url = max(candidates)
                if progress:
                    progress(f"Знайдено актуальний архів Geofabrik від {date[4:6]}.{date[2:4]}.20{date[:2]}")
                return url
    except requests.RequestException:
        pass

    # The directory listing may be temporarily unavailable. Exact dated URLs
    # have no redirect loop; probe the previous two weeks without downloading.
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    for offset in range(14):
        date = today - dt.timedelta(days=offset)
        url = OSM_DATED_URL.format(date.strftime("%y%m%d"))
        try:
            response = requests.head(url, timeout=(8, 12), allow_redirects=False,
                                     headers={"User-Agent": USER_AGENT})
            if response.status_code == 200 and int(response.headers.get("Content-Length", "0")) > 100_000:
                if progress:
                    progress(f"Знайдено актуальний архів Geofabrik від {date:%d.%m.%Y}")
                return url
        except (requests.RequestException, ValueError):
            continue
    raise RuntimeError("Не вдалося визначити актуальний OSM-файл на Geofabrik. "
                       "Спробуй пізніше або вибери завантажений PBF через «Власний PBF».")


def _open_download(url: str):
    """Follow a few HTTPS redirects, never an unbounded redirect chain."""
    visited = set()
    for _ in range(5):
        if url in visited:
            raise ValueError("Джерело зациклює перенаправлення завантаження")
        visited.add(url)
        response = requests.get(url, stream=True, timeout=(15, 65), allow_redirects=False,
                                headers={"User-Agent": USER_AGENT})
        if not 300 <= getattr(response, "status_code", 200) < 400:
            return response
        next_url = urljoin(url, response.headers.get("Location", ""))
        with response:
            pass
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.hostname not in OSM_DOWNLOAD_HOSTS:
            raise ValueError("Джерело перенаправляє завантаження на неперевірений сервер")
        if parsed.hostname == "download.geofabrik.de" and "/ukraine/ukraine/ukraine-" in parsed.path:
            # Known rewrite regression (Nov 2026): every hop inserts another
            # /ukraine/ directory, so skip immediately to the independent mirror.
            raise ValueError("Geofabrik зациклює перенаправлення архіву України")
        url = next_url
    raise ValueError("Джерело зациклює перенаправлення завантаження")


def download_file(url: str, destination: Path, progress=None, max_bytes=1_500_000_000,
                  cancel: threading.Event | None = None, validate_pbf: bool = False):
    """Stream to a .partial file and atomically replace after successful completion."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".partial")
    size = 0
    try:
        with _open_download(url) as resp:
            resp.raise_for_status()
            declared = int(resp.headers.get("Content-Length", "0"))
            if declared > max_bytes:
                raise ValueError("Файл перевищує дозволений розмір")
            with part.open("wb") as out:
                for chunk in resp.iter_content(256 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("Завантаження зупинено")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Завантажено забагато даних")
                    out.write(chunk)
                    if progress and size % (4 * 1024 * 1024) < len(chunk):
                        progress(f"Завантажено {size // (1024*1024)} МБ")
        if declared and size != declared:
            raise ValueError("Завантаження неповне: розмір не відповідає відповіді сервера")
        if validate_pbf:
            with part.open("rb") as check:
                if b"OSMHeader" not in check.read(128):
                    raise ValueError("Замість OSM PBF отримано некоректний файл. Попередні дані збережено.")
        part.replace(destination)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    return destination


def download_ukraine_osm(destination: Path, progress=None,
                         cancel: threading.Event | None = None) -> str:
    """Try Geofabrik first, then an independent nationwide OSM France extract.

    Both sources produce a Ukraine-wide .osm.pbf; only one local copy is kept.
    No failed download can overwrite the previous good dataset.
    """
    sources = []
    try:
        sources.append(("Geofabrik", resolve_osm_url(progress=progress)))
    except (RuntimeError, requests.RequestException) as exc:
        if progress:
            progress(f"Geofabrik недоступний ({str(exc)[:90]}). Перевіряю резервне джерело…")
    sources.append(("OSM France", OSM_MIRROR_URL))
    failures = []
    for name, source in sources:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Завантаження зупинено")
        if progress:
            progress(f"Завантажую повну Україну · джерело: {name}…")
        try:
            download_file(source, destination, progress=progress, cancel=cancel, validate_pbf=True)
            return name
        except InterruptedError:
            raise
        except (OSError, requests.RequestException, ValueError) as exc:
            failures.append(f"{name}: {str(exc)[:100]}")
            if progress:
                progress(f"{name} не відповідає коректно. Перемикаю джерело…")
    raise RuntimeError("Не вдалося завантажити OSM України. " + " | ".join(failures))


def ensure_regions(progress=None) -> dict:
    """Read bundled ADM1 polygons. No network or external service is needed."""
    saved = Path(__file__).resolve().parent / "assets" / "ukraine-adm1.geojson"
    if progress:
        progress("Межі областей доступні локально")
    with saved.open(encoding="utf-8") as handle:
        return json.load(handle)


TRANSLIT = {
    "vinny": "Вінницька", "volyn": "Волинська", "dniprop": "Дніпропетровська",
    "donets": "Донецька", "zhytom": "Житомирська", "zakarpat": "Закарпатська",
    "zaporiz": "Запорізька", "ivano": "Івано-Франківська", "kyiv": "Київська",
    "kiev": "Київська", "kirov": "Кіровоградська", "luhansk": "Луганська",
    "lugansk": "Луганська", "lviv": "Львівська", "mykola": "Миколаївська",
    "nikola": "Миколаївська", "odes": "Одеська", "poltav": "Полтавська",
    "rivne": "Рівненська", "rovno": "Рівненська", "sumy": "Сумська",
    "ternop": "Тернопільська", "khark": "Харківська", "khers": "Херсонська",
    "khmeln": "Хмельницька", "cherkas": "Черкаська", "cherniv": "Чернівецька",
    "chernih": "Чернігівська", "chernig": "Чернігівська", "crime": "АР Крим",
    "krym": "АР Крим", "sevast": "м. Севастополь",
}


def name_region(value: str) -> str:
    name = value.strip().lower()
    if name in ("kyiv", "kiev", "київ", "м. київ"):
        return "м. Київ"
    for region in REGIONS:
        if region.lower() in name:
            return region
    if "kyiv" in name or "kiev" in name:
        if "city" in name or "city of" in name or "misto" in name:
            return "м. Київ"
    for partial, region in TRANSLIT.items():
        if partial in name:
            return region
    return "Не визначено"


# Oblast capitals are unambiguous enough to use when ways/relations carry
# `addr:city` but no `addr:region`. Other towns are not guessed from their name.
CAPITAL_CITIES = {
    "львів": "Львівська", "львов": "Львівська", "lviv": "Львівська",
    "київ": "м. Київ", "киев": "м. Київ", "kyiv": "м. Київ", "kiev": "м. Київ",
    "дніпро": "Дніпропетровська", "днепр": "Дніпропетровська", "dnipro": "Дніпропетровська",
    "харків": "Харківська", "харьков": "Харківська", "kharkiv": "Харківська",
    "одеса": "Одеська", "одесса": "Одеська", "odesa": "Одеська", "odessa": "Одеська",
    "вінниця": "Вінницька", "vinnytsia": "Вінницька",
    "луцьк": "Волинська", "lutsk": "Волинська",
    "житомир": "Житомирська", "zhytomyr": "Житомирська",
    "ужгород": "Закарпатська", "uzhhorod": "Закарпатська",
    "запоріжжя": "Запорізька", "запорожье": "Запорізька", "zaporizhzhia": "Запорізька",
    "івано-франківськ": "Івано-Франківська", "ivano-frankivsk": "Івано-Франківська",
    "кропивницький": "Кіровоградська", "kropyvnytskyi": "Кіровоградська",
    "луганськ": "Луганська", "луганск": "Луганська", "luhansk": "Луганська",
    "миколаїв": "Миколаївська", "николаев": "Миколаївська", "mykolaiv": "Миколаївська",
    "полтава": "Полтавська", "poltava": "Полтавська",
    "рівне": "Рівненська", "ровно": "Рівненська", "rivne": "Рівненська",
    "суми": "Сумська", "sumy": "Сумська",
    "тернопіль": "Тернопільська", "ternopil": "Тернопільська",
    "херсон": "Херсонська", "kherson": "Херсонська",
    "хмельницький": "Хмельницька", "khmelnytskyi": "Хмельницька",
    "черкаси": "Черкаська", "cherkasy": "Черкаська",
    "чернівці": "Чернівецька", "chernivtsi": "Чернівецька",
    "чернігів": "Чернігівська", "chernihiv": "Чернігівська",
    "донецьк": "Донецька", "донецк": "Донецька", "donetsk": "Донецька",
    "сімферополь": "АР Крим", "симферополь": "АР Крим", "simferopol": "АР Крим",
    "севастополь": "м. Севастополь", "sevastopol": "м. Севастополь",
}


def address_region(tags: dict[str, str]) -> str:
    for field in ("addr:region", "addr:state", "is_in:state", "addr:province"):
        region = name_region(tags.get(field, ""))
        if region != "Не визначено":
            return region
    city = tags.get("addr:city", "").strip().lower()
    return CAPITAL_CITIES.get(city, "Не визначено")


def inside_polygon(lon: float, lat: float, ring: list) -> bool:
    """Ray casting using GeoJSON's [longitude, latitude] axis order."""
    found = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][:2]
        xj, yj = ring[j][:2]
        if (yi > lat) != (yj > lat):
            cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < cross:
                found = not found
        j = i
    return found


def polygon_contains(lon: float, lat: float, coords: list) -> bool:
    return inside_polygon(lon, lat, coords[0]) and not any(
        inside_polygon(lon, lat, hole) for hole in coords[1:]
    )


class RegionIndex:
    def __init__(self, feature_collection: dict):
        self.items = []
        for feature in feature_collection.get("features", []):
            props = feature.get("properties", {})
            name = name_region(str(props.get("shapeName") or props.get("NAME_1") or props.get("name") or ""))
            geom = feature.get("geometry", {})
            if geom.get("type") == "Polygon":
                polygons = [geom.get("coordinates", [])]
            elif geom.get("type") == "MultiPolygon":
                polygons = geom.get("coordinates", [])
            else:
                continue
            bounds = []
            for polygon in polygons:
                positions = [point for ring in polygon for point in ring]
                if positions:
                    bounds.append((min(p[0] for p in positions), min(p[1] for p in positions),
                                   max(p[0] for p in positions), max(p[1] for p in positions)))
            self.items.append((name, polygons, bounds))

    def at(self, lon: float, lat: float) -> str:
        for name, polygons, bounds in self.items:
            if any(box[0] <= lon <= box[2] and box[1] <= lat <= box[3] and
                   polygon_contains(lon, lat, p) for p, box in zip(polygons, bounds)):
                return name
        return "Не визначено"


def public_target(url: str) -> bool:
    """Reject non-public IP ranges even if a public hostname resolves internally."""
    parsed = urlparse(normalize_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        for item in socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(item[4][0]).is_global:
                return False
    except (OSError, ValueError):
        return False
    return True


class WebsiteReader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.robots_cache: dict[str, RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robots_cache:
            rp = RobotFileParser()
            robot_url = origin + "/robots.txt"
            try:
                if not public_target(robot_url):
                    return False
                response = self.session.get(robot_url, timeout=(5, 12), allow_redirects=False)
                if response.status_code == 404:
                    self.robots_cache[origin] = None
                elif response.status_code == 200:
                    rp.parse(response.text.splitlines())
                    self.robots_cache[origin] = rp
                else:
                    return False  # fail closed on uncertain policy
            except requests.RequestException:
                return False
        policy = self.robots_cache.get(origin)
        return policy is None or policy.can_fetch(USER_AGENT, url)

    def get(self, url: str):
        url = normalize_url(url)
        if not url or not public_target(url) or not self._allowed(url):
            return None
        try:
            for _ in range(3):
                r = self.session.get(url, timeout=(5, 13), allow_redirects=False,
                                     headers={"Accept": "text/html"})
                if r.is_redirect:
                    next_url = urljoin(url, r.headers.get("Location", ""))
                    if urlparse(next_url).hostname != urlparse(url).hostname:
                        return None
                    if not public_target(next_url) or not self._allowed(next_url):
                        return None
                    url = next_url
                    continue
                r.raise_for_status()
                if "text/html" not in r.headers.get("Content-Type", ""):
                    return None
                if len(r.content) > 2_000_000:
                    return None
                r.encoding = r.apparent_encoding or r.encoding
                return url, BeautifulSoup(r.text, "html.parser")
        except requests.RequestException:
            return None
        return None


def read_site_numbers(url: str, reader: WebsiteReader) -> tuple[dict[str, str], str]:
    """Extract public contact phones; avoid unrelated social links and third-party hosts."""
    root = normalize_url(url)
    pages = [root]
    results: dict[str, str] = {}
    activity = "Невідомо"
    seen = set()
    origin_host = urlparse(root).hostname
    for target in pages:
        if not target or target in seen:
            continue
        seen.add(target)
        response = reader.get(target)
        if not response:
            continue
        actual, soup = response
        for bad in soup(["script", "style", "svg", "noscript"]):
            bad.decompose()
        for a in soup.select("a[href^='tel:']"):
            for number in parse_ua_phones(a.get("href", "")[4:]):
                results.setdefault(number, actual)
        body = soup.get_text(" ", strip=True)
        for number in parse_ua_phones(body[:130_000]):
            results.setdefault(number, actual)
        for time_tag in soup.select("time[datetime]")[:15]:
            candidate = time_tag.get("datetime", "")[:10]
            try:
                days = (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(candidate)).days
                if 0 <= days <= 180:
                    activity = "Публікація за 180 днів"
            except ValueError:
                pass
        if len(pages) < 4:
            for link in soup.find_all("a", href=True):
                label = (link.get_text(" ", strip=True) + " " + link.get("href", "")).lower()
                if not any(term in label for term in ("контакт", "contact", "зв'язок", "контакты", "про нас")):
                    continue
                next_url = urljoin(actual, link["href"])
                if urlparse(next_url).hostname == origin_host and next_url not in pages:
                    pages.append(next_url)
                    if len(pages) >= 4:
                        break
    return results, activity


def find_osm_leads(osm_path: Path, selected_categories: set[str], selected_regions: set[str],
                   regions: RegionIndex, progress=None, limit: int | None = None,
                   cancel: threading.Event | None = None):
    """Compatibility direct scanner; the GUI normally uses the cached index."""
    import osmium

    matches = []
    rnd = random.Random(12345)

    class Handler(osmium.SimpleHandler):
        seen = 0
        scanned = 0

        def _process(self, obj, region: str):
            if cancel and cancel.is_set():
                raise InterruptedError("Пошук зупинено")
            tags = dict(obj.tags)
            name = osm_business_name(tags, obj.id)
            if not name:
                return
            groups = classify(name, tags) & selected_categories
            if not groups:
                return
            if selected_regions and region not in selected_regions:
                return
            phones, website, social = extract_business_contacts(tags)
            if not phones and not website:
                return
            osm_type = {"n": "node", "w": "way", "r": "relation"}.get(obj.type_str(), obj.type_str())
            item = {"name": name, "category": ", ".join(sorted(groups)),
                    "region": region, "website": website, "social": social, "phones": phones,
                    "source_url": f"https://www.openstreetmap.org/{osm_type}/{obj.id}"}
            self.seen += 1
            if limit is None or len(matches) < limit:
                matches.append(item)
            else:
                slot = rnd.randrange(self.seen)
                if slot < limit:
                    matches[slot] = item
            if progress and self.seen % 100 == 0:
                progress(f"Відібрано {len(matches)} з {self.seen} знайдених організацій")

        def node(self, node):
            self.scanned += 1
            if self.scanned % 200_000 == 0:
                if cancel and cancel.is_set():
                    raise InterruptedError("Пошук зупинено")
                if progress:
                    progress(f"Перевірено {self.scanned:,} картографічних об'єктів · кандидатів: {self.seen}")
            if not node.location.valid():
                return
            tags = dict(node.tags)
            name = osm_business_name(tags, node.id)
            if not name:
                return
            if not (classify(name, tags) & selected_categories):
                return
            self._process(node, regions.at(node.location.lon, node.location.lat))

        def way(self, way):
            tags = dict(way.tags)
            name = osm_business_name(tags, way.id)
            if not name or not (classify(name, tags) & selected_categories):
                return
            region = address_region(tags)
            if region == "Не визначено":
                for ref in way.nodes:
                    if ref.location.valid():
                        region = regions.at(ref.location.lon, ref.location.lat)
                        break
            self._process(way, region)

        def relation(self, relation):
            self._process(relation, address_region(dict(relation.tags)))

    Handler().apply_file(str(osm_path), locations=True, idx="flex_mem")
    return matches
