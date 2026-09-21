import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.typesafe_decisions import (
    TypeSafeClient,
    TypeSafeNotConfigured,
    TypeSafeResponseError,
    opportunity_review_questions,
    opportunity_review_state,
    review_opportunity,
)
from tests.helpers_platform import build_and_migrate


def response_for(questions, *, model="jev-1.13.0"):
    answers = {}
    for question_id, question in questions.items():
        if question["type"] == "score":
            levels = len(question["criteria"])
            probabilities = {
                str(index): (1.0 if index == levels - 1 else 0.0)
                for index in range(levels)
            }
            answers[question_id] = {
                "type": "score",
                "score": float(levels - 1),
                "legend": {
                    str(index): label for index, label in enumerate(question["criteria"])
                },
                "probabilities": probabilities,
                "confidence": 0.96,
            }
        else:
            options = list(question["criteria"])
            selected = options[0]
            answers[question_id] = {
                "type": "choice",
                "choice": selected,
                "probabilities": {
                    option: (1.0 if option == selected else 0.0) for option in options
                },
                "confidence": 0.94,
            }
    return {
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": 321, "output_tokens": 0},
    }


class RecordingDecisionClient:
    configured = True
    model = "jev-1.13.0"

    def __init__(self):
        self.calls = []

    def evaluate(self, *, state, questions):
        self.calls.append({"state": state, "questions": questions})
        return response_for(questions)


class TypeSafeClientTests(unittest.TestCase):
    def test_http_adapter_uses_bearer_auth_pinned_model_and_validates_response(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            payload = json.loads(request.content)
            return httpx.Response(200, json=response_for(payload["questions"]))

        client = TypeSafeClient(
            api_key="test-secret",
            model="jev-1.13.0",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )
        questions = opportunity_review_questions()
        result = client.evaluate(state={"document": "test"}, questions=questions)

        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertEqual(seen[0].headers["authorization"], "Bearer test-secret")
        self.assertEqual(json.loads(seen[0].content)["model"], "jev-1.13.0")

    def test_transient_overload_retries_and_bad_choices_are_rejected(self):
        attempts = []
        questions = opportunity_review_questions()

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(529, headers={"retry-after": "0"}, json={"detail": "busy"})
            payload = response_for(questions)
            payload["answers"]["education_compatibility"]["choice"] = "invented"
            return httpx.Response(200, json=payload)

        client = TypeSafeClient(
            api_key="test-secret",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _delay: None,
        )
        with self.assertRaises(TypeSafeResponseError):
            client.evaluate(state="test", questions=questions)
        self.assertEqual(len(attempts), 2)

    def test_missing_key_fails_before_network(self):
        client = TypeSafeClient(api_key="")
        with self.assertRaises(TypeSafeNotConfigured):
            client.evaluate(state="test", questions=opportunity_review_questions())

    def test_remote_base_url_must_keep_tls(self):
        with self.assertRaisesRegex(TypeSafeNotConfigured, "HTTPS"):
            TypeSafeClient(api_key="test-secret", base_url="http://api.example.test/v1")
        local = TypeSafeClient(api_key="test-secret", base_url="http://127.0.0.1:9999/v1")
        self.assertEqual(local.base_url, "http://127.0.0.1:9999/v1")

    def test_timeout_must_be_finite_and_positive(self):
        for timeout in (0, -1, float("nan"), float("inf"), 121):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(TypeSafeNotConfigured, "between 0 and 120"):
                    TypeSafeClient(api_key="test-secret", timeout=timeout)


class OpportunityReviewTests(unittest.TestCase):
    def test_state_has_an_allowlist_and_excludes_identity_contact_and_resume_prose(self):
        opportunity = {
            "company": "Acme",
            "title": "Mechanical Intern",
            "description": "Design fixtures with SolidWorks.",
            "url": "https://example.com/private-token",
            "notes": "private application note",
        }
        profile = {
            "name": "Private Student",
            "school": "Private University",
            "contact": {"email": "private@example.com"},
            "experience": [{"description": "full resume prose"}],
            "degree": "Mechanical Engineering",
            "skills": ["SolidWorks"],
        }
        state, disclosed = opportunity_review_state(opportunity, profile)
        encoded = json.dumps(state)
        self.assertEqual(disclosed, ["degree", "skills"])
        self.assertNotIn("Private Student", encoded)
        self.assertNotIn("Private University", encoded)
        self.assertNotIn("private@example.com", encoded)
        self.assertNotIn("full resume prose", encoded)
        self.assertNotIn("private-token", encoded)
        self.assertIn("SolidWorks", encoded)

    def test_citizenship_is_disclosed_including_a_false_answer(self):
        # "Not a citizen" is an eligibility fact the review needs; dropping a
        # False here would read to Jev as an unanswered question instead.
        opportunity = {"company": "Acme", "title": "Intern", "description": "US citizens only."}
        for answer in (True, False):
            state, disclosed = opportunity_review_state(
                opportunity, {"degree": "ME", "us_citizen": answer}
            )
            self.assertIn("us_citizen", disclosed)
            self.assertIs(state["student_profile"]["us_citizen"], answer)
        _, unanswered = opportunity_review_state(opportunity, {"degree": "ME"})
        self.assertNotIn("us_citizen", unanswered)

    def test_review_is_labeled_unconfirmed_and_preserves_probabilities(self):
        client = RecordingDecisionClient()
        result = review_opportunity(
            client,
            {"company": "Acme", "title": "Mechanical Intern", "description": "CAD work"},
            {"degree": "Mechanical Engineering", "skills": ["CAD"]},
        )
        self.assertEqual(result["kind"], "ai_suggestion")
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["changes_score"])
        self.assertEqual(result["model_resolved"], "jev-1.13.0")
        self.assertEqual(len(result["answers"]), 8)
        self.assertIn("probabilities", result["answers"]["role_alignment"])

    def test_score_label_uses_the_most_probable_level_not_rounded_expectation(self):
        class SplitClient(RecordingDecisionClient):
            def evaluate(self, *, state, questions):
                result = super().evaluate(state=state, questions=questions)
                result["answers"]["role_alignment"].update(
                    score=1.6,
                    probabilities={"0": 0.6, "1": 0.0, "2": 0.0, "3": 0.4},
                    confidence=0.2,
                )
                return result

        questions = opportunity_review_questions()
        result = review_opportunity(
            SplitClient(),
            {"company": "Acme", "title": "Intern", "description": "Ambiguous role"},
            {"degree": "Mechanical Engineering"},
        )
        self.assertEqual(
            result["answers"]["role_alignment"]["label"],
            questions["role_alignment"]["criteria"][0],
        )


class TypeSafeApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.decision_client = RecordingDecisionClient()
        app = create_app(
            db_path=self.platform_path,
            access_token="typesafe-owner-token",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
            typesafe_client_factory=lambda: self.decision_client,
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.headers = {"Authorization": "Bearer typesafe-owner-token"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def test_status_and_explicit_review_expose_no_secret_and_do_not_change_score(self):
        status = self.client.get("/api/v1/typesafe", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["configured"])
        self.assertNotIn("key", json.dumps(status.json()).lower())

        before = self.client.get("/api/v1/opportunities/job-a", headers=self.headers).json()
        response = self.client.post(
            "/api/v1/opportunities/job-a/jev-review", headers=self.headers
        )
        after = self.client.get("/api/v1/opportunities/job-a", headers=self.headers).json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "ai_suggestion")
        self.assertEqual(before["score"], after["score"])
        self.assertEqual(len(self.decision_client.calls), 1)

    def test_unknown_opportunity_is_not_sent_to_provider(self):
        response = self.client.post(
            "/api/v1/opportunities/unknown/jev-review", headers=self.headers
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.decision_client.calls, [])

    def test_review_does_not_provision_a_missing_profile(self):
        from contextlib import closing
        from opportunity_app.schema import connect_product

        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("DELETE FROM profile_facts WHERE user_id='local-user'")
            conn.execute("DELETE FROM profiles WHERE user_id='local-user'")
            conn.commit()

        response = self.client.post(
            "/api/v1/opportunities/job-a/jev-review", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.decision_client.calls[-1]["state"]["student_profile"], {})

        with closing(connect_product(self.platform_path, read_only=True)) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM profiles WHERE user_id='local-user'"
            ).fetchone()[0]
        self.assertEqual(remaining, 0, "an advisory review must not mutate local profile state")


if __name__ == "__main__":
    unittest.main()

