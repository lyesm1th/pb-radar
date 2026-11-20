"""Contact extraction, category matching, URL normalization and persistent storage."""

from __future__ import annotations

import csv
import datetime as dt
import json
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

OWNERSHIP_TYPES = ("Приватна", "Державна / комунальна", "Не визначено")
OWNERSHIP_FILTERS = ("Без державних", "Тільки приватні", "Державні", "Не визначено", "Усі")


def classify_ownership(name: str, *, buyer_kind: str = "") -> str:
    """Conservative classification; an organization's name is not an ownership registry.

    Prozorro's official ``procuringEntity.kind`` distinguishes public buyers.
    ``special`` may include entities with different ownership, so it remains
    unknown without a clear legal-form hint. Managers can override the result.
    """
    kind = buyer_kind.casefold().strip()
    if kind in {"authority", "central", "defense", "general", "other", "social"}:
        return "Державна / комунальна"
    upper = re.sub(r"\s+", " ", name or "").upper().strip(' "«»')
    if re.search(r"\b(КОМУНАЛЬН(?:Е|А|ИЙ|ОГО)|ДЕРЖАВН(?:Е|А|ИЙ|ОГО)|БЮДЖЕТН(?:А|Е)|ВІЙСЬКОВА ЧАСТИНА|"
                 r"МІСЬКА РАДА|СЕЛИЩНА РАДА|СІЛЬСЬКА РАДА|РАЙОННА ДЕРЖАВНА АДМІНІСТРАЦІЯ)\b", upper):
        return "Державна / комунальна"
    if re.match(r"^(ТОВ|ТЗОВ|ФОП|ПП|ПРИВАТНЕ ПІДПРИЄМСТВО)\b", upper):
        return "Приватна"
    return "Не визначено"


def filter_ownership(rows: list[dict], mode: str) -> list[dict]:
    if mode == "Без державних":
        return [row for row in rows if row["ownership"] != "Державна / комунальна"]
    mapping = {"Тільки приватні": "Приватна", "Державні": "Державна / комунальна",
               "Не визначено": "Не визначено"}
    return [row for row in rows if row["ownership"] == mapping[mode]] if mode in mapping else rows


def group_contacts_by_company(rows: list[dict]) -> list[list[dict]]:
    """Group display rows by official company ID or a stable local company key.

    Each number retains its individual status and source in the children. We
    deliberately do not merge similar names unless a shared EDRPOU confirms
    that the records refer to the same registered entity.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = ("edr:" + str(row["edrpou"])) if row.get("edrpou") else (
            "company:" + str(row["company_id"]) if row.get("company_id") else
            "row:" + str(row.get("id", "")))
        groups.setdefault(key, []).append(row)
    return list(groups.values())

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
            activity TEXT NOT NULL DEFAULT 'Невідомо',
            ownership TEXT NOT NULL DEFAULT 'Не визначено',
            ownership_locked INTEGER NOT NULL DEFAULT 0,
            edrpou TEXT NOT NULL DEFAULT ''
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
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
        );
    """)
    # Upgrade pre-0.4.1 databases in place, keeping all manager statuses.
    columns = {row["name"] for row in con.execute("PRAGMA table_info(companies)")}
    migrating = "ownership" not in columns
    if "ownership" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN ownership TEXT NOT NULL DEFAULT 'Не визначено'")
    if "ownership_locked" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN ownership_locked INTEGER NOT NULL DEFAULT 0")
    if "edrpou" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN edrpou TEXT NOT NULL DEFAULT ''")
    if migrating:
        for row in con.execute("SELECT id,name FROM companies"):
            ownership = classify_ownership(row["name"])
            if ownership != "Не визначено":
                con.execute("UPDATE companies SET ownership=? WHERE id=?", (ownership, row["id"]))
    return con


def read_setting(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def write_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("""INSERT INTO app_settings(key,value) VALUES(?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))


def read_column_widths(con: sqlite3.Connection) -> dict[str, int]:
    try:
        widths = json.loads(read_setting(con, "column_widths", "{}"))
        return {str(k): max(55, min(900, int(v))) for k, v in widths.items()}
    except (ValueError, TypeError, AttributeError):
        return {}


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
                 social: str = "", ownership: str = "", edrpou: str = "") -> bool:
    """Upsert phone and preserve its review status across searches and companies.

    Returns True only if this *phone number* has never been seen before, even
    when the same public number appears on another company's website.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    key = company_key(name, website, region)
    if ownership not in OWNERSHIP_TYPES:
        ownership = classify_ownership(name)
    edrpou = edrpou.strip() if re.fullmatch(r"\d{8}|\d{10}", edrpou.strip()) else ""
    inherited_ownership = None
    if edrpou:
        # A shared home page or a repeated name is not enough to identify a
        # legal entity. Keep distinct registry numbers as distinct companies.
        existing = con.execute("SELECT edrpou FROM companies WHERE company_key=?", (key,)).fetchone()
        if existing and existing["edrpou"] and existing["edrpou"] != edrpou:
            key = "edr:" + edrpou
        prior_owner = con.execute("""SELECT ownership FROM companies
                                    WHERE edrpou=? AND ownership_locked=1 LIMIT 1""",
                                  (edrpou,)).fetchone()
        if prior_owner:
            inherited_ownership = prior_owner["ownership"]
            ownership = inherited_ownership
    con.execute("""
        INSERT INTO companies(company_key, name, category, region, website, social,
                              source_url, checked_at, activity, ownership, edrpou)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_key) DO UPDATE SET
            name=excluded.name, category=excluded.category,
            region=CASE WHEN excluded.region != 'Не визначено' THEN excluded.region ELSE companies.region END,
            website=CASE WHEN excluded.website != '' THEN excluded.website ELSE companies.website END,
            social=CASE WHEN excluded.social != '' THEN excluded.social ELSE companies.social END,
            checked_at=excluded.checked_at,
            activity=CASE WHEN excluded.activity != 'Невідомо' THEN excluded.activity ELSE companies.activity END,
            edrpou=CASE WHEN excluded.edrpou != '' THEN excluded.edrpou ELSE companies.edrpou END,
            ownership=CASE WHEN companies.ownership_locked=1 THEN companies.ownership
                           WHEN excluded.ownership != 'Не визначено' THEN excluded.ownership
                           ELSE companies.ownership END
    """, (key, name, category, region, website, social, source_url, now, activity, ownership, edrpou))
    company_id = con.execute("SELECT id FROM companies WHERE company_key=?", (key,)).fetchone()[0]
    if inherited_ownership:
        con.execute("UPDATE companies SET ownership=?, ownership_locked=1 WHERE id=?",
                    (inherited_ownership, company_id))
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


def set_company_ownership(con: sqlite3.Connection, company_id: int, ownership: str) -> None:
    """Manager's decision survives repeated scans and later classification."""
    if ownership not in OWNERSHIP_TYPES:
        raise ValueError("Невідомий тип організації")
    entity = con.execute("SELECT edrpou FROM companies WHERE id=?", (company_id,)).fetchone()
    if entity and entity["edrpou"]:
        con.execute("UPDATE companies SET ownership=?, ownership_locked=1 WHERE edrpou=?",
                    (ownership, entity["edrpou"]))
    else:
        con.execute("UPDATE companies SET ownership=?, ownership_locked=1 WHERE id=?",
                    (ownership, company_id))


def set_phone_status(con: sqlite3.Connection, phone: str, status: str) -> int:
    """Mark every record of the same number so it never reappears as new."""
    if status not in CONTACT_STATUSES:
        raise ValueError("Невідомий статус контакту")
    cursor = con.execute("UPDATE contacts SET status=? WHERE phone=?", (status, phone))
    return cursor.rowcount


def read_contacts(con: sqlite3.Connection, statuses: tuple[str, ...] = (),
                  *, include_prozorro: bool = True,
                  categories: set[str] | None = None,
                  regions: set[str] | None = None) -> list[dict]:
    # Show one row per number, prioritizing an already-reviewed association.
    # Filter before ranking: if a number belongs to two organizations, an
    # eligible category/region can still show it instead of silently losing it.
    # None = all; empty set = explicitly no categories/regions selected.
    clauses = ["(?=1 OR LOWER(c2.source_url) NOT LIKE '%prozorro.gov.ua%')"]
    args: list[str | int] = [int(include_prozorro)]
    for field, selected in (("category", categories), ("region", regions)):
        if selected is not None:
            if not selected:
                clauses.append("1=0")
            else:
                clauses.append(f"p2.{field} IN ({','.join('?' for _ in selected)})")
                args.extend(sorted(selected))
    filter_sql = " AND ".join(clauses)
    sql = """SELECT c.id, p.id AS company_id, p.name, p.category, p.region, c.phone,
                    c.role, c.status, p.website, p.social, c.source_url, c.checked_at,
                    p.activity, p.ownership, p.edrpou
             FROM contacts c JOIN companies p ON p.id=c.company_id
             WHERE c.id IN (
                 SELECT id FROM (
                     SELECT c2.id, ROW_NUMBER() OVER (
                         PARTITION BY c2.phone ORDER BY
                         CASE WHEN c2.status='Не опрацьований' THEN 1 ELSE 0 END, c2.id
                     ) AS row_rank FROM contacts c2
                     JOIN companies p2 ON p2.id=c2.company_id
                     WHERE """ + filter_sql + """
                 ) WHERE row_rank = 1
             )"""
    if statuses:
        sql += " AND c.status IN (" + ",".join("?" for _ in statuses) + ")"
        args.extend(statuses)
    sql += " ORDER BY c.checked_at DESC, p.name"
    return [dict(row) for row in con.execute(sql, args)]


SEARCH_FIELDS = (
    "name", "category", "region", "phone", "role", "status", "website",
    "social", "source_url", "checked_at", "activity", "ownership", "edrpou",
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


def visible_link(row: dict) -> str:
    """The actual website/social/source URL shown in the last table column."""
    return str(row.get("website") or row.get("social") or row.get("source_url") or "")


def copyable_contact_value(row: dict, column: str) -> str:
    """Return a real cell value, never a grouped row's phone-count label."""
    if column == "phone" and row.get("is_group"):
        return ""
    if column == "website":
        return visible_link(row)
    return str(row.get(column) or "")


def export_csv(path: Path, rows: list[dict]) -> None:
    headings = ("name", "category", "region", "phone", "role", "status", "ownership", "edrpou",
                "website", "social", "source_url", "checked_at", "activity")
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headings, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        # Untrusted business names can begin with spreadsheet formula markers.
        # Prefix such values as text to avoid formula execution on opening CSV.
        for row in rows:
            writer.writerow({key: "'" + str(row.get(key, "")) if str(row.get(key, "")).startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                             else row.get(key, "") for key in headings})
