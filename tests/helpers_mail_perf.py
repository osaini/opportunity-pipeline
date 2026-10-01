"""Generated outreach data for the mail-path parity tests (tests/test_mail_perf_parity.py).

The tests keep a copy of each pre-optimisation function and compare it with the live one on the data built here:
targets in every status, several Gmail drafts per kind, sends before and after a bounce, drafts too old to watch,
unreadable event details. Seeded, so a failure reproduces.

Not a test module: nothing here is named test*, so neither unittest nor pytest collects it."""

import json
import random
from datetime import datetime, timedelta, timezone

USER = "local-user"
STATUSES = ["not_started", "drafted", "paused", "sent", "followed_up", "replied", "declined", "no_response"]
UNSENT = {"not_started", "drafted", "paused"}


def populate_outreach(conn, n: int, seed: int, now: datetime | None = None) -> list[str]:
    """Add ``n`` outreach targets with a mixed history of Gmail events, in one transaction. Returns their ids."""
    rnd = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    ids = []
    for i in range(n):
        status = rnd.choice(STATUSES)
        contact_email = f"person{i}@synthco{i}.example"
        target_id = f"outreach-synth-{seed}-{i}"
        ids.append(target_id)
        stamp = (now - timedelta(days=60 - i % 50)).isoformat(timespec="microseconds")
        conn.execute(
            "INSERT INTO outreach_targets(id, user_id, company, contact_name, contact_email, contact_cc, location, email_subject, "
            "email_body, follow_up_subject, status, sent_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (target_id, USER, f"Synthco{i} Labs", f"Person {i}", contact_email,
             f"cc{i}@synthco{i}.example" if rnd.random() < 0.3 else "", "Austin, TX",
             f"Robotics internship question number {i}", f"Hi Person,\n\nShort note about Synthco{i}.\n\nSam",
             f"Following up on Synthco{i} role" if rnd.random() < 0.6 else "",
             status, None if status in UNSENT else "2026-09-01", stamp, stamp),
        )
        events: list[tuple[str, datetime, object]] = []
        # Some drafts are inside the 30 days the sends step watches and some are older.
        made = now - timedelta(minutes=rnd.randint(1, 60 * 24 * 45))
        kinds = []
        if rnd.random() < 0.75:
            kinds.append("initial")
        if status in ("sent", "followed_up") and rnd.random() < 0.5:
            kinds.append("follow_up")
        for kind in kinds:
            for version in range(rnd.randint(1, 3)):
                events.append((
                    "gmail_draft_created", made + timedelta(seconds=version * 7 + (kind == "follow_up") * 3600),
                    {"kind": kind, "draft_id": f"r-{i}-{kind}-{version}", "message_id": f"m-{i}-{kind}-{version}",
                     "thread_id": f"t{i}", "fingerprint": "f", "to": contact_email},
                ))
        if status in ("sent", "followed_up") or rnd.random() < 0.1:
            events.append(("gmail_sent", made + timedelta(hours=rnd.choice([-1, 2, 30])),
                           {"kind": "initial", "to": contact_email, "thread_id": f"t{i}", "message_id": f"s{i}", "sent_ms": 1}))
        if status == "followed_up" or rnd.random() < 0.05:
            events.append(("gmail_sent", made + timedelta(days=3), {"kind": "follow_up", "to": contact_email, "thread_id": f"t{i}"}))
        if rnd.random() < 0.12:
            events.append(("bounced", made + timedelta(hours=rnd.choice([-1, 1, 5, 100])), {"address": contact_email}))
        if rnd.random() < 0.05:
            events.append(("thank_you_sent", made + timedelta(days=6), {"to": contact_email, "thread_id": f"ty{i}"}))
        if rnd.random() < 0.05:
            events.append(("gmail_draft_created", made, "{not json"))
        if rnd.random() < 0.05:
            events.append(("gmail_draft_created", made, {"kind": "mystery", "draft_id": "x"}))
        if rnd.random() < 0.05:
            events.append(("gmail_draft_created", made, {"kind": "initial"}))
        if rnd.random() < 0.15:
            events.append(("reply_logged", made + timedelta(days=5), {"text": "x" * 400}))
        for number, (event_type, at, detail) in enumerate(events):
            text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True)
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, detail_json, created_at) VALUES(?,?,?,?,?,?,?)",
                (f"ev-{seed}-{i}-{number}", target_id, USER, event_type, text, "{}", at.isoformat(timespec="microseconds")),
            )
    conn.commit()
    return ids
