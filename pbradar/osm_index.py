"""One reusable SQLite index for nationwide OSM business candidates.

The original Geofabrik PBF is kept once. Re-indexing writes to a temporary
database and replaces the previous index only after a successful full scan.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .core import BASE_DIR, CATEGORIES, REGIONS, classify, normalize_url, parse_ua_phones
from .discovery import RegionIndex, name_region

INDEX_FILE = BASE_DIR / "cache" / "osm-index.sqlite3"
INDEX_VERSION = "2"
PROGRESS_EVERY = 150_000


def count_osm_objects(osm_path: Path, progress=None,
                      cancel: threading.Event | None = None) -> tuple[int, int, int]:
    """Count exact PBF nodes/ways/relations without creating another data file.

    PBF headers do not contain a reliable total object count. This lightweight
    preliminary read adds some startup time but avoids showing a fake percentage.
    """
    import osmium

    if cancel is not None and cancel.is_set():
        raise InterruptedError("Індексування зупинено")

    class Counter(osmium.SimpleHandler):
        nodes = 0
        ways = 0
        relations = 0

        def _check(self):
            count = self.nodes + self.ways + self.relations
            if count % 500_000 == 0:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Індексування зупинено")
                if progress:
                    progress(f"Підраховую загальну кількість OSM-об'єктів · уже {count:,}…")

        def node(self, _node):
            self.nodes += 1
            self._check()

        def way(self, _way):
            self.ways += 1
            self._check()

        def relation(self, _relation):
            self.relations += 1
            self._check()

    counter = Counter()
    counter.apply_file(str(osm_path), locations=False)
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Індексування зупинено")
    return counter.nodes, counter.ways, counter.relations


def _source_signature(path: Path) -> tuple[str, str, str]:
    stat = path.stat()
    return str(path.resolve()), str(stat.st_size), str(stat.st_mtime_ns)


def index_is_current(osm_path: Path, db_path: Path = INDEX_FILE) -> bool:
    try:
        expected = _source_signature(osm_path)
        if not db_path.is_file():
            return False
        with sqlite3.connect(db_path) as con:
            meta = dict(con.execute("SELECT key, value FROM meta"))
        return (meta.get("version") == INDEX_VERSION and
                tuple(meta.get(key) for key in ("source", "size", "mtime_ns")) == expected)
    except (OSError, sqlite3.Error, ValueError):
        return False


def build_osm_index(osm_path: Path, regions: RegionIndex, progress=None,
                    cancel: threading.Event | None = None, db_path: Path = INDEX_FILE,
                    on_progress=None) -> int:
    """Build for ALL categories and oblasts, not only currently selected ones."""
    import osmium

    if index_is_current(osm_path, db_path):
        with sqlite3.connect(db_path) as con:
            return con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    if progress:
        progress("Готую точний лічильник OSM · один попередній швидкий прохід…")
    nodes, ways, relations = count_osm_objects(osm_path, progress=progress, cancel=cancel)
    total = nodes + ways + relations
    if progress:
        progress(f"OSM: усього {total:,} об'єктів. Починаю індексацію…")
    if on_progress:
        on_progress(0, total)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    stage = db_path.with_name(db_path.name + ".partial")
    # A previous interrupted process may have left only this known staging file.
    stage.unlink(missing_ok=True)
    con = sqlite3.connect(stage)
    try:
        con.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE leads(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
                categories TEXT NOT NULL, website TEXT NOT NULL,
                phones TEXT NOT NULL, source_url TEXT NOT NULL
            );
            CREATE TABLE lead_categories(lead_id INTEGER NOT NULL, category TEXT NOT NULL);
        """)

        class Handler(osmium.SimpleHandler):
            scanned = 0
            found = 0
            relevant = 0
            no_contacts = 0
            started = time.monotonic()

            def _tick(self):
                self.scanned += 1
                if self.scanned % PROGRESS_EVERY:
                    return
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Індексування зупинено")
                if on_progress:
                    on_progress(self.scanned, total)
                if progress:
                    elapsed = time.monotonic() - self.started
                    remaining = (elapsed / self.scanned * (total - self.scanned)) if total else 0
                    eta = (f"~{round(remaining / 60)} хв" if remaining >= 120
                           else f"~{round(remaining)} с")
                    fraction = (100 * self.scanned / total) if total else 100
                    progress(f"OSM {self.scanned:,} / {total:,} ({fraction:.1f}%) · "
                             f"залишилось {eta} · кандидатів {self.relevant:,} · "
                             f"із контактами {self.found:,}")

            def _add(self, obj, region: str, tags: dict, groups: set[str]):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Індексування зупинено")
                phones = parse_ua_phones(tags.get("contact:phone", "") + " " + tags.get("phone", ""))
                website = normalize_url(tags.get("contact:website") or tags.get("website") or "")
                if not phones and not website:
                    self.no_contacts += 1
                    return
                osm_type = {"n": "node", "w": "way", "r": "relation"}.get(obj.type_str(), obj.type_str())
                row = (tags["name"].strip(), region, ", ".join(sorted(groups)), website,
                       json.dumps(phones, ensure_ascii=False),
                       f"https://www.openstreetmap.org/{osm_type}/{obj.id}")
                cur = con.execute("""INSERT INTO leads(name,region,categories,website,phones,source_url)
                                     VALUES (?,?,?,?,?,?)""", row)
                con.executemany("INSERT INTO lead_categories(lead_id,category) VALUES (?,?)",
                                [(cur.lastrowid, group) for group in sorted(groups)])
                self.found += 1
                if self.found % 400 == 0:
                    con.commit()
                    if progress:
                        progress(f"Індекс України: {self.found:,} організацій з публічними контактами")

            def node(self, node):
                self._tick()
                if not node.location.valid():
                    return
                name = node.tags.get("name", "").strip()
                if not name:
                    return
                tags = {key: node.tags.get(key, "") for key in
                        ("name", "name:en", "description", "office", "craft", "contact:phone",
                         "phone", "contact:website", "website")}
                groups = classify(name, tags)
                if groups:
                    self.relevant += 1
                    self._add(node, regions.at(node.location.lon, node.location.lat), tags, groups)

            def way(self, way):
                self._tick()
                name = way.tags.get("name", "").strip()
                if not name:
                    return
                tags = dict(way.tags)
                groups = classify(name, tags)
                if groups:
                    self.relevant += 1
                    region = name_region(tags.get("addr:region", "") or tags.get("addr:state", ""))
                    self._add(way, region, tags, groups)

            def relation(self, _relation):
                # Relations are counted in the progress denominator. They are
                # not yet indexed as contacts in this version of the scanner.
                self._tick()

        handler = Handler()
        handler.apply_file(str(osm_path), locations=False)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Індексування зупинено")
        if on_progress:
            on_progress(total, total)
        if progress:
            progress(f"OSM {total:,} / {total:,} (100%) · формую індекс для швидкого пошуку…")

        con.executescript("""
            CREATE INDEX idx_leads_region ON leads(region);
            CREATE INDEX idx_lead_categories_category ON lead_categories(category,lead_id);
        """)
        source, size, mtime_ns = _source_signature(osm_path)
        con.executemany("INSERT INTO meta(key,value) VALUES (?,?)",
                        [("version", INDEX_VERSION), ("source", source),
                         ("size", size), ("mtime_ns", mtime_ns),
                         ("total_objects", str(total)), ("node_count", str(nodes)),
                         ("way_count", str(ways)), ("relation_count", str(relations)),
                         ("relevant_candidates", str(handler.relevant)),
                         ("without_public_contacts", str(handler.no_contacts))])
        con.commit()
        con.close()
        stage.replace(db_path)
        if progress:
            progress(f"Індекс готовий: {handler.relevant:,} релевантних об'єктів · "
                     f"{handler.found:,} із телефоном або сайтом · "
                     f"{handler.no_contacts:,} без публічних контактів в OSM")
        return handler.found
    except BaseException:
        con.close()
        stage.unlink(missing_ok=True)
        raise


def find_indexed_leads(osm_path: Path, selected_categories: set[str],
                       selected_regions: set[str], db_path: Path = INDEX_FILE,
                       limit: int = 1500) -> list[dict]:
    """Fast region/category query, entirely local after a successful index build."""
    if not index_is_current(osm_path, db_path):
        raise RuntimeError("Індекс OSM відсутній або застарілий")
    if not selected_categories or limit <= 0:
        return []
    categories = sorted(selected_categories & set(CATEGORIES))
    if not categories:
        return []
    category_marks = ",".join("?" for _ in categories)
    sql = ("SELECT DISTINCT leads.name,leads.region,leads.categories,leads.website,"
           "leads.phones,leads.source_url FROM leads "
           "JOIN lead_categories cats ON cats.lead_id=leads.id "
           f"WHERE cats.category IN ({category_marks})")
    params: list = list(categories)
    if selected_regions:
        chosen = sorted(selected_regions & set(REGIONS))
        if not chosen:
            return []
        sql += f" AND leads.region IN ({','.join('?' for _ in chosen)})"
        params.extend(chosen)
    sql += " ORDER BY RANDOM() LIMIT ?"
    params.append(limit)
    with sqlite3.connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return [{"name": row[0], "region": row[1], "category": row[2],
             "website": row[3], "phones": json.loads(row[4]), "source_url": row[5]}
            for row in rows]
