"""Contact extraction, category matching, URL normalization and persistent storage."""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import phonenumbers

BASE_DIR = Path(os.getenv("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "PB-Radar"
DB_PATH = BASE_DIR / "contacts.sqlite3"

CATEGORIES = (
    "Архітектори", "Архітектурні бюро", "Дизайнери інтер'єру",
    "Дизайнери екстер'єру", "Ландшафтні дизайнери", "Забудовники",
    "Генпідрядні організації", "Будівельні компанії", "Виконроби",
    "Проєктні організації", "Відділи закупівель", "Монтажні організації",
    "Підрядники приватного будівництва", "Промислове будівництво",
    "Будівництво складів", "Металоконструкції", "Протипожежні системи",
)

REGIONS = (
    "Вінницька", "Волинська", "Дніпропетровська", "Донецька", "Житомирська",
    "Закарпатська", "Запорізька", "Івано-Франківська", "Київська", "Кіровоградська",
    "Луганська", "Львівська", "Миколаївська", "Одеська", "Полтавська",
    "Рівненська", "Сумська", "Тернопільська", "Харківська", "Херсонська",
    "Хмельницька", "Черкаська", "Чернівецька", "Чернігівська", "м. Київ",
    "АР Крим", "м. Севастополь",
)

# Match business categories using public OSM tags and business names. Inference
# does not imply the phone belongs to a named individual or department.
RULES = {
    "Архітектори": ("архітектор", "архитектор", "architect"),
    "Архітектурні бюро": ("архітектурн", "архитектурн", "architectural", "architecture"),
    "Дизайнери інтер'єру": ("інтер'єр", "интерьер", "interior"),
    "Дизайнери екстер'єру": ("екстер'єр", "экстерьер", "exterior"),
    "Ландшафтні дизайнери": ("ландшафт", "landscap"),
    "Забудовники": ("забудовник", "девелоп", "developer", "development"),
    "Генпідрядні організації": ("генпідряд", "генподряд", "general contractor"),
    "Будівельні компанії": ("будівельн", "строительн", "construction", "будівництв"),
    "Виконроби": ("виконроб", "прораб", "site foreman"),
    "Проєктні організації": ("проєктн", "проектн", "engineering", "проектуван"),
    "Відділи закупівель": ("відділ закупів", "отдел закуп", "procurement"),
    "Монтажні організації": ("монтаж", "installation"),
    "Підрядники приватного будівництва": ("котедж", "приватн", "cottage", "house building"),
    "Промислове будівництво": ("промислов", "промышлен", "industrial"),
    "Будівництво складів": ("складськ", "складов", "warehouse", "logistics"),
    "Металоконструкції": ("металоконструк", "металлоконструк", "steel structure"),
    "Протипожежні системи": ("протипожеж", "пожежн", "fire protect"),
}


def classify(name: str, tags: dict[str, str]) -> set[str]:
    blob = " ".join([name, tags.get("description", ""), tags.get("office", ""),
                     tags.get("craft", ""), tags.get("name:en", "")]).lower()
    groups = {cat for cat, words in RULES.items() if any(w in blob for w in words)}
    if tags.get("office") == "architect":
        groups.update(("Архітектори", "Архітектурні бюро"))
    if tags.get("office") == "construction_company":
        groups.add("Будівельні компанії")
    if tags.get("craft") in ("builder", "roofer"):
        groups.add("Підрядники приватного будівництва")
    return groups


def parse_ua_phones(text: str) -> list[str]:
    """Return deduplicated Ukrainian dialable numbers in E.164 format."""
    results: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(text, "UA", leniency=phonenumbers.Leniency.VALID):
        obj = match.number
        if obj.country_code != 380 or not phonenumbers.is_valid_number(obj):
            continue
        number = phonenumbers.format_number(obj, phonenumbers.PhoneNumberFormat.E164)
        if number not in results:
            results.append(number)
    return results


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return ""
    return value


def company_key(name: str, website: str, region: str) -> str:
    host = urlparse(normalize_url(website)).hostname or ""
    if host:
        host = re.sub(r"^www\.", "", host.lower())
        return f"web:{host}"
    normalized = re.sub(r"\W+", "", name.lower(), flags=re.UNICODE)
    return f"name:{normalized}:{region.lower()}"


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY, company_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL,
            website TEXT NOT NULL DEFAULT '', social TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL DEFAULT '',
            activity TEXT NOT NULL DEFAULT 'Невідомо'
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
            phone TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'Не визначено',
            status TEXT NOT NULL DEFAULT 'Не опрацьований',
            found_at TEXT NOT NULL, checked_at TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            UNIQUE(company_id, phone)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone);
    """)
    return con


def save_contact(con: sqlite3.Connection, name: str, category: str, region: str,
                 phone: str, website: str = "", source_url: str = "",
                 activity: str = "Невідомо", role: str = "Не визначено",
                 social: str = "") -> bool:
    """Upsert contact and preserve user-set status; return True for new phone/company pair."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    key = company_key(name, website, region)
    con.execute("""
        INSERT INTO companies(company_key, name, category, region, website, social, source_url, checked_at, activity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_key) DO UPDATE SET
            name=excluded.name, category=excluded.category,
            region=CASE WHEN excluded.region != 'Не визначено' THEN excluded.region ELSE companies.region END,
            website=CASE WHEN excluded.website != '' THEN excluded.website ELSE companies.website END,
            social=CASE WHEN excluded.social != '' THEN excluded.social ELSE companies.social END,
            checked_at=excluded.checked_at,
            activity=CASE WHEN excluded.activity != 'Невідомо' THEN excluded.activity ELSE companies.activity END
    """, (key, name, category, region, website, social, source_url, now, activity))
    company_id = con.execute("SELECT id FROM companies WHERE company_key=?", (key,)).fetchone()[0]
    existing = con.execute(
        "SELECT id FROM contacts WHERE company_id=? AND phone=?", (company_id, phone)
    ).fetchone()
    con.execute("""
        INSERT INTO contacts(company_id, phone, role, found_at, checked_at, source_url)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id,phone) DO UPDATE SET
            checked_at=excluded.checked_at,
            source_url=CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE contacts.source_url END,
            role=CASE WHEN contacts.role='Не визначено' THEN excluded.role ELSE contacts.role END
    """, (company_id, phone, role, now, now, source_url))
    return existing is None


def read_contacts(con: sqlite3.Connection, statuses: tuple[str, ...] = ()) -> list[dict]:
    sql = """SELECT c.id, p.name, p.category, p.region, c.phone, c.role, c.status,
                    p.website, p.social, c.source_url, c.checked_at, p.activity
             FROM contacts c JOIN companies p ON p.id=c.company_id"""
    args: list[str] = []
    if statuses:
        sql += " WHERE c.status IN (" + ",".join("?" for _ in statuses) + ")"
        args = list(statuses)
    sql += " ORDER BY c.checked_at DESC, p.name"
    return [dict(row) for row in con.execute(sql, args)]


def export_csv(path: Path, rows: list[dict]) -> None:
    headings = ("name", "category", "region", "phone", "role", "status", "website", "social", "source_url", "checked_at", "activity")
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headings, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
