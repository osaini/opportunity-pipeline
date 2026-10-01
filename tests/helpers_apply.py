"""Shared fixtures for the apply-for-me tests: a throwaway database cloned from one migrated template per module, and the fictional postings and claims built on it.

Not a test module: a test module that wants the template imports setUpModule and tearDownModule from here, which unittest and pytest then run for it."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from opportunity_app import SERVER_INSTANCE, actions, apply_preflight, apply_runs, apply_sensitive, automation, preparation
from opportunity_app.apply_checks import question_key
from opportunity_app.apply_policy import SchemaField, Sources, build_plan
from opportunity_app.apply_runs import company_key
from opportunity_app.apply_sensitive import StoreRefused, add_entry
from opportunity_app.profile import update_profile
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate


USER = "local-user"
BLUEFIN = "Bluefin Robotics"


_TEMPLATE_DIR = None
_TEMPLATE_DB = None


def setUpModule():
    """Migrate one database for the whole module; every test starts from its own copy, which is much faster."""
    global _TEMPLATE_DIR, _TEMPLATE_DB
    _TEMPLATE_DIR = tempfile.TemporaryDirectory()
    _, _TEMPLATE_DB = build_and_migrate(Path(_TEMPLATE_DIR.name))


def tearDownModule():
    _TEMPLATE_DIR.cleanup()


class ApplyCase(unittest.TestCase):
    """A throwaway database, a clock near the wall clock, and helpers for fictional postings and claims."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.path = self.root / "platform.db"
        shutil.copyfile(_TEMPLATE_DB, self.path)
        self.conn = connect_product(self.path)
        self.addCleanup(self.conn.close)
        # Times are near the wall clock, as everything else in the database is, so a "day old" application is one.
        self.base = datetime.now(timezone.utc).replace(microsecond=0)
        apply_runs.RUNNING.clear()
        apply_runs.RUNNING_RUNS.clear()
        self.addCleanup(apply_runs.RUNNING.clear)
        self.addCleanup(apply_runs.RUNNING_RUNS.clear)
        env = mock.patch.dict(os.environ, {"PIPELINE_TIMEZONE": "UTC"})
        env.start()
        self.addCleanup(env.stop)
        self.companies = {}
        self.serial = 0

    # --- fictional postings and claims

    def at(self, minutes=0, **kwargs):
        return self.base + timedelta(minutes=minutes, **kwargs)

    def opportunity(self, opportunity_id, company=BLUEFIN, title="Controls Intern"):
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity_id, company, title, f"https://boards.example.test/{opportunity_id}", stamp, stamp, stamp, stamp),
            )
        self.companies[opportunity_id] = company
        return opportunity_id

    def start(self, opportunity_id, mode="handoff", *, job=None, board="bluefin", now=None, **kwargs):
        """apply_runs.claim for a fictional posting, opening the posting first when it is new."""
        if opportunity_id not in self.companies:
            self.opportunity(opportunity_id)
        return apply_runs.claim(
            self.conn, user_id=USER, opportunity_id=opportunity_id, mode=mode, ats="greenhouse", board_token=board,
            job_ref=job or f"{board}/{opportunity_id}", company=company_key(self.companies[opportunity_id]), now=now, **kwargs,
        )

    def raw_claim(self, *, state="submitted", mode="one_click", handed_over_at=None, after_click=None, company=BLUEFIN, board="bluefin",
                  instance=SERVER_INSTANCE, heartbeat_at=None, stage_policy="record", token=None, verification="", stage_recorded=0,
                  updated_at=None, submitted_at=None, note="", job_ref=None, confirmed_at=None, detail=None):
        """A claim row written directly, in whatever state a test needs, with a posting and an application of its own."""
        self.serial += 1
        opportunity_id = f"raw-{self.serial}"
        self.opportunity(opportunity_id, company)
        stamp = updated_at or utc_now()
        token = token or f"tok-{self.serial}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?, ?, ?, 'applying', ?, ?)",
                (f"app-{opportunity_id}", opportunity_id, USER, stamp, stamp),
            )
            self.conn.execute(
                """
                INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, after_click, ats,
                    board_token, job_ref, company_key, stage_policy, plan_hash, handed_over_at, heartbeat_at, verification, stage_recorded,
                    submitted_at, note, confirmed_at, detail_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'greenhouse', ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (token, f"app-{opportunity_id}", USER, opportunity_id, instance, mode, state,
                 1 if handed_over_at is not None and after_click is None else (after_click or 0), board,
                 job_ref or f"{board}/{opportunity_id}", company_key(company), stage_policy, handed_over_at, heartbeat_at or stamp,
                 verification, stage_recorded, submitted_at, note, confirmed_at, json.dumps(detail or {}), stamp, stamp),
            )
        return token

    def claim_row(self, token):
        return dict(self.conn.execute("SELECT * FROM application_submit_claims WHERE token=?", (token,)).fetchone())

    def stage(self, opportunity_id):
        row = self.conn.execute("SELECT stage, applied_at FROM applications WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        return None if row is None else (row["stage"], row["applied_at"])

    def events(self, application_id):
        return [row["event_type"] for row in self.conn.execute(
            "SELECT event_type FROM application_events WHERE application_id=? ORDER BY id", (application_id,)).fetchall()]

    def notices(self):
        return [row["title"] for row in automation.list_notices(self.conn, USER, limit=50)]

    def set_limits(self, **values):
        profile = {"apply_agent": values}
        with self.conn:
            self.conn.execute(
                "INSERT INTO profiles(user_id, profile_json, created_at, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json",
                (USER, json.dumps(profile), utc_now(), utc_now()),
            )

    def make_run(self, kind="rehearsal", *, company=BLUEFIN, opportunity_id="op-run", started=None, **kwargs):
        return apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=opportunity_id, kind=kind, started_by="student", ats="greenhouse", board_token="bluefin",
            page_url="https://boards.example.test/bluefin/1", company=company_key(company), deadline_seconds=300,
            now=started or self.at(), **kwargs,
        )

    def reviewed_rehearsal(self, company, minutes, *, verdict="right", clean=True, outcome="rehearsed"):
        run_id = self.make_run(company=company, started=self.at(minutes))
        apply_runs.finish_run(self.conn, run_id, outcome=outcome, clean=clean, now=self.at(minutes, seconds=30))
        apply_runs.mark_review(self.conn, run_id, user_id=USER, verdict=verdict, now=self.at(minutes, seconds=40))
        return run_id


COMPANY = "Example Robotics"
OTHER = "Orbit Systems"


def F(name, label, kind="input_text", *, required=True, options=(), section="custom", parent=""):
    return SchemaField(name=name, label=label, required=required, type=kind, options=tuple(options), section=section, parent=parent)


SINGLE = "multi_value_single_select"
MULTI = "multi_value_multi_select"


RESUME_OK = {"kind": "confirmed", "version_id": "v1", "label": "Your confirmed résumé", "original_name": "Sam Rivera Resume.pdf",
             "sha256": "a" * 64, "problem_kind": "", "problem": ""}
LETTER_NONE = {"problem_kind": "cover_letter_missing", "problem": "No cover letter is approved for this role. Draft one"}
LETTER_OK = {"document_id": "doc-1", "version": 2, "content_sha256": "b" * 64, "problem_kind": "", "problem": ""}
FACTS = {"name": "Sam Rivera", "contact": {"email": "sam.rivera@example.test", "phone": "555-0100"}}
KEY = b"k" * 32
BASE = [
    F("first_name", "First Name", section="standard"),
    F("last_name", "Last Name", section="standard"),
    F("email", "Email", section="standard"),
    F("resume", "Resume/CV", "input_file", section="standard"),
]


def answer(question, text, company=COMPANY, tags=(), answer_id=None):
    return {"id": answer_id or f"a-{abs(hash((question, text, company))) % 10**6}", "question": question, "answer": text,
            "company": company, "tags": list(tags), "updated_at": ""}


class Store:
    """The sensitive-answers store as spec 5.4 will hold it: exact key, category, company '' or this one."""

    def __init__(self, *entries):
        self.entries = entries

    def __call__(self, *, category, question_key, company_key, mode, company_only=False):
        for entry in self.entries:
            if entry["category"] == category and entry["question_key"] == question_key and entry.get("company_key", "") in ("", company_key):
                if company_only and not entry.get("company_key", ""):
                    continue
                return entry
        return None


def entry(category, question, text, kind="option", company_key="", entry_id="s1"):
    return {"id": entry_id, "category": category, "question_key": question_key(question), "answer_kind": kind, "answer": text, "company_key": company_key}


def sources(*, facts=None, answers=(), labels=None, allowed=(), store=None, resume=None, letter=None):
    return Sources(
        facts=copy.deepcopy(FACTS if facts is None else facts), answers=list(answers), ats_labels=dict(labels or {}),
        sensitive_allowed=frozenset(allowed), sensitive_lookup=store or Store(), resume=resume or RESUME_OK,
        cover_letter=letter or LETTER_NONE, mac_key=KEY,
    )


def plan(fields, src=None, mode="submit", company=COMPANY, **kwargs):
    return build_plan(fields, kwargs.pop("scan", None), src or sources(), company, mode, **kwargs)


def kinds(result):
    return {problem.key: problem.kind for problem in result.problems}


class ResumeCase(ApplyCase):
    """The résumé to use, from the database (6.9)."""

    def setUp(self):
        super().setUp()
        self.resumes = self.root / "resumes"
        self.resumes.mkdir()
        self.serial_file = 0

    def add_resume(self, *, confirmed=True, label="", name="Resume.pdf", data=None, variant_at=None):
        self.serial_file += 1
        data = data if data is not None else f"%PDF-1.4 fictional {self.serial_file}".encode()
        file_id, version_id = f"file-{self.serial_file}", f"ver-{self.serial_file}"
        stored = f"{file_id}.pdf"
        (self.resumes / stored).write_bytes(data)
        stamp = variant_at or utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at, variant_label) VALUES(?, ?, ?, 'application/pdf', ?, ?, ?, ?, ?)",
                (file_id, USER, name, len(data), __import__("hashlib").sha256(data).hexdigest(), stored, stamp, label),
            )
            self.conn.execute(
                "INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, status, created_at, confirmed_at) VALUES(?, ?, ?, 'text', ?, ?, ?)",
                (version_id, file_id, USER, "confirmed" if confirmed else "draft", stamp, stamp if confirmed else None),
            )
        return file_id, version_id

    def pick(self, opportunity_id, file_id, by="student", status="picked"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunity_resume_picks(user_id, opportunity_id, resume_file_id, picked_by, matched_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (USER, opportunity_id, file_id, by, json.dumps({"status": status, "matched": [], "reason": ""}), utc_now(), utc_now()),
            )


class StaticClient:
    def __init__(self, listing):
        self.listing = listing
        self.calls = 0

    def fetch(self, board_token, job_id):
        self.calls += 1
        return copy.deepcopy(self.listing)


SIMPLE = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Last Name", "required": True, "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file", "values": []}, {"name": "resume_text", "type": "textarea", "values": []}]},
        {"label": "Why do you want to work at Bluefin Robotics?", "required": True, "fields": [{"name": "question_1", "type": "textarea", "values": []}]},
        {"label": "Which team are you most interested in?", "required": True,
         "fields": [{"name": "question_2", "type": SINGLE, "values": [{"label": "Perception", "value": 1}, {"label": "Controls", "value": 2}]}]},
    ],
    "location_questions": [], "compliance": [], "demographic_questions": None, "data_compliance": [],
}
JOB = "https://job-boards.greenhouse.io/bluefin/jobs/4000000001"


class PolicyCase(ResumeCase):
    """A Greenhouse role the student saved, a confirmed profile and résumé, and a listing served from memory."""

    def setUp(self):
        super().setUp()
        # A fixed noon, so nothing here straddles a day boundary.
        self.base = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        update_profile(self.conn, {"name_parts": {"first": "Sam", "last": "Rivera", "preferred": ""},
                                   "contact": {"email": "sam.rivera@example.test", "phone": "555-0100"}}, ["name_parts", "contact"], user_id=USER)
        self.add_resume(name="Sam Rivera Resume.pdf")
        self.client = StaticClient(SIMPLE)

    def role(self, opportunity_id="gh-1", company=BLUEFIN, job="4000000001", saved=True):
        self.opportunity(opportunity_id, company)
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (f"https://job-boards.greenhouse.io/bluefin/jobs/{job}", opportunity_id))
        if saved:
            actions.record_intent(self.conn, opportunity_id, "saved", user_id=USER)
        return opportunity_id

    def answers_for(self, company=BLUEFIN):
        preparation.save_answer(self.conn, "Why do you want to work at Bluefin Robotics?", "I build robot arms", company, [], user_id=USER)
        preparation.save_answer(self.conn, "Which team are you most interested in?", "Controls", company, [], user_id=USER)

    def run_check(self, opportunity_id="gh-1", **kwargs):
        return apply_preflight.check(self.conn, USER, opportunity_id, client=kwargs.pop("client", self.client), cache=kwargs.pop("cache", None),
                                     resume_root=self.resumes, now=kwargs.pop("now", self.at(0)), **kwargs)

    def clean_rehearsals(self, count):
        for index in range(count):
            self.reviewed_rehearsal(f"Company {index}", index * 5)


ACCURATE = "I certify that the information I have provided is accurate"


class StoreCase(ApplyCase):
    """A throwaway database in which the student has switched the kinds of answer on that a test needs."""

    def allow(self, *categories):
        apply_sensitive.set_allowed_categories(self.conn, USER, categories)

    def add(self, **kwargs):
        kwargs.setdefault("consent", True)
        return add_entry(self.conn, USER, **kwargs)

    def rows(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM apply_sensitive_answers ORDER BY created_at, id").fetchall()]

    def refused(self, needle, **kwargs):
        with self.assertRaises(StoreRefused) as caught:
            self.add(**kwargs)
        self.assertIn(needle, str(caught.exception))
        return caught.exception


def planned(key, question, value, *, required=True, disposition="fill", control="text", source="profile", **extra):
    return {"key": key, "question": question, "value": value, "required": required, "disposition": disposition,
            "control": control, "source": {"kind": source, "ref": "x"}, **extra}


@dataclass
class FakePlan:
    fields: list = field(default_factory=list)
    plan_hash: str = "hash-1"
