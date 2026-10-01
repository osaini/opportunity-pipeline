"""import_targets writes each row once and reads nothing back, and the database ends exactly as it did.

It used to call create_target per record (which reads the new target back with get_target) and, for a record the
caller marked unverified, run a second UPDATE with its own commit and read the target back again. It now inserts the
row with research_confidence already set and keeps only the new id. The reference below is the old function, written
out against the public create_target and get_target; both run on identical databases and the tables are compared.
"""

import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from uuid import uuid4

from opportunity_app import outreach
from opportunity_app.outreach import (
    IMPORT_IGNORED_FIELDS,
    OUTREACH_ORIGINS,
    _apply_draft_side_effects,
    _apply_status_side_effects,
    _claim_detail,
    log_event,
    _normalize,
    create_target,
    existing_keys,
    get_target,
    import_targets,
)
from opportunity_app.outreach_identity import company_key
from opportunity_app.contact_names import website_domain
from opportunity_app.database import is_unique_violation
from opportunity_app.schema import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
EVENT_ID = re.compile(r"^[a-z-]+-[0-9a-f]{32}$")  # an event's own id: a prefix and a uuid


def reference_create_target(conn, payload, *, user_id, today=None, origin="manual", discovery_run_id=None):
    """REFERENCE: create_target exactly as it was before the importer stopped reading each new target back.

    Frozen on purpose. The live create_target now delegates to the same _create_target the new importer calls, so a
    reference that called it would share the code under test; this copy shares only the unchanged helpers.
    """
    if origin not in OUTREACH_ORIGINS:
        raise ValueError(f"origin must be one of: {', '.join(OUTREACH_ORIGINS)}")
    today = today or outreach.local_today(conn, user_id)  # looked up on the module, so a test can patch it
    values = _normalize(payload, partial=False)
    values.setdefault("status", "not_started")
    _apply_status_side_effects(values, None, today)
    _apply_draft_side_effects(values, None)
    values["origin"] = origin
    if origin == "discovery":
        values["research_confidence"] = "unverified"
    if values.get("location"):
        values["location_basis"] = {"discovery": "research", "manual": "manual"}.get(origin, "")
    if discovery_run_id:
        values["discovery_run_id"] = discovery_run_id
    target_id = f"outreach-{uuid4().hex}"
    timestamp = utc_now()
    columns = ["id", "user_id", *values.keys(), "created_at", "updated_at"]
    try:
        with conn:
            tracked = conn.execute("SELECT company FROM outreach_targets WHERE user_id=?", (user_id,)).fetchall()
            same = next((row[0] for row in tracked if company_key(row[0]) == company_key(values["company"])), None)
            if same is not None:
                raise ValueError(f"{same} is already in your outreach list")
            conn.execute(
                f"INSERT INTO outreach_targets({', '.join(columns)}) VALUES({', '.join('?' * len(columns))})",
                [target_id, user_id, *values.values(), timestamp, timestamp],
            )
            log_event(conn, target_id, user_id, "created", to_status=values["status"])
            if values.get("location") and origin == "import":
                log_event(conn, target_id, user_id, "location_import_claim", detail=_claim_detail(payload))
            conn.execute(
                "DELETE FROM outreach_dismissed WHERE user_id=? AND company_key=?",
                (user_id, company_key(values["company"])),
            )
    except Exception as exc:
        if is_unique_violation(exc):
            raise ValueError(f"{values['company']} is already in your outreach list") from exc
        raise
    return get_target(conn, target_id, user_id=user_id, today=today)


def old_import_targets(conn, records, *, user_id, origin="import", discovery_run_id=None):
    if len(records) > 500:
        raise ValueError("Outreach imports are limited to 500 targets")
    names, domains = existing_keys(conn, user_id=user_id)
    imported, skipped, errors, created_ids = 0, 0, [], []
    for index, record in enumerate(records, start=1):
        record = {key: value for key, value in record.items() if key not in IMPORT_IGNORED_FIELDS}
        research_confidence = record.pop("research_confidence", None)
        if research_confidence == "unverified":
            record["_research_confidence"] = "unverified"
        company = str(record.get("company") or "").strip()
        domain = website_domain(str(record.get("website") or ""))
        if company_key(company) in names or (domain and domain in domains):
            skipped += 1
            continue
        try:
            internal_confidence = record.pop("_research_confidence", None)
            target = reference_create_target(conn, record, user_id=user_id, origin=origin, discovery_run_id=discovery_run_id)
            if internal_confidence == "unverified" and target["research_confidence"] != "unverified":
                with conn:
                    conn.execute(
                        "UPDATE outreach_targets SET research_confidence='unverified' WHERE id=? AND user_id=?",
                        (target["id"], user_id),
                    )
                target = get_target(conn, target["id"], user_id=user_id)
        except ValueError as exc:
            errors.append({"row": index, "company": company, "error": str(exc)})
            continue
        names.add(company_key(company))
        if domain:
            domains.add(domain)
        created_ids.append(target["id"])
        imported += 1
    return {"imported": imported, "skipped": skipped, "errors": errors, "created_ids": created_ids}


def records(count=70):
    found = []
    for index in range(count):
        record = {
            "company": f"Importco {index} Inc.", "website": f"https://importco{index}.com", "contact_email": f"hi@importco{index}.com",
            "location": "Austin, TX" if index % 3 == 0 else "", "summary": f"summary {index}",
            "status": "sent" if index % 7 == 0 else "not_started", "email_body": "Hi there" if index % 4 == 0 else "",
            "source_urls": ["https://example.com/a"] if index % 5 == 0 else [],
        }
        if index % 5 == 0:
            record["research_confidence"] = "unverified"
        if index % 6 == 0:
            record["research_confidence"] = "confirmed"
        if index % 11 == 0 and index:
            record["company"] = f"Importco {index - 1} LLC"  # the same company as an earlier row
        if index % 13 == 0 and index:
            record["status"] = "not a status"  # an error row
        if index % 17 == 0 and index:
            record["website"] = f"https://importco{index - 2}.com"  # an earlier row's domain
        if index % 19 == 0 and index:
            record.pop("company")  # no company at all
        found.append(record)
    return found


def dump(conn):
    """Every outreach row and event, with ids replaced by the company and timestamps masked."""
    names = {row["id"]: row["company"] for row in conn.execute("SELECT id, company FROM outreach_targets")}

    def clean(row):
        out = {}
        for key, value in dict(row).items():
            if key in ("id", "target_id") and value in names:
                value = names[value]
            elif isinstance(value, str) and TIMESTAMP.match(value):
                value = "<timestamp>"
            elif isinstance(value, str) and EVENT_ID.match(value):
                value = "<event id>"
            out[key] = value
        return out

    return {
        table: sorted((clean(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()), key=lambda row: json.dumps(row, sort_keys=True, default=str))
        for table in ("outreach_targets", "outreach_events", "outreach_dismissed")
    }


class Case(unittest.TestCase):
    def database(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _, platform_path = build_and_migrate(Path(tmp.name))
        conn = connect_product(platform_path)
        self.addCleanup(conn.close)
        # Already tracked, so some rows are skipped by name and by domain, and one was dismissed before.
        create_target(conn, {"company": "Importco 3", "website": "https://importco3.com"}, user_id=USER)
        create_target(conn, {"company": "Existing Co", "website": "https://importco9.com"}, user_id=USER)
        with conn:
            conn.execute(
                "INSERT INTO outreach_dismissed(user_id, company_key, company, dismissed_at) VALUES(?, ?, 'Importco 4', 't')",
                (USER, company_key("Importco 4")),
            )
        return conn


class ImportParityTests(Case):
    def test_the_database_ends_as_the_old_import_left_it(self):
        for origin in ("import", "discovery"):
            with self.subTest(origin=origin):
                old, new = self.database(), self.database()
                batch = records()
                before = old_import_targets(old, json.loads(json.dumps(batch)), user_id=USER, origin=origin)
                after = import_targets(new, json.loads(json.dumps(batch)), user_id=USER, origin=origin)
                self.assertGreater(after["imported"], 40)
                self.assertGreater(after["skipped"], 3)
                self.assertGreater(len(after["errors"]), 3)
                names = {row["id"]: row["company"] for row in old.execute("SELECT id, company FROM outreach_targets")}
                new_names = {row["id"]: row["company"] for row in new.execute("SELECT id, company FROM outreach_targets")}
                self.assertEqual([names[item] for item in before["created_ids"]], [new_names[item] for item in after["created_ids"]])
                self.assertEqual({key: value for key, value in before.items() if key != "created_ids"},
                                 {key: value for key, value in after.items() if key != "created_ids"})
                self.assertEqual(dump(old), dump(new))

    def test_a_record_marked_unverified_is_stored_unverified_and_a_confirmed_one_is_not(self):
        conn = self.database()
        import_targets(conn, [
            {"company": "Marked Co", "research_confidence": "unverified"},
            {"company": "Plain Co"},
            {"company": "Confirmed Co", "research_confidence": "confirmed"},
        ], user_id=USER)
        got = {row["company"]: row["research_confidence"] for row in conn.execute("SELECT company, research_confidence FROM outreach_targets")}
        self.assertEqual((got["Marked Co"], got["Plain Co"], got["Confirmed Co"]), ("unverified", "confirmed", "confirmed"))

    def test_each_new_row_is_one_insert_and_one_commit_with_no_read_back(self):
        conn = self.database()
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            import_targets(conn, [{"company": "One Co", "research_confidence": "unverified"}], user_id=USER)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(sum(1 for text in statements if text.startswith("INSERT INTO outreach_targets")), 1)
        self.assertEqual(sum(1 for text in statements if text.startswith("UPDATE outreach_targets")), 0)
        self.assertEqual(sum(1 for text in statements if text.strip().upper() == "COMMIT"), 1)
        self.assertEqual(sum(1 for text in statements if "outreach_draft_versions" in text), 0, "no get_target read-back")

    def test_each_row_takes_the_day_when_it_is_written_so_a_midnight_crossing_import_matches_the_old_one(self):
        days = [date(2026, 10, 1), date(2026, 10, 1), date(2026, 10, 2)]
        batch = [{"company": f"Midnight Co {n}", "status": "sent"} for n in range(3)]
        sent = {}
        for name, run in (("old", old_import_targets), ("new", import_targets)):
            conn = self.database()
            with mock.patch.object(outreach, "local_today", side_effect=list(days)):
                run(conn, json.loads(json.dumps(batch)), user_id=USER)
            sent[name] = [tuple(row) for row in conn.execute(
                "SELECT company, sent_at, follow_up_at FROM outreach_targets WHERE company LIKE 'Midnight Co %' ORDER BY company")]
        self.assertEqual(sent["new"], sent["old"])
        self.assertEqual([row[1] for row in sent["new"]], ["2026-10-01", "2026-10-01", "2026-10-02"])

    def test_create_target_still_returns_the_whole_target(self):
        conn = self.database()
        made = create_target(conn, {"company": "Whole Co", "website": "https://whole.example"}, user_id=USER)
        self.assertEqual(made, get_target(conn, made["id"], user_id=USER))
        self.assertEqual(made["research_confidence"], "confirmed")
        found = create_target(conn, {"company": "Found Co"}, user_id=USER, origin="discovery")
        self.assertEqual(found["research_confidence"], "unverified")

    def test_a_duplicate_is_still_refused_inside_the_row_transaction(self):
        conn = self.database()
        with self.assertRaises(ValueError) as caught:
            create_target(conn, {"company": "Importco 3 LLC"}, user_id=USER)
        self.assertIn("already in your outreach list", str(caught.exception))
        # import_targets skips it before trying, but a row that raced in between is still caught inside the transaction.
        with mock.patch.object(outreach, "existing_keys", return_value=(set(), set())):
            result = import_targets(conn, [{"company": "Importco 3 Incorporated"}], user_id=USER)
        self.assertEqual((result["imported"], len(result["errors"])), (0, 1))


if __name__ == "__main__":
    unittest.main()
