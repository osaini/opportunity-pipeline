"""Phase 5 M7: Apply for me attaches the approved cover letter for the role (spec D11 B, 6.9, 7.2 row L).

No browser and no network. What is proven here is the machinery around the agent: that the runner reads the approved letter's
PDF (rendered again when the stored one was made from other text), hands it to the agent with the hashes that name it, and
answers the agent's last question before the file goes in (is this still the latest version, approved, with this text?). The
agent's own attach, in a real Chromium, is tests/test_apply_agent_browser.py.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_fake_ats
from apply_fake_ats import FakeApplyAgentFactory, FakeSchemaClient, JOB_URL
from helpers_platform import build_and_migrate
from test_apply_api import seed_student
from test_apply_runner import ACME, GRACE, USER, Recorder, job, run_supervised

from opportunity_app.apply import policy as apply_policy, runner as apply_runner, runner_child as apply_runner_child
from opportunity_app.apply.agent_types import OP_FILE_CHECK, OP_FILE_CHECK_REPLY, ApplyTimeouts, FilePayload, RunResult
from opportunity_app.apply.runner import ApplyRunner
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now
from opportunity_app.student import artifacts as document_artifacts

LETTER = "# Cover letter\n\nDear Hiring Team,\n\nI would like to work on your robot arms.\n\nSincerely,\nSam Rivera\n"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LetterCase(unittest.TestCase):
    """A throwaway database with a confirmed student, the saved Acme role made a Greenhouse role whose listing needs a cover letter."""

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        _, self.path = build_and_migrate(self.root)
        self.conn = connect_product(self.path)
        self.addCleanup(self.conn.close)
        seed_student(self.conn, self.root / "resumes")
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (JOB_URL, ACME))
        patch = mock.patch.dict(apply_fake_ats.CANNED, {"letter_required": True})
        patch.start()
        self.addCleanup(patch.stop)
        self.apply_root = self.root / "apply"
        self.schema = FakeSchemaClient(any_job=True)
        self.runner = ApplyRunner(cancel_grace_s=GRACE)
        self.addCleanup(self.runner.shutdown, 10)

    def letter(self, version=1, status="approved", content=LETTER, conn=None):
        stamp = utc_now()
        (conn or self.conn).execute(
            "INSERT INTO generated_documents(id, user_id, opportunity_id, document_type, version, content, evidence_json, status, approved_at, "
            "created_at, updated_at) VALUES(?, ?, ?, 'cover_letter', ?, ?, ?, ?, ?, ?, ?)",
            (f"doc-{version}", USER, ACME, version, content, json.dumps([{"profile_field": "name", "value": "Sam", "source": "confirmed_profile"}]),
             status, stamp if status == "approved" else None, stamp, stamp),
        )
        (conn or self.conn).commit()
        return f"doc-{version}"

    def start(self, factory, kind="rehearsal"):
        return self.runner.start(
            self.conn, database_target=self.path, user_id=USER, opportunity_id=ACME, kind=kind, agent_factory=factory, schema_client=self.schema,
            apply_root=self.apply_root, resume_root=self.root / "resumes", posting_confirmed=True,
        )

    def finish(self, run_id):
        self.assertTrue(self.runner.wait(run_id, 60), "the run did not finish")
        return dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())


class ProbeFactory:
    """A thread agent that keeps what it was given, and asks the runner about the letter the way the real agent does."""

    isolation = "thread"

    def __init__(self, between=None):
        self.seen = {}
        self.between = between or (lambda step: None)

    def available(self):
        return ""

    def __call__(self, **kwargs):
        factory = self

        class Agent:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, plan, *, page_url, schema, files, lookup=None, replan=None, hand_over=None, cancelled=None, link=None, check_file=None):
                factory.seen.update(files=dict(files), check_file=check_file)
                entry = next((item for item in plan.fields if item.source.kind == "cover_letter"), None)
                factory.seen["entry"] = entry
                if entry is not None and check_file is not None:
                    args = ("cover_letter", entry.source.ref, entry.file_sha256)
                    factory.seen["answers"] = [check_file(*args)]
                    factory.between("after the first ask")
                    factory.seen["answers"].append(check_file(*args))
                    factory.seen["wrong_sha"] = check_file("cover_letter", entry.source.ref, "0" * 64)
                    factory.seen["wrong_ref"] = check_file("cover_letter", "doc-9@9", entry.file_sha256)
                    factory.seen["no_version"] = check_file("cover_letter", "doc-1", entry.file_sha256)
                return RunResult("rehearsed", ["probe"])

        return Agent()


class RunnerReadsTheLetterTests(LetterCase):
    def test_the_approved_letter_reaches_the_agent_as_a_pdf_with_the_hashes_that_name_it(self):
        self.letter(1)
        factory = ProbeFactory()
        self.finish(self.start(factory))
        payload = factory.seen["files"]["cover_letter"]
        self.assertIsInstance(payload, FilePayload)
        role = dict(self.conn.execute("SELECT company, title FROM opportunities WHERE id=?", (ACME,)).fetchone())
        self.assertEqual(payload.name, document_artifacts.document_file_name({**role, "document_type": "cover_letter", "version": 1}))
        self.assertTrue(payload.buffer.startswith(b"%PDF"))
        self.assertEqual((payload.mime_type, payload.sha256), ("application/pdf", hashlib.sha256(payload.buffer).hexdigest()))
        self.assertEqual(payload.content_sha256, digest(LETTER))
        entry = factory.seen["entry"]
        self.assertEqual((entry.source.ref, entry.file_sha256), ("doc-1@1", digest(LETTER)), "the plan names the same text the file was made from")
        stored = self.conn.execute("SELECT filename, content_sha256, sha256 FROM generated_document_artifacts WHERE document_id='doc-1'").fetchone()
        self.assertEqual((stored["filename"], stored["content_sha256"], stored["sha256"]), (payload.name, digest(LETTER), payload.sha256))
        self.assertTrue(payload.name.endswith("-cover_letter-v1.pdf"), "the employer sees a name with the role and the version, never a storage name")
        self.assertNotIn("document-artifact", payload.name)

    def test_the_agent_may_ask_again_and_is_told_yes_only_while_it_is_the_latest_approved_version_with_this_text(self):
        self.letter(1)
        other = connect_product(self.path)
        self.addCleanup(other.close)

        def between(_step):
            # After the first ask: the student edits the letter (it goes back to a draft), then approves it again with other words.
            other.execute("UPDATE generated_documents SET content='Dear Hiring Team,\n\nOther words.\n', status='approved' WHERE id='doc-1'")
            other.commit()

        factory = ProbeFactory(between=between)
        self.finish(self.start(factory))
        self.assertEqual(factory.seen["answers"], [True, False], "the same version with other text is not the letter the plan was made from")
        self.assertFalse(factory.seen["wrong_sha"])
        self.assertFalse(factory.seen["wrong_ref"])
        self.assertFalse(factory.seen["no_version"], "a ref without a version is nothing the runner knows")

    def test_a_newer_draft_or_an_unapproval_ends_the_yes(self):
        for label, change in (
            ("a newer draft", lambda c: c.execute(
                "INSERT INTO generated_documents(id, user_id, opportunity_id, document_type, version, content, evidence_json, status, created_at, updated_at) "
                "VALUES('doc-2', 'local-user', 'job-a', 'cover_letter', 2, 'v2', '[]', 'draft', '2026-10-08T00:00:00+00:00', '2026-10-08T00:00:00+00:00')")),
            ("a newer approved one", lambda c: c.execute(
                "INSERT INTO generated_documents(id, user_id, opportunity_id, document_type, version, content, evidence_json, status, created_at, updated_at) "
                "VALUES('doc-2', 'local-user', 'job-a', 'cover_letter', 2, 'v2', '[]', 'approved', '2026-10-08T00:00:00+00:00', '2026-10-08T00:00:00+00:00')")),
            ("an edit", lambda c: c.execute("UPDATE generated_documents SET status='draft' WHERE id='doc-1'")),
            ("a delete", lambda c: c.execute("DELETE FROM generated_documents WHERE id='doc-1'")),
        ):
            with self.subTest(label):
                with self.conn:
                    self.conn.execute("DELETE FROM generated_document_artifacts")
                    self.conn.execute("DELETE FROM generated_documents")
                self.letter(1)
                other = connect_product(self.path)
                self.addCleanup(other.close)

                def between(_step, change=change, other=other):
                    change(other)
                    other.commit()

                factory = ProbeFactory(between=between)
                self.finish(self.start(factory))
                self.assertEqual(factory.seen["answers"], [True, False])

    def test_an_edit_that_left_the_old_pdf_behind_does_not_reach_the_agent(self):
        self.letter(1)
        old = document_artifacts.ensure_document_artifact(self.conn, "doc-1", self.root / "resumes", user_id=USER)
        # The edit committed (and the student approved the new words) but the delete of the old PDF never happened.
        with self.conn:
            self.conn.execute("UPDATE generated_documents SET content=? WHERE id='doc-1'", ("Dear Hiring Team,\n\nNew words.\n",))
        factory = ProbeFactory()
        self.finish(self.start(factory))
        payload = factory.seen["files"]["cover_letter"]
        self.assertEqual(payload.content_sha256, digest("Dear Hiring Team,\n\nNew words.\n"))
        self.assertNotEqual(payload.sha256, old["sha256"])
        self.assertEqual(factory.seen["answers"], [True, True])

    def test_nothing_approved_gives_the_agent_no_letter_and_no_check(self):
        for label, prepare in (("no letter at all", lambda: None), ("only a draft", lambda: self.letter(1, "draft"))):
            with self.subTest(label):
                with self.conn:
                    self.conn.execute("DELETE FROM generated_documents")
                prepare()
                factory = ProbeFactory()
                self.finish(self.start(factory))
                self.assertNotIn("cover_letter", factory.seen["files"])
                self.assertIsNone(factory.seen["check_file"], "an agent with no letter to attach is not given the means to ask")

    def test_a_letter_that_cannot_be_rendered_leaves_no_payload_and_the_run_goes_on(self):
        self.letter(1)
        factory = ProbeFactory()
        with mock.patch.object(document_artifacts, "_render_pdf", side_effect=RuntimeError("PDF export needs the pinned reportlab web dependency")):
            row = self.finish(self.start(factory))
        self.assertEqual(row["outcome"], "rehearsed")
        self.assertNotIn("cover_letter", factory.seen["files"])
        self.assertIn("resume", factory.seen["files"], "the résumé does not wait for the letter")

    def test_the_rehearsal_names_the_version_and_counts_the_attached_letter_as_filled(self):
        self.letter(1)
        self.letter(2)
        row = self.finish(self.start(FakeApplyAgentFactory(step_delay=0)))
        view = apply_runner.run_view(self.conn, row)
        letter = next(item for item in view["fields"] if item["key"] == "cover_letter")
        self.assertEqual((letter["disposition"], letter["source_text"], letter["disposition_text"]), ("fill", "Approved cover letter, version 2", "Filled in the rehearsal"))
        plan = {item["key"]: item for item in json.loads(row["plan_json"])}
        self.assertEqual((plan["cover_letter"]["source"]["ref"], plan["cover_letter"]["file_sha256"]), ("doc-2@2", digest(LETTER)))
        for secret in ("Dear Hiring Team", "robot arms"):
            self.assertNotIn(secret, json.dumps({key: row[key] for key in row}), "the row holds none of the letter's words")

    def test_the_preview_shows_the_name_and_all_of_the_letter_and_notices_a_change(self):
        self.letter(1)
        row = self.finish(self.start(FakeApplyAgentFactory(step_delay=0)))
        values = apply_policy.preview_values(self.conn, USER, row, key=apply_policy.mac_key(self.apply_root), storage_root=self.root / "resumes")
        shown = values["cover_letter"]
        self.assertEqual((shown["changed"], shown["available"], shown["shown"], shown["body"]), (False, True, True, LETTER))
        self.assertTrue(shown["text"].endswith("-cover_letter-v1.pdf"))
        with self.conn:
            self.conn.execute("UPDATE generated_documents SET content='Dear Hiring Team,\n\nOther words.\n' WHERE id='doc-1'")
        changed = apply_policy.preview_values(self.conn, USER, row, key=apply_policy.mac_key(self.apply_root), storage_root=self.root / "resumes")["cover_letter"]
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["body"], "Dear Hiring Team,\n\nOther words.\n", "a rehearsal shows today's letter, flagged as changed")
        with self.conn:
            self.conn.execute("UPDATE generated_documents SET status='draft' WHERE id='doc-1'")
        gone = apply_policy.preview_values(self.conn, USER, row, key=apply_policy.mac_key(self.apply_root), storage_root=self.root / "resumes")["cover_letter"]
        self.assertEqual((gone["available"], gone["text"], gone.get("body")), (False, "", None))


class PipeTests(unittest.TestCase):
    """The question the agent asks the runner just before it attaches the letter, across the pipe."""

    def payload(self):
        return FilePayload(name="letter.pdf", mime_type="application/pdf", buffer=b"%PDF-1.4 x", sha256="a" * 64, content_sha256="b" * 64)

    def run_with(self, answer, files):
        asked, given = [], {}

        class Factory:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                class Agent:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return None

                    def run(self, plan, **more):
                        given.update(more)
                        if more.get("check_file") is not None:
                            given["answer"] = more["check_file"]("cover_letter", "doc-1@2", "b" * 64)
                        return RunResult("rehearsed", ["probe"])

                return Agent()

        def check(key, ref, sha):
            asked.append((key, ref, sha))
            return answer(key, ref, sha)

        recorder = Recorder()
        handlers = recorder.handlers()
        handlers.check_file = check
        outcome = run_supervised(Factory(), job=job(files=files), handlers=handlers)
        self.assertEqual(outcome.stop, "")
        return asked, given

    def test_the_agent_is_given_the_question_only_when_it_has_a_letter_and_the_answer_is_the_runners(self):
        asked, given = self.run_with(lambda key, ref, sha: True, {"cover_letter": self.payload()})
        self.assertEqual((asked, given["answer"]), ([("cover_letter", "doc-1@2", "b" * 64)], True))
        asked, given = self.run_with(lambda key, ref, sha: False, {"cover_letter": self.payload()})
        self.assertEqual((asked, given["answer"]), ([("cover_letter", "doc-1@2", "b" * 64)], False))
        asked, given = self.run_with(lambda key, ref, sha: True, {"resume": self.payload()})
        self.assertEqual((asked, given.get("check_file")), ([], None), "a résumé alone needs no check, and an agent written without it still runs")

    def test_anything_but_an_explicit_yes_is_a_no(self):
        def broken(key, ref, sha):
            raise ValueError("a message that must not cross")

        for label, answer in (("raises", broken), ("truthy but not True", lambda key, ref, sha: "yes"), ("None", lambda key, ref, sha: None)):
            with self.subTest(label):
                _asked, given = self.run_with(answer, {"cover_letter": self.payload()})
                self.assertIs(given["answer"], False)

    def test_a_supervisor_with_no_handler_answers_no(self):
        outcome_given = {}

        class Factory:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                class Agent:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return None

                    def run(self, plan, **more):
                        outcome_given["answer"] = more["check_file"]("cover_letter", "doc-1@2", "b" * 64)
                        return RunResult("rehearsed", [])

                return Agent()

        outcome = run_supervised(Factory(), job=job(files={"cover_letter": self.payload()}))
        self.assertEqual((outcome.stop, outcome_given["answer"]), ("", False))

    def test_a_closed_pipe_is_a_no_at_once(self):
        sent = []

        class Closed:
            def send(self, message):
                sent.append(message)
                raise OSError("closed")

            def recv(self):
                raise EOFError

            def close(self):
                return None

        channel = apply_runner_child.ChildChannel(Closed(), Closed(), ApplyTimeouts(reply_s=0.2))
        self.assertIs(channel.check_file("cover_letter", "doc-1@2", "b" * 64), False)
        self.assertEqual([(item["op"], item["key"], item["ref"], item["sha256"]) for item in sent], [(OP_FILE_CHECK, "cover_letter", "doc-1@2", "b" * 64)])
        self.assertNotEqual(OP_FILE_CHECK, OP_FILE_CHECK_REPLY)


if __name__ == "__main__":
    unittest.main()
