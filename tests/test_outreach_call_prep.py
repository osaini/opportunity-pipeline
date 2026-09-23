"""Call prep notes for outreach targets that wrote back."""

import json
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.outreach import confirm_research, create_target, get_target, log_reply, update_target
from opportunity_app.operations import enqueue_job, run_next_job
from opportunity_app.outreach_call_prep import (
    DURING_CALL, JOB_TYPE, CallPrepRejected, CallPrepWorker, ReplyRequired, auto_queue_call_prep,
    generate_call_prep, queue_call_prep,
)
from opportunity_app.schema import connect_product, ensure_product_schema
from opportunity_app.worker import WEB_APP_JOB_TYPES

from helpers_platform import build_and_migrate
from test_outreach_drafting import AUTH, USER, ScriptedProvider, confirm_facts

REPLY = "Thanks for writing! Could we set up a call Thursday at 3pm? I'd like to hear about your drone work."
EXPERIENCE = [
    {
        "organization": "Dronelab", "role": "Engineering Intern", "outreach": "lead",
        "highlights": ["Cut drone weight from 1.5 kg to 500 g"],
    },
    {
        "organization": "Secret Co", "role": "Software Intern", "outreach": "omit",
        "highlights": ["Indexed 250,000 records"],
    },
]


def prep_json(**overrides):
    sections = {
        "about_them": [{"text": "Robots that charge parked EVs", "basis": "research:summary"}],
        "their_reply": [{"text": "Wants a call Thursday at 3pm", "basis": "reply"}],
        "talking_points": [{"text": "Cut our drone's weight from 1.5 kg to 500 g", "basis": "profile:experience"}],
        "what_i_bring": [{"text": "Sensor mounting on test robots", "basis": "inference"}],
        "questions": ["How does the robot find the car?", "What would a first project be?"],
    }
    sections.update(overrides)
    return json.dumps(sections)


class CallPrepTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        confirm_facts(self.conn, name="Test Student", school="UT Austin", degree="Mechanical Engineering", experience=EXPERIENCE)
        self.target = create_target(self.conn, {
            "company": "Chargebot",
            "website": "https://chargebot.example",
            "summary": "Robots that charge parked EVs",
            "contact_email": "hi@chargebot.example",
            "source_urls": ["https://chargebot.example/about"],
            "email_subject": "Drone builder", "email_body": "I cut drone weight from 1.5 kg to 500 g.",
        }, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": "sent"}, user_id=USER)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def reply_and_mark(self, status="call_scheduled"):
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": status}, user_id=USER)

    def generate(self, provider):
        return generate_call_prep(self.conn, self.target["id"], user_id=USER, provider_factory=lambda *_: provider, provider="anthropic")

    def test_grounded_notes_are_stored_as_easy_to_fill_bullets(self):
        self.reply_and_mark()
        provider = ScriptedProvider([prep_json()])
        target = self.generate(provider)
        notes = target["call_prep"]
        for heading in ("ABOUT THEM", "WHAT THEY SAID", "TALKING POINTS", "WHAT I CAN BRING", "QUESTIONS TO ASK", "DURING THE CALL"):
            self.assertIn(heading, notes)
        self.assertIn("- Sensor mounting on test robots (my guess)", notes, "inference is labelled as a guess")
        self.assertIn("- Cut our drone's weight from 1.5 kg to 500 g\n", notes, "a fact is not")
        for blank in DURING_CALL:
            self.assertIn(f"- {blank}", notes)
        self.assertEqual(target["call_prep_generated_by"], "anthropic:claude-sonnet-5")
        self.assertEqual({claim["section"] for claim in target["call_prep_claims"]}, {"about_them", "their_reply", "talking_points", "what_i_bring"})
        prompt = provider.prompts[0]
        self.assertIn("Thursday at 3pm", prompt, "the logged reply reaches the model")
        self.assertIn("Dronelab", prompt)
        self.assertNotIn("Secret Co", prompt, "an entry marked omit never reaches the model")

    def test_notes_wait_for_a_reply_status(self):
        with self.assertRaisesRegex(ValueError, "replied"):
            self.generate(ScriptedProvider([prep_json()]))

    def test_an_unsupported_number_or_basis_is_retried_then_refused(self):
        self.reply_and_mark("replied")
        bad = prep_json(talking_points=[{"text": "Flew 900 hours", "basis": "profile:experience"}])
        bad_basis = prep_json(about_them=[{"text": "Raised a Series B", "basis": "research:funding"}])
        provider = ScriptedProvider([bad, bad_basis])
        with self.assertRaises(CallPrepRejected) as caught:
            self.generate(provider)
        self.assertIn("research:funding", str(caught.exception))
        self.assertIn("900", provider.prompts[1], "the retry says what was wrong")
        self.assertEqual(get_target(self.conn, self.target["id"], user_id=USER)["call_prep"], "", "nothing is stored")

    def test_only_what_i_bring_may_rest_on_inference(self):
        self.reply_and_mark("replied")
        guessed = prep_json(about_them=[{"text": "They are hiring interns", "basis": "inference"}])
        provider = ScriptedProvider([guessed, prep_json()])
        target = self.generate(provider)
        self.assertIn("about_them rests", provider.prompts[1])
        self.assertNotIn("hiring interns", target["call_prep"])

    def test_the_reply_must_be_summarized_once_logged(self):
        self.reply_and_mark()
        provider = ScriptedProvider([prep_json(their_reply=[]), prep_json()])
        self.generate(provider)
        self.assertIn("leaves out what the reply said", provider.prompts[1])

    def test_notes_need_a_logged_reply(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        provider = ScriptedProvider([prep_json(their_reply=[])])
        with self.assertRaisesRegex(ReplyRequired, "Paste their reply"):
            self.generate(provider)
        with self.assertRaises(ReplyRequired):
            queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="test")
        self.assertEqual(provider.prompts, [], "the model is never asked")

    def test_regenerating_keeps_the_replaced_notes_in_history(self):
        self.reply_and_mark()
        self.generate(ScriptedProvider([prep_json()]))
        update_target(self.conn, self.target["id"], {"call_prep": "My own notes from the call"}, user_id=USER)
        self.generate(ScriptedProvider([prep_json()]))
        events = get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]
        replaced = [event for event in events if event["event_type"] == "call_prep_replaced"]
        self.assertEqual([event["detail"] for event in replaced], ["My own notes from the call"])

    def test_unverified_research_is_cited_as_unverified_until_confirmed(self):
        self.conn.execute("UPDATE outreach_targets SET research_confidence='unverified' WHERE id=?", (self.target["id"],))
        self.conn.commit()
        self.reply_and_mark()
        provider = ScriptedProvider([prep_json(), prep_json(about_them=[{"text": "Robots that charge parked EVs", "basis": "unverified:summary"}])])
        target = self.generate(provider)
        self.assertEqual(target["call_prep_claims"][0]["basis"], "unverified:summary")
        confirmed = confirm_research(self.conn, self.target["id"], user_id=USER)
        self.assertEqual(confirmed["call_prep_claims"][0]["basis"], "research:summary")

    def test_template_notes_use_facts_only(self):
        self.reply_and_mark()
        target = generate_call_prep(self.conn, self.target["id"], user_id=USER, provider_factory=None, provider="legacy")
        self.assertEqual(target["call_prep_generated_by"], "template")
        self.assertIn("Cut drone weight from 1.5 kg to 500 g", target["call_prep"])
        self.assertIn("(Write one or two", target["call_prep"], "no inference without a model")
        self.assertIn("Thursday at 3pm", target["call_prep"], "the reply is quoted as they wrote it")
        self.assertNotIn("Indexed", target["call_prep"])


class FailingProvider:
    """A model call cut off mid-flight, as a sleeping laptop cuts one off."""

    name = "anthropic"
    model = "test-model"

    def create(self, **_kwargs):
        raise RuntimeError("connection reset")


class CallPrepJobTests(unittest.TestCase):
    """Background jobs: automatic starts, retries, and recovery after a restart."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        confirm_facts(self.conn, name="Test Student", school="UT Austin", experience=EXPERIENCE)
        self.target = create_target(self.conn, {
            "company": "Chargebot", "summary": "Robots that charge parked EVs", "contact_email": "hi@chargebot.example",
        }, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": "sent"}, user_id=USER)
        self.provider = ScriptedProvider([prep_json(), prep_json()])

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def worker(self, provider=None):
        return CallPrepWorker(self.platform_path, provider_factory=lambda *_: provider or self.provider, provider="anthropic")

    def target_now(self):
        return get_target(self.conn, self.target["id"], user_id=USER)

    def test_the_web_app_and_maintenance_worker_share_one_job_type_name(self):
        self.assertEqual(WEB_APP_JOB_TYPES, (JOB_TYPE,))

    def test_a_reply_and_a_reply_status_start_it_once(self):
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "not replied yet")
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "no reply logged")
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        self.assertTrue(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"))
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "one job at a time")
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "queued")
        self.assertEqual(self.worker().run_pending(), 1)
        target = self.target_now()
        self.assertEqual(target["call_prep_job"]["state"], "succeeded")
        self.assertIn("TALKING POINTS", target["call_prep"])
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "notes exist")

    def test_a_cut_off_model_call_is_retried(self):
        update_target(self.conn, self.target["id"], {"status": "call_scheduled"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.worker(FailingProvider()).run_pending()
        job = self.target_now()["call_prep_job"]
        self.assertEqual((job["state"], job["attempts"]), ("retry", 1))
        self.assertIn("connection reset", job["error"])
        self.assertEqual(self.worker().run_pending(), 0, "the retry waits for its backoff")
        self.conn.execute("UPDATE job_queue SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE job_type=?", (JOB_TYPE,))
        self.conn.commit()
        self.worker().run_pending()
        target = self.target_now()
        self.assertEqual(target["call_prep_job"]["state"], "succeeded")
        self.assertIn("TALKING POINTS", target["call_prep"])

    def test_a_job_cut_off_by_a_restart_runs_again(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        # The server died mid-generation: the row still says running.
        self.conn.execute("UPDATE job_queue SET state='running', locked_at='2026-01-01T00:00:00+00:00' WHERE job_type=?", (JOB_TYPE,))
        self.conn.commit()
        worker = self.worker()
        self.assertEqual(worker.recover_interrupted(), 1)
        worker.run_pending()
        self.assertIn("TALKING POINTS", self.target_now()["call_prep"])

    def test_an_automatic_job_keeps_notes_saved_while_it_waited(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t")
        update_target(self.conn, self.target["id"], {"call_prep": "My own notes"}, user_id=USER)
        self.worker().run_pending()
        self.assertEqual(self.target_now()["call_prep"], "My own notes")
        self.assertEqual(self.provider.prompts, [])

    def test_a_job_whose_company_left_a_reply_status_writes_nothing(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        update_target(self.conn, self.target["id"], {"status": "declined"}, user_id=USER)
        self.worker().run_pending()
        target = self.target_now()
        self.assertEqual((target["call_prep_job"]["state"], target["call_prep"]), ("succeeded", ""))

    def test_the_thread_runs_jobs_it_is_woken_for(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        worker = self.worker()
        worker.start()
        self.addCleanup(worker.stop)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        worker.wake()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.target_now()["call_prep"]:
            time.sleep(0.05)
        self.assertIn("TALKING POINTS", self.target_now()["call_prep"])

    def test_the_two_workers_never_take_each_others_jobs(self):
        enqueue_job(self.conn, "someone_elses_job", {}, "other-job")
        self.assertIsNone(run_next_job(self.conn, {JOB_TYPE: lambda _payload: None}, only_handled=True))
        state = self.conn.execute("SELECT state FROM job_queue WHERE idempotency_key='other-job'").fetchone()[0]
        self.assertEqual(state, "queued")
        self.conn.execute("DELETE FROM job_queue")
        self.conn.commit()
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.assertIsNone(run_next_job(self.conn, {}, exclude_types=WEB_APP_JOB_TYPES), "the maintenance worker leaves it")
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "queued")


class CallPrepApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.provider = ScriptedProvider([prep_json(), prep_json()])
        app = create_app(
            db_path=self.platform_path,
            access_token="drafting-owner",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
            agent_provider_factory=lambda *_: self.provider,
            outreach_draft_provider="anthropic",
        )
        self.client = TestClient(app)
        self.client.__enter__()
        with closing(connect_product(self.platform_path)) as conn:
            confirm_facts(conn, name="Test Student", school="UT Austin", experience=EXPERIENCE)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def run_jobs(self):
        self.client.app.state.call_prep_worker.run_pending()

    def test_write_edit_and_refuse_before_a_reply(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Chargebot", "summary": "Robots that charge parked EVs", "contact_email": "hi@chargebot.example",
        }).json()
        early = self.client.post(f"/api/v1/outreach/{created['id']}/call-prep", headers=AUTH)
        self.assertEqual(early.status_code, 422, early.text)
        moved = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "replied"}).json()
        self.assertIsNone(moved["call_prep_job"], "nothing starts without a reply")
        self.assertEqual(moved["reply_count"], 0)
        no_reply = self.client.post(f"/api/v1/outreach/{created['id']}/call-prep", headers=AUTH)
        self.assertEqual(no_reply.status_code, 409, no_reply.text)
        self.assertIn("Paste their reply", no_reply.json()["detail"])

        logged = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": REPLY}).json()
        self.assertEqual(logged["target"]["call_prep_job"]["state"], "queued", "logging the reply starts it")
        self.run_jobs()
        written = self.client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()
        self.assertIn("TALKING POINTS", written["call_prep"])
        self.assertEqual(written["call_prep_job"]["state"], "succeeded")

        again = self.client.post(f"/api/v1/outreach/{created['id']}/call-prep", headers=AUTH)
        self.assertEqual(again.status_code, 202, again.text)
        self.assertEqual(again.json()["call_prep_job"]["state"], "queued")
        edited = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"call_prep": "Talked to the CTO\r\n- next: send a thank-you"})
        self.assertEqual(edited.json()["call_prep"], "Talked to the CTO\n- next: send a thank-you")
        missing = self.client.post("/api/v1/outreach/outreach-nope/call-prep", headers=AUTH)
        self.assertEqual(missing.status_code, 404)

    def test_moving_to_a_reply_status_with_a_reply_starts_it(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Chargebot", "summary": "Robots that charge parked EVs", "contact_email": "hi@chargebot.example",
        }).json()
        self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "sent"})
        logged = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": REPLY}).json()
        self.assertIsNone(logged["target"]["call_prep_job"], "still at Sent: no call prep yet")
        moved = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "call_scheduled"}).json()
        self.assertEqual(moved["call_prep_job"]["state"], "queued")
        self.assertEqual(moved["reply_count"], 1)

    def test_the_pane_offers_call_prep(self):
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("/call-prep`", script)
        self.assertIn('["prep", "Call prep"]', script)
        self.assertIn("window.confirm(", script[script.index("function askForReply"):])
        self.assertIn("visibilitychange", script)


if __name__ == "__main__":
    unittest.main()
