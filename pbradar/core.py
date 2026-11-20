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

CONTACT_STATUSES = (
    "Не опрацьований", "Опрацьовано", "Зателефонували", "Не відповідає",
    "Номер недійсний", "Не цікавить співпраця", "Потенційний партнер",
    "Співпрацюємо", "Не контактувати",
)

# Match business categories using public OSM tags and business names. Inference
# does not imply the phone belongs to a named individual or department.
RULES = {
    "Архітектори": ("архітектор", "архитектор", "architect"),
    "Архітектурні бюро": ("архітектурн", "архитектурн", "architectural", "architecture"),
    "Дизайнери інтер'єру": ("інтер'єр", "інтер’єр", "интерьер", "interior design", "interior architect"),
    "Дизайнери екстер'єру": ("екстер'єр", "екстер’єр", "экстерьер", "exterior design"),
    "Ландшафтні дизайнери": ("ландшафтн", "ландшафтний дизайн", "landscape design", "landscape architect"),
    "Забудовники": ("забудовник", "девелопер", "девелопмент", "real estate developer", "property development"),
    "Генпідрядні організації": ("генпідряд", "генподряд", "general contractor", "генеральний підряд"),
    "Будівельні компанії": ("будівельн", "строительн", "construction company", "будівництв", "стройкомпан", "будсервіс"),
    "Виконроби": ("виконроб", "прораб", "site foreman"),
    "Проєктні організації": ("проєктуван", "проектуван", "проєктн", "проектн", "інжиніринг", "инжиниринг", "engineering firm"),
    "Відділи закупівель": ("відділ закупів", "отдел закуп", "procurement department", "тендерний відділ"),
    "Монтажні організації": ("монтажн", "монтажні", "будмонтаж", "installation contractor"),
    "Підрядники приватного будівництва": ("котеджне будівництво", "будівництво котедж", "будівництво будинків", "house builder", "home builder"),
    "Промислове будівництво": ("промислове будівництво", "промышленное строительство", "industrial construction"),
    "Будівництво складів": ("будівництво складів", "складське будівництво", "warehouse construction"),
    "Металоконструкції": ("металоконструк", "металлоконструк", "steel structure", "steel fabrication"),
    "Протипожежні системи": ("протипожеж", "пожежна сигналіз", "пожарная сигнализ", "fire protection", "fire safety system"),
}


def classify(name: str, tags: dict[str, str]) -> set[str]:
    # Tags describing a site/building (e.g. landuse=industrial, building=warehouse)
    # are NOT evidence that the site operator builds such structures for clients.
    blob = " ".join([name, *(tags.get(field, "") for field in (
        "description", "name:en", "name:uk", "name:ru", "brand", "operator",
        "company", "business", "service"))]).lower()
    groups = {cat for cat, words in RULES.items() if any(w in blob for w in words)}
    office = tags.get("office", "").lower()
    craft = tags.get("craft", "").lower()
    company = tags.get("company", "").lower()
    if office in {"architect", "architecture"}:
        groups.update(("Архітектори", "Архітектурні бюро"))
    if office in {"construction", "construction_company", "building_contractor"} or company == "construction":
        groups.add("Будівельні компанії")
    if office in {"engineer", "engineering"} or company in {"civil_engineering", "engineering"}:
        groups.add("Проєктні організації")
    if office in {"property_developer", "developer"} or company in {"property_development", "developer"}:
        groups.add("Забудовники")
    if craft in {"builder", "roofer", "plasterer", "floorer", "carpenter", "stonemason"}:
        groups.add("Підрядники приватного будівництва")
    if craft in {"builder", "roofer"}:
        groups.add("Будівельні компанії")
    if craft in {"interior_designer", "interior_design"}:
        groups.add("Дизайнери інтер'єру")
    if office in {"interior_design", "interior_designer"}:
        groups.add("Дизайнери інтер'єру")
    if office in {"landscape_architect", "landscape_design"}:
        groups.add("Ландшафтні дизайнери")
    if craft in {"landscape_gardener", "landscape_architect", "landscape_designer"}:
        groups.add("Ландшафтні дизайнери")
    if craft in {"window_construction", "door_construction", "floorer", "plasterer", "electrician"}:
        groups.add("Монтажні організації")
    if craft in {"metal_construction", "metalworking", "welder", "steelworker"}:
        groups.add("Металоконструкції")
    if office in {"fire_protection", "fire_safety"}:
        groups.add("Протипожежні системи")
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
    parsed = urlparse(normalize_url(website))
    host = parsed.hostname or ""
    if host:
        host = re.sub(r"^www\.", "", host.lower())
        # A social platform hosts thousands of unrelated companies. Domain alone
        # would collapse them into one record and silently lose their contacts.
        shared_hosts = {"facebook.com", "instagram.com", "linkedin.com", "behance.net",
                        "t.me", "sites.google.com", "linktr.ee", "youtube.com"}
        if host in shared_hosts:
            path = parsed.path.strip("/").lower()
            if path:
                return f"web:{host}/{path}"
            return f"name:{re.sub(r'\W+', '', name.lower())}:{region.lower()}"
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
        CREATE TABLE IF NOT EXISTS website_checks (
            website TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
            phone_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS source_cursors (
            source TEXT PRIMARY KEY, offset TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
    """)
    return con


def site_needs_check(con: sqlite3.Connection, website: str,
                     now: dt.datetime | None = None) -> bool:
    """Avoid repeated requests: recheck fruitful sites weekly, empty sites daily."""
    item = con.execute("SELECT checked_at,phone_count FROM website_checks WHERE website=?",
                       (website,)).fetchone()
    if item is None:
        return True
    try:
        last = dt.datetime.fromisoformat(item["checked_at"])
        today = now or dt.datetime.now(dt.timezone.utc)
        return today - last >= dt.timedelta(days=7 if item["phone_count"] else 1)
    except (ValueError, TypeError):
        return True


def mark_site_checked(con: sqlite3.Connection, website: str, phone_count: int) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    con.execute("""INSERT INTO website_checks(website,checked_at,phone_count) VALUES(?,?,?)
                   ON CONFLICT(website) DO UPDATE SET checked_at=excluded.checked_at,
                   phone_count=excluded.phone_count""", (website, now, phone_count))


def save_contact(con: sqlite3.Connection, name: str, category: str, region: str,
                 phone: str, website: str = "", source_url: str = "",
                 activity: str = "Невідомо", role: str = "Не визначено",
                 social: str = "") -> bool:
    """Upsert phone and preserve its review status across searches and companies.

    Returns True only if this *phone number* has never been seen before, even
    when the same public number appears on another company's website.
    """
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
    prior = con.execute(
        """SELECT status FROM contacts WHERE phone=?
           ORDER BY CASE WHEN status='Не опрацьований' THEN 1 ELSE 0 END, id
           LIMIT 1""", (phone,)
    ).fetchone()
    inherited_status = prior["status"] if prior else "Не опрацьований"
    con.execute("""
        INSERT INTO contacts(company_id, phone, role, status, found_at, checked_at, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id,phone) DO UPDATE SET
            checked_at=excluded.checked_at,
            source_url=CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE contacts.source_url END,
            role=CASE WHEN contacts.role='Не визначено' THEN excluded.role ELSE contacts.role END
    """, (company_id, phone, role, inherited_status, now, now, source_url))
    return prior is None


def set_phone_status(con: sqlite3.Connection, phone: str, status: str) -> int:
    """Mark every record of the same number so it never reappears as new."""
    if status not in CONTACT_STATUSES:
        raise ValueError("Невідомий статус контакту")
    cursor = con.execute("UPDATE contacts SET status=? WHERE phone=?", (status, phone))
    return cursor.rowcount


def read_contacts(con: sqlite3.Connection, statuses: tuple[str, ...] = ()) -> list[dict]:
    # Show one row per number, prioritizing an already-reviewed association.
    # All underlying source/company associations remain in SQLite.
    sql = """SELECT c.id, p.name, p.category, p.region, c.phone, c.role, c.status,
                    p.website, p.social, c.source_url, c.checked_at, p.activity
             FROM contacts c JOIN companies p ON p.id=c.company_id
             WHERE c.id IN (
                 SELECT id FROM (
                     SELECT id, ROW_NUMBER() OVER (
                         PARTITION BY phone ORDER BY
                         CASE WHEN status='Не опрацьований' THEN 1 ELSE 0 END, id
                     ) AS row_rank FROM contacts
                 ) WHERE row_rank = 1
             )"""
    args: list[str] = []
    if statuses:
        sql += " AND c.status IN (" + ",".join("?" for _ in statuses) + ")"
        args = list(statuses)
    sql += " ORDER BY c.checked_at DESC, p.name"
    return [dict(row) for row in con.execute(sql, args)]


SEARCH_FIELDS = (
    "name", "category", "region", "phone", "role", "status", "website",
    "social", "source_url", "checked_at", "activity",
)


def filter_contacts(rows: list[dict], query: str) -> list[dict]:
    """Search every displayed/stored contact field; terms can match separately.

    Recognize both +380... and the 0XX... Ukrainian local form so managers
    can paste either number format. Search is offline and read-only.
    """
    terms = query.casefold().replace("’", "'").split()
    if not terms:
        return rows
    result = []
    for row in rows:
        haystack = " ".join(str(row.get(field) or "") for field in SEARCH_FIELDS).casefold().replace("’", "'")
        phone = str(row.get("phone") or "")
        digits = re.sub(r"\D", "", phone)
        local_digits = "0" + digits[3:] if digits.startswith("380") else digits
        if all(term in haystack or (
            (needle := re.sub(r"\D", "", term)) and len(needle) >= 2 and
            (needle in digits or needle in local_digits)
        ) for term in terms):
            result.append(row)
    return result


def export_csv(path: Path, rows: list[dict]) -> None:
    headings = ("name", "category", "region", "phone", "role", "status", "website", "social", "source_url", "checked_at", "activity")
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headings, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        # Untrusted business names can begin with spreadsheet formula markers.
        # Prefix such values as text to avoid formula execution on opening CSV.
        for row in rows:
            writer.writerow({key: "'" + str(row.get(key, "")) if str(row.get(key, "")).startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                             else row.get(key, "") for key in headings})
