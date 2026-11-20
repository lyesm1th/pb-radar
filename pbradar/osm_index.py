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

from .core import BASE_DIR, CATEGORIES, REGIONS, classify
from .discovery import (RegionIndex, address_region, extract_business_contacts,
                        osm_business_name)

INDEX_FILE = BASE_DIR / "cache" / "osm-index.sqlite3"
INDEX_VERSION = "3"
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
            return con.execute("SELECT COUNT(*) FROM leads WHERE phones != '[]' OR website != ''").fetchone()[0]

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
                social TEXT NOT NULL, phones TEXT NOT NULL, source_url TEXT NOT NULL
            );
            CREATE TABLE lead_categories(lead_id INTEGER NOT NULL, category TEXT NOT NULL);
        """)

        class Handler(osmium.SimpleHandler):
            scanned = 0
            found = 0
            with_phones = 0
            website_only = 0
            relevant = 0
            no_contacts = 0
            no_region = 0
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
                    progress(f"OSM-об'єкти {self.scanned:,} / {total:,} ({fraction:.1f}%) · "
                             f"залишилось {eta} · кандидатів {self.relevant:,} · "
                             f"з телефоном {self.with_phones:,} · лише сайт {self.website_only:,}")

            def _add(self, obj, region: str, tags: dict, groups: set[str], name: str):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Індексування зупинено")
                phones, website, social = extract_business_contacts(tags)
                if not phones and not website:
                    self.no_contacts += 1
                else:
                    self.found += 1
                    if phones:
                        self.with_phones += 1
                    else:
                        self.website_only += 1
                if region == "Не визначено":
                    self.no_region += 1
                osm_type = {"n": "node", "w": "way", "r": "relation"}.get(obj.type_str(), obj.type_str())
                row = (name, region, ", ".join(sorted(groups)), website, social,
                       json.dumps(phones, ensure_ascii=False),
                       f"https://www.openstreetmap.org/{osm_type}/{obj.id}")
                cur = con.execute("""INSERT INTO leads(name,region,categories,website,social,phones,source_url)
                                     VALUES (?,?,?,?,?,?,?)""", row)
                con.executemany("INSERT INTO lead_categories(lead_id,category) VALUES (?,?)",
                                [(cur.lastrowid, group) for group in sorted(groups)])
                if self.relevant % 400 == 0:
                    con.commit()
                    if progress:
                        progress(f"Індекс України: {self.relevant:,} кандидатів · "
                                 f"{self.with_phones:,} з телефоном · {self.website_only:,} лише із сайтом")

            def _consider(self, obj, tags: dict, region: str):
                name = osm_business_name(tags, obj.id)
                if not name:
                    return
                groups = classify(name, tags)
                if not groups:
                    return
                self.relevant += 1
                self._add(obj, region, tags, groups, name)

            def node(self, node):
                self._tick()
                if not node.location.valid():
                    return
                if not (node.tags.get("name") or node.tags.get("name:uk") or
                        node.tags.get("name:en") or node.tags.get("name:ru") or node.tags.get("brand") or
                        node.tags.get("operator") or node.tags.get("office") or
                        node.tags.get("craft") or node.tags.get("company")):
                    return
                tags = dict(node.tags)
                name = osm_business_name(tags, node.id)
                groups = classify(name, tags) if name else set()
                if groups:
                    self.relevant += 1
                    self._add(node, regions.at(node.location.lon, node.location.lat), tags, groups, name)

            def way(self, way):
                self._tick()
                if not way.tags:
                    return
                tags = dict(way.tags)
                name = osm_business_name(tags, way.id)
                if not name or not classify(name, tags):
                    return
                region = address_region(tags)
                if region == "Не визначено":
                    # The location index holds coordinates only in memory;
                    # no second nationwide PBF or node-cache file is created.
                    for ref in way.nodes:
                        if ref.location.valid():
                            region = regions.at(ref.location.lon, ref.location.lat)
                            break
                self._consider(way, tags, region)

            def relation(self, relation):
                self._tick()
                if not relation.tags:
                    return
                tags = dict(relation.tags)
                self._consider(relation, tags, address_region(tags))

        handler = Handler()
        handler.apply_file(str(osm_path), locations=True, idx="flex_mem")
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
                         ("without_public_contacts", str(handler.no_contacts)),
                         ("with_public_phone", str(handler.with_phones)),
                         ("website_without_phone", str(handler.website_only)),
                         ("unknown_region", str(handler.no_region))])
        con.commit()
        con.close()
        stage.replace(db_path)
        if progress:
            progress(f"Індекс готовий: {handler.relevant:,} кандидатів · "
                     f"{handler.with_phones:,} з телефонами OSM · "
                     f"{handler.website_only:,} лише із сайтом · "
                     f"{handler.no_contacts:,} без телефону/сайту")
        return handler.found
    except BaseException:
        con.close()
        stage.unlink(missing_ok=True)
        raise


def find_indexed_leads(osm_path: Path, selected_categories: set[str],
                       selected_regions: set[str], db_path: Path = INDEX_FILE,
                       limit: int | None = None) -> list[dict]:
    """Fast region/category query, entirely local after a successful index build."""
    if not index_is_current(osm_path, db_path):
        raise RuntimeError("Індекс OSM відсутній або застарілий")
    if not selected_categories or (limit is not None and limit <= 0):
        return []
    categories = sorted(selected_categories & set(CATEGORIES))
    if not categories:
        return []
    category_marks = ",".join("?" for _ in categories)
    sql = ("SELECT DISTINCT leads.id,leads.name,leads.region,leads.categories,leads.website,"
           "leads.social,leads.phones,leads.source_url FROM leads "
           "JOIN lead_categories cats ON cats.lead_id=leads.id "
           f"WHERE cats.category IN ({category_marks}) AND "
           "(leads.phones != '[]' OR leads.website != '')")
    params: list = list(categories)
    if selected_regions:
        chosen = sorted(selected_regions & set(REGIONS))
        if not chosen:
            return []
        sql += f" AND leads.region IN ({','.join('?' for _ in chosen)})"
        params.extend(chosen)
    sql += " ORDER BY leads.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with sqlite3.connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return [{"name": row[1], "region": row[2], "category": row[3],
             "website": row[4], "social": row[5], "phones": json.loads(row[6]), "source_url": row[7]}
            for row in rows]
