"""Offline contract tests for the official public Prozorro adapter."""

import tempfile
import threading
import unittest
from pathlib import Path

from pbradar.core import connect_db, read_contacts, set_phone_status
from pbradar.prozorro import (extract_tender_contacts, read_cursor,
                             scan_prozorro)


def buyer(name="Міське управління", phone="+380671234567", region="Львівська область"):
    return {"name": name, "contactPoint": {"telephone": phone},
            "address": {"region": region, "locality": "Львів"}}


def supplier():
    return {"name": "ТОВ Будмонтаж", "contactPoint": {"telephone": "0501234567",
            "uri": "https://budmontazh.example"},
            "address": {"region": "Київська область", "locality": "Буча"}}


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        if url.endswith("/tenders/" + "a" * 32):
            return FakeResponse({"data": {"tenderID": "UA-2026-11-20-000001-a",
                "procuringEntity": buyer(),
                "items": [{"classification": {"id": "45210000-2"}}],
                "awards": [{"status": "active", "suppliers": [supplier()]}]}})
        if url.endswith("/tenders") and params.get("offset") == "older":
            return FakeResponse({"data": [{"id": "b" * 32, "tenderID": "UA-2026-11-19-000002-a",
                                  "procuringEntity": buyer("Другий замовник", "+380931234567")}],
                                 "next_page": {"offset": "oldest"}})
        return FakeResponse({"data": [{"id": "a" * 32, "tenderID": "UA-2026-11-20-000001-a",
                                  "procuringEntity": buyer()}],
                             "next_page": {"offset": "older"}})


class ProzorroTests(unittest.TestCase):
    def test_extracts_awarded_construction_suppliers_and_public_buyers(self):
        tender = {"tenderID": "UA-2026-11-20-000001-a",
                  "procuringEntity": buyer(),
                  "items": [{"classification": {"id": "45210000-2"}}],
                  "awards": [{"status": "active", "suppliers": [supplier()]}]}
        rows = extract_tender_contacts(tender,
                                      {"Будівельні компанії", "Відділи закупівель"}, set())
        self.assertEqual({row["phone"] for row in rows}, {"+380671234567", "+380501234567"})
        self.assertEqual({row["region"] for row in rows}, {"Львівська", "Київська"})
        self.assertTrue(all("prozorro.gov.ua/tender/" in row["source_url"] for row in rows))
        self.assertEqual(len(extract_tender_contacts(tender, {"Будівельні компанії"}, {"Львівська"})), 0)
        tender["items"] = [{"classification": {"id": "39100000-3"}}]
        self.assertEqual(extract_tender_contacts(tender, {"Будівельні компанії"}, set()), [])

    def test_incremental_feed_persists_cursor_and_manager_status(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "contacts.sqlite3"
            session = FakeSession()
            run = scan_prozorro({"Відділи закупівель", "Будівельні компанії"}, set(),
                                db_path=db, max_tenders=1, max_details=1, session=session)
            self.assertEqual(run, {"examined": 1, "leads": 2, "new": 2, "details": 1})
            with connect_db(db) as con:
                self.assertEqual(read_cursor(con), "older")
                self.assertEqual(set_phone_status(con, "+380671234567", "Опрацьовано"), 1)
            second = scan_prozorro({"Відділи закупівель"}, set(),
                                   db_path=db, max_tenders=1, max_details=0, session=session)
            self.assertEqual(second["new"], 1)
            self.assertEqual(session.calls[-1][1]["offset"], "older")
            with connect_db(db) as con:
                self.assertEqual(len(read_contacts(con)), 3)
                statuses = {r["phone"]: r["status"] for r in read_contacts(con)}
                self.assertEqual(statuses["+380671234567"], "Опрацьовано")
                self.assertEqual(read_cursor(con), "oldest")

    def test_stop_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            cancelled = threading.Event()
            cancelled.set()
            db = Path(folder) / "contacts.sqlite3"
            with self.assertRaises(InterruptedError):
                scan_prozorro({"Відділи закупівель"}, set(), db_path=db,
                              cancel=cancelled, session=FakeSession())
            with connect_db(db) as con:
                self.assertEqual(read_cursor(con), "")


if __name__ == "__main__":
    unittest.main()
