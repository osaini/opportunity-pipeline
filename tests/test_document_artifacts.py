"""The PDF made from an approved document (student/artifacts.py): it is always the one the approved text makes.

Phase 5 M7: Apply for me attaches the approved cover letter, so a PDF rendered from older text must never be
returned. The artifact records the SHA-256 of the text it was rendered from, and a different text, an artifact with
no record (one made before this column) or a file that no longer matches its own hash is rendered again.
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.core.timestamps import utc_now
from opportunity_app.student import artifacts as document_artifacts, preparation

from helpers_apply import ApplyCase, USER, setUpModule, tearDownModule  # noqa: F401 - the module hooks unittest and pytest run


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArtifactFreshnessTests(ApplyCase):
    def setUp(self):
        super().setUp()
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.opportunity("job-1")

    def document(self, content="Dear Hiring Team,\n\nFirst text.\n", *, status="approved", version=1, document_id="doc-1"):
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO generated_documents(id, user_id, opportunity_id, document_type, version, content, evidence_json, status, approved_at, "
                "created_at, updated_at) VALUES(?, ?, 'job-1', 'cover_letter', ?, ?, ?, ?, ?, ?, ?)",
                (document_id, USER, version, content, json.dumps([{"profile_field": "name", "value": "Sam", "source": "confirmed_profile"}]),
                 status, stamp if status == "approved" else None, stamp, stamp),
            )
        return document_id

    def ensure(self, document_id="doc-1"):
        return document_artifacts.ensure_document_artifact(self.conn, document_id, self.storage, user_id=USER)

    def pdf_path(self, artifact):
        return self.storage / "generated" / artifact["storage_path"]

    def edit_without_deleting_the_pdf(self, document_id, content):
        """What the API did before: the edit committed, then the old PDF's delete failed (or never ran)."""
        with self.conn:
            self.conn.execute(
                "UPDATE generated_documents SET content=?, status='draft', approved_at=NULL WHERE id=?", (content, document_id))
        preparation.approve_document(self.conn, document_id, user_id=USER)

    def test_a_new_artifact_records_the_hash_of_the_text_it_was_made_from(self):
        text = "Dear Hiring Team,\n\nFirst text.\n"
        artifact = self.ensure(self.document(text))
        self.assertEqual(artifact["content_sha256"], digest(text))
        self.assertEqual(hashlib.sha256(self.pdf_path(artifact).read_bytes()).hexdigest(), artifact["sha256"])

    def test_a_fresh_artifact_is_returned_as_it_is(self):
        self.document()
        first = self.ensure()
        with mock.patch.object(document_artifacts, "_render_pdf", side_effect=AssertionError("rendered again")):
            again = self.ensure()
        self.assertEqual(again["id"], first["id"])

    def test_an_edit_that_committed_but_left_the_old_pdf_gets_a_new_pdf(self):
        self.document()
        old = self.ensure()
        old_path = self.pdf_path(old)
        self.edit_without_deleting_the_pdf("doc-1", "Dear Hiring Team,\n\nSecond text, quite different.\n")
        self.assertTrue(old_path.exists(), "the stale file is still there, which is the case this guards")
        new = self.ensure()
        self.assertNotEqual(new["id"], old["id"])
        self.assertNotEqual(new["sha256"], old["sha256"], "a different text makes a different file")
        self.assertEqual(new["content_sha256"], digest("Dear Hiring Team,\n\nSecond text, quite different.\n"))
        self.assertTrue(self.pdf_path(new).exists())
        self.assertFalse(old_path.exists(), "the stale file is removed once its row is replaced")
        rows = self.conn.execute("SELECT id FROM generated_document_artifacts WHERE document_id='doc-1'").fetchall()
        self.assertEqual([row["id"] for row in rows], [new["id"]], "one artifact per document")

    def test_an_artifact_made_before_the_hash_was_recorded_is_made_again(self):
        self.document()
        old = self.ensure()
        with self.conn:
            self.conn.execute("UPDATE generated_document_artifacts SET content_sha256='' WHERE id=?", (old["id"],))
        new = self.ensure()
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(new["content_sha256"], digest("Dear Hiring Team,\n\nFirst text.\n"))

    def test_a_file_that_no_longer_matches_its_own_hash_is_made_again(self):
        self.document()
        old = self.ensure()
        self.pdf_path(old).write_bytes(b"%PDF-1.4 something else entirely")
        new = self.ensure()
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(hashlib.sha256(self.pdf_path(new).read_bytes()).hexdigest(), new["sha256"])

    def test_a_missing_file_is_made_again(self):
        self.document()
        old = self.ensure()
        self.pdf_path(old).unlink()
        new = self.ensure()
        self.assertTrue(self.pdf_path(new).exists())

    def test_a_draft_never_becomes_an_artifact(self):
        self.document(status="draft")
        with self.assertRaises(ValueError):
            self.ensure()


if __name__ == "__main__":
    unittest.main()
