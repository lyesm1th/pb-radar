"""Evidence-based checks for publicly advertised business phones.

This module cannot prove carrier reachability, ownership of a SIM, or the
business's legal operating status. Those need a documented call or registry.
"""
from __future__ import annotations
import datetime as dt
import re
import threading
from dataclasses import dataclass
from urllib.parse import urlparse
from .core import connect_db, normalize_url, parse_ua_phones
from .discovery import WebsiteReader, read_site_numbers

NOT_COMPANY = ("lun.ua", "list.in.ua", "novobudovy.com", "prozorro.gov.ua",
               "openstreetmap.org", "facebook.com", "instagram.com",
               "linkedin.com", "t.me", "youtube.com", "google.com")
ROLE_LABELS = (
    ("Відділ продажу", ("відділ продаж", "sales office", "sales department", "отдел продаж")),
    ("Офіс", ("приймальн", "office", "офіс")),
    ("Сервіс", ("сервіс", "service", "підтримк", "support")),
    ("Бухгалтерія", ("бухгалтер", "accounting")),
)

@dataclass
class SiteEvidence:
    accessible: bool
    phone_pages: dict[str, str]
    heading: str = ""
    body: str = ""
    roles: dict[str, str] | None = None
    recent: bool = False

def official_site(url: str) -> str:
    candidate = normalize_url(url)
    host = (urlparse(candidate).hostname or "").lower().removeprefix("www.")
    return "" if not host or any(host == h or host.endswith("." + h) for h in NOT_COMPANY) else candidate

def site_matches_company(name: str, code: str, site: SiteEvidence) -> bool:
    if re.fullmatch(r"\d{8}", (code or "").strip()) and re.search(
        r"(?<!\d)" + re.escape(code.strip()) + r"(?!\d)", site.body):
        return True
    name = re.sub(r"^(?:товариство з обмеженою відповідальністю|тов|тзов|пп|приватне підприємство|пат|прат)\s+",
                  "", name.strip().casefold())
    company = re.sub(r"[^a-zа-яіїєґ0-9]+", "", name)
    header = re.sub(r"[^a-zа-яіїєґ0-9]+", "", site.heading.casefold())
    return len(company) >= 7 and company in header

def extract_roles(soup) -> dict[str, str]:
    found = {}
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if not href.lower().startswith("tel:"):
            continue
        phone_numbers = parse_ua_phones(href[4:])
        parent = a.find_parent(["li", "p", "td"]) or a.parent
        context = (parent.get_text(" ", strip=True) if parent else "").casefold()[:180]
        for label, needles in ROLE_LABELS:
            if any(needle in context for needle in needles):
                for number in phone_numbers:
                    found.setdefault(number, label)
                break
    return found

def inspect_site(url: str, reader: WebsiteReader) -> SiteEvidence:
    first = reader.get(url)
    if first is None:
        return SiteEvidence(False, {})
    _, soup = first
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = soup.h1.get_text(" ", strip=True) if soup.h1 else ""
    body = soup.get_text(" ", strip=True)[:150_000]
    roles = extract_roles(soup)
    phones, activity = read_site_numbers(url, reader)
    return SiteEvidence(True, phones, title + " " + h1, body, roles,
                        activity == "Публікація за 180 днів")

def call_result(status: str) -> str:
    if status == "Номер недійсний":
        return "Позначений менеджером недійсним"
    if status == "Не відповідає":
        return "Не відповідає (за позначкою)"
    if status in ("Потенційний партнер", "Співпрацюємо"):
        return "Контакт встановлено менеджером"
    if status == "Зателефонували":
        return "Дзвінок був; відповідь невідома"
    return "Не перевірено дзвінком"

def judge(row: dict, evidence: SiteEvidence | None, distinct_codes: int = 0) -> dict[str, str]:
    url = official_site(row.get("website") or "") or official_site(row.get("source_url") or "")
    data = dict(verification="Немає власного сайту", verified_owner="Не встановлено",
                company_state="ЄДР не перевірено", evidence_url=row.get("source_url") or "",
                verified_website=url)
    if not url:
        return data
    if evidence is None or not evidence.accessible:
        data["verification"] = "Сайт не вдалося перевірити"
        return data
    data["company_state"] = ("Сайт доступний, є недавня публікація; діяльність не доведена"
                             if evidence.recent else "Сайт доступний; діяльність не доведена")
    phone = row.get("phone") or ""
    if phone not in evidence.phone_pages:
        data["verification"] = "На сайті номер не знайдено"
        return data
    data["evidence_url"] = evidence.phone_pages[phone]
    if site_matches_company(row.get("name") or "", row.get("edrpou") or "", evidence):
        data["verification"] = "Є на сайті фірми"
        role = (evidence.roles or {}).get(phone, "")
        data["verified_owner"] = (role + " · " if role else "") + (row.get("name") or "")
    else:
        data["verification"] = "Є на сайті; власник невідомий"
    if distinct_codes > 1:
        data["verification"] = "Спільний номер · потрібна звірка"
        data["verified_owner"] = "Кілька компаній з різними ЄДРПОУ"
    return data

def verify_saved(*, ids: set[int] | None = None,
                 cancel: threading.Event | None = None,
                 progress=None, refresh=None, reader: WebsiteReader | None = None) -> dict:
    """Checks one phone/company association at a time, commits every record."""
    reader = reader or WebsiteReader()
    cache: dict[str, SiteEvidence] = {}
    now = dt.datetime.now(dt.timezone.utc)
    with connect_db() as db:
        where, args = "", []
        if ids is not None:
            if not ids:
                return dict(total=0, checked=0, confirmed=0, skipped=0)
            where = " WHERE c.id IN (" + ",".join("?" for _ in ids) + ")"
            args = sorted(ids)
        records = [dict(r) for r in db.execute(
            """SELECT c.id,c.phone,c.status,c.source_url,p.website,p.name,p.edrpou,
                      v.verified_at,v.verified_website
               FROM contacts c JOIN companies p ON p.id=c.company_id
               LEFT JOIN contact_verifications v ON v.contact_id=c.id"""
            + where + " ORDER BY c.id", args)]
        checked = confirmed = skipped = 0
        for index, row in enumerate(records, 1):
            if cancel and cancel.is_set():
                raise InterruptedError()
            website = (official_site(row.get("website") or "") or
                       official_site(row.get("source_url") or ""))
            previous = row.get("verified_at")
            recent = False
            if previous and website == (row.get("verified_website") or ""):
                try:
                    recent = now - dt.datetime.fromisoformat(previous) < dt.timedelta(days=7)
                except (ValueError, TypeError):
                    pass
            if recent:
                skipped += 1
                continue
            if website and website not in cache:
                cache[website] = inspect_site(website, reader)
            codes = db.execute(
                """SELECT COUNT(DISTINCT p.edrpou) FROM companies p
                   JOIN contacts c ON c.company_id=p.id
                   WHERE c.phone=? AND p.edrpou<>''""", (row["phone"],)).fetchone()[0]
            judgement = judge(row, cache.get(website), codes)
            db.execute(
                """INSERT INTO contact_verifications
                   (contact_id,verification,verified_owner,company_state,evidence_url,
                    verified_website,verified_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(contact_id) DO UPDATE SET
                   verification=excluded.verification,
                   verified_owner=excluded.verified_owner,
                   company_state=excluded.company_state,
                   evidence_url=excluded.evidence_url,
                   verified_website=excluded.verified_website,
                   verified_at=excluded.verified_at""",
                (row["id"], judgement["verification"], judgement["verified_owner"],
                 judgement["company_state"], judgement["evidence_url"],
                 judgement["verified_website"], now.isoformat(timespec="seconds")))
            db.commit()
            checked += 1
            confirmed += judgement["verification"] == "Є на сайті фірми"
            if progress and (index % 5 == 0 or index == len(records)):
                progress(f"Звірка {index:,}/{len(records):,} · підтверджено {confirmed:,} · "
                         f"раніше перевірено {skipped:,}")
            if refresh and (index % 15 == 0 or index == len(records)):
                refresh()
    return dict(total=len(records), checked=checked, confirmed=confirmed, skipped=skipped)
