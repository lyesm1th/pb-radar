"""Public Prozorro feed: company-first discovery independent of OSM.

The API provides published procurement and awarded supplier contact points.
No authentication, search-engine scraping or personal profile crawling.
Each run moves backward through tender history using a saved feed cursor.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from pathlib import Path

import requests

from .core import (CATEGORIES, DB_PATH, REGIONS, connect_db, normalize_url,
                   parse_ua_phones, save_contact)
from .discovery import address_region

API_ROOT = "https://public-api.prozorro.gov.ua/api/2.5"
CURSOR_NAME = "prozorro-tenders-desc-v1"
PAGE_SIZE = 100
DEFAULT_TENDERS = 5000
MAX_DETAIL_REQUESTS = 300
SUPPLIER_CATEGORIES = {"Архітектори", "Архітектурні бюро", "Проєктні організації",
                       "Будівельні компанії", "Монтажні організації",
                       "Будівництво складів", "Протипожежні системи",
                       "Ландшафтні дизайнери", "Дизайнери інтер'єру"}
USER_AGENT = "PB-Radar/0.4 (public procurement business contacts)"


def _phone_numbers(value: str) -> list[str]:
    result: list[str] = []
    for part in re.split(r"[;|\n]+", value or ""):
        for number in parse_ua_phones(part):
            if number not in result:
                result.append(number)
    return result


def _region(party: dict) -> str:
    address = party.get("address") or {}
    if not isinstance(address, dict):
        return "Не визначено"
    return address_region({"addr:region": str(address.get("region") or ""),
                           "addr:city": str(address.get("locality") or "")})


def _source_url(tender: dict) -> str:
    tender_id = str(tender.get("tenderID") or "")
    if re.fullmatch(r"UA-\d{4}-\d{2}-\d{2}-\d{6}-[a-z]", tender_id):
        return "https://prozorro.gov.ua/tender/" + tender_id
    return "https://prozorro.gov.ua/"


def _category_for_cpv(tender: dict) -> set[str]:
    items = tender.get("items") or []
    codes = {str((item.get("classification") or {}).get("id") or "")
             for item in items if isinstance(item, dict)}
    result: set[str] = set()
    for code in codes:
        if code.startswith("712"):
            result.update(("Архітектори", "Архітектурні бюро"))
        elif code.startswith(("713", "714", "715")):
            result.add("Проєктні організації")
        elif code.startswith("71"):
            result.add("Проєктні організації")
        if code.startswith("45"):
            result.add("Будівельні компанії")
            if code.startswith(("453", "454")):
                result.add("Монтажні організації")
            if code.startswith("452132"):
                result.add("Будівництво складів")
            if code.startswith("45343"):
                result.add("Протипожежні системи")
        if code.startswith("7142"):
            result.add("Ландшафтні дизайнери")
        if code.startswith("79932"):
            result.add("Дизайнери інтер'єру")
    return result


def extract_tender_contacts(tender: dict, categories: set[str],
                            regions: set[str]) -> list[dict]:
    """Only public work phone fields; supplier leads require relevant CPV."""
    source = _source_url(tender)
    published = str(tender.get("dateModified") or tender.get("dateCreated") or "")[:10]
    result = []

    def add_party(party: dict, category: str, role: str):
        if category not in categories or not isinstance(party, dict):
            return
        name = str(party.get("name") or (party.get("identifier") or {}).get("legalName") or "").strip()
        point = party.get("contactPoint") or {}
        if not isinstance(point, dict) or not name:
            return
        region = _region(party)
        if regions and region not in regions:
            return
        phones = _phone_numbers(str(point.get("telephone") or ""))
        if not phones:
            return
        identifier = party.get("identifier") or {}
        website = normalize_url(str(point.get("uri") or identifier.get("uri") or ""))
        for phone in phones:
            result.append({"name": name, "category": category, "region": region,
                           "phone": phone, "website": website, "source_url": source,
                           "role": role, "activity": "Prozorro · " + (published or "без дати")})

    if "Відділи закупівель" in categories:
        add_party(tender.get("procuringEntity") or {}, "Відділи закупівель", "Закупівлі (Prozorro)")

    matched = sorted(_category_for_cpv(tender) & categories)
    if matched:
        # Only awarded suppliers, not all tenderers or personal bidder data.
        for award in tender.get("awards") or []:
            if not isinstance(award, dict) or award.get("status") not in {"active", "pending"}:
                continue
            for supplier in award.get("suppliers") or []:
                for category in matched:
                    add_party(supplier, category, "Підрядник (Prozorro)")
    return result


def _get_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=(8, 25),
                                   headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
            break
        retry_header = response.headers.get("Retry-After", "")
        wait = min(20, int(retry_header)) if retry_header.isdigit() else 2 ** attempt
        time.sleep(wait)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), (dict, list)):
        raise ValueError("Prozorro повернуло неочікуваний формат відповіді")
    return data


def read_cursor(con) -> str:
    row = con.execute("SELECT offset FROM source_cursors WHERE source=?", (CURSOR_NAME,)).fetchone()
    return row[0] if row else ""


def write_cursor(con, offset: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    con.execute("""INSERT INTO source_cursors(source,offset,updated_at) VALUES(?,?,?)
                   ON CONFLICT(source) DO UPDATE SET offset=excluded.offset,
                   updated_at=excluded.updated_at""", (CURSOR_NAME, offset, now))


def scan_prozorro(categories: set[str], regions: set[str], *,
                  db_path: Path = DB_PATH, max_tenders: int = DEFAULT_TENDERS,
                  max_details: int = MAX_DETAIL_REQUESTS, resume: bool = True,
                  cancel: threading.Event | None = None, progress=None,
                  refresh=None, session: requests.Session | None = None) -> dict:
    """Scan a bounded feed slice; checkpoint each full page to resume safely.

    Buyer contacts come directly from the compact feed; detail requests are
    limited to a separate, explicit budget for awarded construction suppliers.
    """
    selected = categories & set(CATEGORIES)
    selected_regions = regions & set(REGIONS)
    if not selected or max_tenders <= 0:
        return {"examined": 0, "leads": 0, "new": 0, "details": 0}
    own_session = session is None
    session = session or requests.Session()
    examined = found = new = details = 0
    with connect_db(db_path) as con:
        offset = read_cursor(con) if resume else ""
        if progress:
            progress("Prozorro: читаю публічні контакти з офіційного реєстру…")
        try:
            while examined < max_tenders:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Пошук Prozorro зупинено")
                params = {"descending": "1", "limit": min(PAGE_SIZE, max_tenders - examined),
                          "opt_fields": "tenderID,procuringEntity"}
                if offset:
                    params["offset"] = offset
                feed = _get_json(session, API_ROOT + "/tenders", params)
                rows = feed["data"]
                if not rows:
                    break
                for item in rows:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("Пошук Prozorro зупинено")
                    if not isinstance(item, dict):
                        continue
                    records = extract_tender_contacts(item, selected, selected_regions)
                    # Read details only if searching for construction/design
                    # suppliers, or feed lacks an advertised procuringEntity.
                    need_detail = (details < max_details and
                                   (bool(selected & SUPPLIER_CATEGORIES) or
                                    ("Відділи закупівель" in selected and not item.get("procuringEntity"))))
                    if need_detail and re.fullmatch(r"[a-f0-9]{32}", str(item.get("id") or "")):
                        tender = _get_json(session, API_ROOT + "/tenders/" + item["id"])["data"]
                        details += 1
                        records.extend(extract_tender_contacts(tender, selected, selected_regions))
                        if progress and details % 25 == 0:
                            progress(f"Prozorro: детально перевірено {details}/{max_details} записів · "
                                     f"тендерів {examined}/{max_tenders} · номерів {new}")
                    seen_records = set()
                    for row in records:
                        identity = (row["name"], row["phone"], row["category"])
                        if identity in seen_records:
                            continue
                        seen_records.add(identity)
                        new += save_contact(con, row["name"], row["category"], row["region"],
                                            row["phone"], row["website"], row["source_url"],
                                            row["activity"], row["role"])
                        found += 1
                examined += len(rows)
                next_offset = str((feed.get("next_page") or {}).get("offset") or "")
                if not next_offset or next_offset == offset or len(next_offset) > 200:
                    con.commit()
                    break
                offset = next_offset
                write_cursor(con, offset)
                con.commit()
                if refresh:
                    refresh()
                if progress:
                    progress(f"Prozorro: перевірено {examined:,}/{max_tenders:,} тендерів · "
                             f"детально {details:,} · контактів {found:,} · нових {new:,}")
        finally:
            if own_session:
                session.close()
    if refresh:
        refresh()
    return {"examined": examined, "leads": found, "new": new, "details": details}
