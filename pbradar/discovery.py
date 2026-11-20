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


def download_file(url: str, destination: Path, progress=None, max_bytes=1_500_000_000,
                  cancel: threading.Event | None = None, validate_pbf: bool = False):
    """Stream to a .partial file and atomically replace after successful completion."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".partial")
    size = 0
    try:
        with requests.get(url, stream=True, timeout=(15, 65), headers={"User-Agent": USER_AGENT}) as resp:
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
                   regions: RegionIndex, progress=None, limit=1500, cancel: threading.Event | None = None):
    """Scan public POIs; sample across the whole country instead of PBF file order.

    Region of a way is accepted only if explicit in tags. This prevents guessing
    its location from a bounding rectangle across administrative borders.
    """
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
            name = tags.get("name", "").strip()
            if not name:
                return
            groups = classify(name, tags) & selected_categories
            if not groups:
                return
            if selected_regions and region not in selected_regions:
                return
            phones = parse_ua_phones(tags.get("contact:phone", "") + " " + tags.get("phone", ""))
            website = normalize_url(tags.get("contact:website") or tags.get("website") or "")
            if not phones and not website:
                return
            osm_type = {"n": "node", "w": "way", "r": "relation"}.get(obj.type_str(), obj.type_str())
            item = {"name": name, "category": ", ".join(sorted(groups)),
                    "region": region, "website": website, "phones": phones,
                    "source_url": f"https://www.openstreetmap.org/{osm_type}/{obj.id}"}
            self.seen += 1
            if len(matches) < limit:
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
            name = node.tags.get("name")
            if not name:
                return
            # Important for the 800+ MB Ukraine extract: point-in-polygon is
            # expensive. Only calculate oblasts for relevant business POIs.
            small_tags = {field: node.tags.get(field, "") for field in
                          ("description", "office", "craft", "name:en")}
            if not (classify(name, small_tags) & selected_categories):
                return
            self._process(node, regions.at(node.location.lon, node.location.lat))

        def way(self, way):
            tags = dict(way.tags)
            region = name_region(tags.get("addr:region", "") or tags.get("addr:state", ""))
            self._process(way, region)

    Handler().apply_file(str(osm_path), locations=False)
    return matches
