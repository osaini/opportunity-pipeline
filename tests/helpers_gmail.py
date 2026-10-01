"""Shared Gmail fakes for the outreach tests: a scripted Gmail API and the fixtures built around it.

Not a test module: nothing here is named test*, so neither unittest nor pytest collects it."""

import base64
import email
import json
import re
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import getaddresses, parseaddr
from pathlib import Path
from unittest import mock

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, outreach_delivery, outreach_gmail, outreach_inbox
from opportunity_app.api import create_app
from opportunity_app.mail_trust import Authentication
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate


ACCOUNT = "student@school.example"
PDF = b"%PDF-1.4 fake resume"
# The two scopes a connection made before the reply label was built holds: the fixture for "connected, cannot label".
SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]
MODIFY = "https://www.googleapis.com/auth/gmail.modify"
LABEL_SCOPES = [*SCOPES, MODIFY]


def plain_notice(text, *, subject="Mail delivery failed: returning message to sender", headers=""):
    """A notice with no delivery report, as Exim and some groups send them."""
    return (
        f"From: Mail Delivery System <Mailer-Daemon@mx.example>\nTo: {ACCOUNT}\nSubject: {subject}\n{headers}"
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n"
        f"{text}\n"
    ).encode()


def delivery_report(failed=(), delayed=(), text="Address not found. Your message wasn't delivered."):
    """A standard delivery status notice (RFC 3464), as the raw text Gmail stores."""
    blocks = "".join(
        f"\nFinal-Recipient: rfc822; {address}\nAction: {action}\nStatus: {status}\n"
        f"Diagnostic-Code: smtp; {status.replace('.', '')[:3]} {status} {address} does not exist\n"
        for address, action, status in [*((a, "failed", "5.1.1") for a in failed), *((a, "delayed", "4.4.1") for a in delayed)]
    )
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
        f"To: {ACCOUNT}\n"
        "Subject: Delivery Status Notification (Failure)\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\n\n'
        "--b\n"
        'Content-Type: text/plain; charset="UTF-8"\n\n'
        f"Hello {ACCOUNT},\n\n{text}\n\n"
        "--b\n"
        "Content-Type: message/delivery-status\n\n"
        "Reporting-MTA: dns; googlemail.com\n"
        f"{blocks}\n"
        "--b--\n"
    ).encode()


def rate_limited(reason="rateLimitExceeded", status=403, headers=None):
    """Gmail asking the app to slow down, as its API words it."""
    return httpx.Response(status, headers=headers or {}, json={"error": {
        "code": status, "message": "Rate Limit Exceeded", "errors": [{"reason": reason, "domain": "usageLimits"}],
        "status": "PERMISSION_DENIED" if status == 403 else "RESOURCE_EXHAUSTED",
    }})


class AlwaysInTransaction:
    """A SQLite connection that says a transaction is open after any statement, as PostgresConnection does.

    psycopg opens a transaction on the first query, a read included, so on
    PostgreSQL in_transaction is True from a caller's first SELECT until it
    commits. Everything else goes to the real connection.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    @property
    def in_transaction(self):
        return True

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def forget_gmail_backoff(test):
    """Start with no rate limit, search position or mail listing remembered for anyone, and leave none behind.

    The memory outlives a test, and every test's student is local-user with sends in thread-1, thread-2...
    """
    from opportunity_app import outreach_inbox

    for state in (outreach_gmail._BACKOFF, outreach_gmail._HEALTH, outreach_inbox._RESUME, outreach_inbox._LAST_SWEEP):
        state.clear()
        test.addCleanup(state.clear)


def failure_notice(notice_id="dsn-1", *, subject="Delivery Status Notification (Failure)",
                   sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>", failed="", snippet="Address not found"):
    """A delivery status notice as Gmail's threads.get returns it in the metadata format."""
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
               {"name": "Content-Type", "value": 'multipart/report; report-type=delivery-status; boundary="b"'}]
    if failed:
        headers.append({"name": "X-Failed-Recipients", "value": failed})
    return {"id": notice_id, "labelIds": ["INBOX"], "internalDate": "2000", "snippet": snippet,
            "payload": {"mimeType": "multipart/report", "headers": headers}}


class FakeGmail:
    """A scripted Gmail API and token endpoint that records every request."""

    def __init__(self):
        self.requests = []
        self.profile_email = ACCOUNT
        self.expired_tokens = set()
        self.refresh_ok = True
        # A status the token endpoint answers a renewal with instead (429: too many renewals).
        self.refresh_status = None
        # Called for every Gmail API read instead of answering it (a rate limit, say).
        self.read_response = None
        self.drafts = {}
        self.draft_count = 0
        self.sent = []
        self.send_status = 200
        # The JSON Gmail sends with a send_status that is not 200.
        self.send_body = None
        # Raised after Gmail accepted the message, as a timeout on the answer would be.
        self.send_error = None
        # Raised before the message reaches Gmail.
        self.send_unreached = None
        self.draft_status = 200
        self.draft_error = None
        self.draft_get_status = None
        # Run once, the next time Gmail sees that call: "profile", "send", "create_draft", "get_draft".
        self.hooks = {}
        # Messages Gmail threaded with a sent email after it, by thread id.
        self.replies = {}
        self.thread_status = None
        # Full text of messages by id, the ids a search for failure notices
        # finds, and the ids a search for replies finds.
        self.raw = {}
        self.inbox_notices = []
        self.inbox_replies = []
        self.searches = []
        # Results per page of a search, or None for one page.
        self.page_size = None
        # Messages Gmail reads back in the metadata format, and what a search of Sent finds.
        self.metadata = {}
        self.sent_search = []
        # The Gmail thread and labels of a message, by id: one not listed is alone in its own thread, in the
        # inbox. The same names and meaning as MailboxGmail's (test_application_inbox).
        self.threads = {}
        self.labels = {}
        # Every messages.list request's parameters (includeSpamTrash, say), and every threads.get
        # (thread id, format, headers asked for).
        self.search_params = []
        self.thread_gets = []
        # Threads deleted for good (threads.get answers 404), and answers for the next threads.get calls.
        self.gone_threads = set()
        self.thread_answers = []
        # The reply label's Gmail: threads as {thread id: [{"id", "labelIds"[, "payload": {"headers": [...]}]}]}; a message
        # with a payload has its headers cut to the ones a metadata read asks for.
        self.label_threads = {}
        # The mailbox's labels ({"id", "name", "type"}), every labels.create body, and every messages.batchModify body.
        self.gmail_labels = []
        self.label_creates = []
        self.batch_modifies = []
        # Answers for the next labels.create and batchModify calls, in order: a callable returning a response, else the default.
        self.create_answers = []
        self.modify_answers = []
        # A labels.list answer used instead of the mailbox's labels while it is set.
        self.labels_list_response = None
        self.label_gets = 0

    def label_named(self, name, kind="user"):
        return next((label for label in self.gmail_labels if label["name"] == name and label["type"] == kind), None)

    def add_label(self, name, kind="user"):
        label = {"id": f"Label_{len(self.gmail_labels) + 1}", "name": name, "type": kind}
        self.gmail_labels.append(label)
        return label

    def batch_modify(self, request):
        body = json.loads(request.content)
        self.batch_modifies.append(body)
        if self.modify_answers:
            return self.modify_answers.pop(0)()
        known = {label["id"] for label in self.gmail_labels}
        for label_id in body.get("addLabelIds", []):
            if label_id not in known:
                return httpx.Response(400, json={"error": {"code": 400, "message": f"Invalid label: {label_id}"}})
        for messages in self.label_threads.values():
            for message in messages:
                if message["id"] in body["ids"]:
                    message["labelIds"] = [*message.get("labelIds", []), *body.get("addLabelIds", [])]
        return httpx.Response(204)

    def thread_of(self, message_id):
        for thread_id, items in self.replies.items():
            if any(item.get("id") == message_id for item in items):
                return thread_id
        return self.threads.get(message_id, message_id)

    def parsed(self, message_id):
        return email.message_from_bytes(self.raw[message_id][0], policy=policy.default)

    def listed(self, message_id, query, spam_and_trash):
        """Whether a search lists a message: the folders it asked for, its from:(...) terms, its subject and phrase terms."""
        labels = set(self.labels.get(message_id, ["INBOX"]))
        if labels & {"SPAM", "TRASH"} and not spam_and_trash:
            return False
        if ("-in:trash" in query and "TRASH" in labels) or ("-in:sent" in query and "SENT" in labels):
            return False
        # -label:name drops a message that already carries that label (the label's search form: lowercase, dashes for spaces and slashes).
        for wanted_form in re.findall(r"(?<![\w-])-label:(\S+)", query):
            ids = {label["id"] for label in self.gmail_labels if re.sub(r"[ /]+", "-", label["name"].lower()) == wanted_form}
            held = next((item.get("labelIds", []) for items in self.label_threads.values() for item in items if item["id"] == message_id), [])
            if ids & set(held):
                return False
        # -from:(a OR b) drops a message of the label threads whose sender's name is one of those (a delivery notice).
        for terms in re.findall(r"-from:\(([^)]*)\)", query):
            named = {term.strip().casefold() for term in terms.split(" OR ") if term.strip()}
            held = next((item for items in self.label_threads.values() for item in items if item["id"] == message_id), {})
            sender = next((h["value"] for h in (held.get("payload") or {}).get("headers", []) if h["name"].casefold() == "from"), "")
            if parseaddr(sender)[1].casefold().split("@", 1)[0] in named:
                return False
        # A history search's {to:a cc:a subject:"phrase"}: a message read back in the metadata format is found only when
        # one of its recipients holds a term's address or its subject holds a phrase. Gmail matches an address by its
        # words, so to:ann@bovi.example also finds jo.ann@bovi.example; the app checks each hit itself.
        history = re.search(r"\{(.*)\}", query) if query.startswith("in:sent") else None
        if history is not None and message_id in self.metadata:
            headers = {h["name"].casefold(): h["value"] for h in self.metadata[message_id]["payload"]["headers"]}
            recipients = {address.casefold() for name in ("to", "cc", "bcc") for _n, address in getaddresses([headers.get(name, "")])}
            wanted = set(re.findall(r"(?:to|cc|bcc):(\S+)", history.group(1)))
            phrases = re.findall(r'subject:"([^"]+)"', history.group(1))
            after, taken = re.search(r"\bafter:(\d+)", query), self.metadata[message_id].get("internalDate")
            if after and taken is not None and int(taken) / 1000 < int(after.group(1)):
                return False
            def words(text):
                return [word for word in re.split(r"[^0-9a-z]+", text.casefold()) if word]

            def holds(recipient, term):
                have, need = words(recipient), words(term)
                return bool(need) and any(have[start:start + len(need)] == need for start in range(len(have) - len(need) + 1))

            return any(holds(recipient, term) for recipient in recipients for term in wanted) or any(
                phrase.casefold() in headers.get("subject", "").casefold() for phrase in phrases
            )
        if message_id not in self.raw:
            return True
        message = self.parsed(message_id)
        terms = re.search(r"(?<!-)\bfrom:\(([^)]*)\)", query)
        if terms is not None:
            wanted = {term.strip().casefold() for term in terms.group(1).split(" OR ") if term.strip()}
            # Gmail matches the From's address, from its raw text: it never fails on a header Python cannot parse.
            raw_from = next((str(value) for name, value in message.raw_items() if name.casefold() == "from"), "")
            found = [address for _name, address in getaddresses([raw_from]) if "@" in address]
            sender = (found[0] if found else parseaddr(raw_from)[1]).casefold()
            domain = sender.rsplit("@", 1)[-1]
            return sender in wanted or any(domain == term or domain.endswith("." + term) for term in wanted if "@" not in term)
        either = re.match(r"^\{(.*)\}", query)
        if either is not None:
            subject = str(message.get("Subject", "")).casefold()
            body = message.get_body(preferencelist=("plain",))
            words = f"{subject} {body.get_content() if body else ''}".casefold()
            for field, phrase in re.findall(r'(subject:)?"([^"]+)"', either.group(1)):
                if (phrase.casefold() in subject) if field else (phrase.casefold() in words):
                    return True
            return False
        return True

    def run_hook(self, name):
        hook = self.hooks.pop(name, None)
        if hook:
            hook()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            if self.refresh_status:
                return httpx.Response(self.refresh_status)
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        if request.headers.get("Authorization", "").removeprefix("Bearer ") in self.expired_tokens:
            return httpx.Response(401, json={"error": {"code": 401}})
        if request.method == "GET" and self.read_response is not None:
            return self.read_response()
        if path.endswith("/profile"):
            self.run_hook("profile")
            return httpx.Response(200, json={"emailAddress": self.profile_email})
        if path.endswith("/labels"):
            if request.method == "GET":
                self.label_gets += 1
                if self.labels_list_response is not None:
                    return self.labels_list_response()
                return httpx.Response(200, json={"labels": list(self.gmail_labels)})
            if request.method == "POST":
                body = json.loads(request.content)
                self.label_creates.append(body)
                if self.create_answers:
                    return self.create_answers.pop(0)()
                return httpx.Response(200, json=self.add_label(body["name"]))
        if request.method == "POST" and path.endswith("/messages/batchModify"):
            return self.batch_modify(request)
        if request.method == "POST" and path.endswith("/drafts"):
            self.run_hook("create_draft")
            if self.draft_status != 200:
                return httpx.Response(self.draft_status)
            self.draft_count += 1
            number = self.draft_count
            self.drafts[f"r-{number}"] = json.loads(request.content)
            if self.draft_error:
                raise self.draft_error
            return httpx.Response(200, json={"id": f"r-{number}", "message": {"id": f"18c{number}", "threadId": f"18c{number}"}})
        if request.method == "POST" and path.endswith("/messages/send"):
            self.run_hook("send")
            if self.send_unreached:
                raise self.send_unreached
            if self.send_status != 200:
                return httpx.Response(self.send_status, json=self.send_body) if self.send_body else httpx.Response(self.send_status)
            self.sent.append(json.loads(request.content))
            if self.send_error:
                raise self.send_error
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": f"thread-{len(self.sent)}"})
        if request.method == "POST" and path.endswith("/drafts/send"):
            draft_id = json.loads(request.content)["id"]
            if draft_id not in self.drafts:
                return httpx.Response(404)
            self.sent.append(self.drafts.pop(draft_id)["message"])
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": f"thread-{len(self.sent)}"})
        if request.method == "GET" and "/threads/" in path:
            if self.thread_status:
                return httpx.Response(self.thread_status, json={"error": {"message": "Request had insufficient authentication scopes."}})
            thread_id = path.rsplit("/", 1)[1]
            form = request.url.params.get("format", "full")
            asked = [name.casefold() for name in request.url.params.get_list("metadataHeaders")]
            self.thread_gets.append((thread_id, form, asked))
            if self.thread_answers:
                return self.thread_answers.pop(0)()
            if thread_id in self.gone_threads:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}})
            if form in ("minimal", "metadata") and thread_id in self.label_threads:
                messages = []
                for message in self.label_threads[thread_id]:
                    message = {"threadId": thread_id, **message}
                    if form == "metadata" and asked and message.get("payload"):
                        payload = dict(message["payload"])
                        payload["headers"] = [header for header in payload.get("headers", []) if header["name"].casefold() in asked]
                        message["payload"] = payload
                    messages.append(message)
                return httpx.Response(200, json={"id": thread_id, "historyId": "1", "messages": messages})
            sent = {"id": thread_id.replace("thread-", "sent-"), "labelIds": ["SENT"], "internalDate": "1000",
                    "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": ACCOUNT}]}}
            placed = []
            for message_id, home in self.threads.items():
                if home == thread_id and message_id in self.raw:
                    message = self.parsed(message_id)
                    placed.append({"id": message_id, "labelIds": list(self.labels.get(message_id, ["INBOX"])),
                                   "internalDate": str(self.raw[message_id][1]), "snippet": "",
                                   "payload": {"mimeType": message.get_content_type(),
                                               "headers": [{"name": key, "value": " ".join(str(value).split())}
                                                           for key, value in message.raw_items()]}})
            messages = []
            for message in (sent, *self.replies.get(thread_id, []), *placed):
                message = {**message, "threadId": thread_id}
                if form == "metadata" and asked:
                    payload = dict(message.get("payload") or {})
                    payload["headers"] = [header for header in payload.get("headers", []) if header["name"].casefold() in asked]
                    message["payload"] = payload
                messages.append(message)
            return httpx.Response(200, json={"id": thread_id, "historyId": "1", "messages": messages})
        if request.method == "GET" and path.endswith("/messages"):
            if self.thread_status:
                return httpx.Response(self.thread_status)
            params = request.url.params
            query = params.get("q", "")
            self.searches.append(query)
            self.search_params.append(dict(params))
            if "mailer-daemon" in query and "-from:(" not in query:
                found = self.inbox_notices
            elif query.startswith("in:sent"):
                found = [m for m in self.sent_search if self.listed(m, query, False)]
            else:
                found = [m for m in self.inbox_replies if self.listed(m, query, params.get("includeSpamTrash") == "true")]
            listed = lambda ids: [{"id": message_id, "threadId": self.thread_of(message_id)} for message_id in ids]
            if self.page_size:
                start = int(request.url.params.get("pageToken") or 0)
                page = found[start:start + self.page_size]
                more = start + self.page_size < len(found)
                body = {"messages": listed(page)}
                if more:
                    body["nextPageToken"] = str(start + self.page_size)
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"messages": listed(found)})
        if request.method == "GET" and "/messages/" in path:
            message_id = path.rsplit("/", 1)[1]
            if request.url.params.get("format") == "minimal":
                home = next((thread for thread, items in self.label_threads.items() if any(item["id"] == message_id for item in items)), None)
                return httpx.Response(200, json={"id": message_id, "threadId": home}) if home else httpx.Response(404)
            if request.url.params.get("format") == "metadata":
                return httpx.Response(200, json=self.metadata[message_id]) if message_id in self.metadata else httpx.Response(404)
            if message_id not in self.raw:
                return httpx.Response(404)
            raw, received = self.raw[message_id]
            return httpx.Response(200, json={
                "id": message_id, "threadId": self.thread_of(message_id),
                "labelIds": list(self.labels.get(message_id, ["INBOX"])),
                "internalDate": str(received), "raw": base64.urlsafe_b64encode(raw).decode(),
            })
        if request.method == "GET" and "/drafts/" in path:
            self.run_hook("get_draft")
            if self.draft_get_status:
                return httpx.Response(self.draft_get_status)
            draft_id = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": draft_id}) if draft_id in self.drafts else httpx.Response(404)
        return httpx.Response(500)


def now_ms(offset=timedelta()):
    return int((datetime.now(timezone.utc) + offset).timestamp() * 1000)


def mail(body, *, sender="Greg Lee <greg@bovi.example>", subject="Re: Robotics internship question", headers="",
         to=ACCOUNT, verified=True):
    """A message as the raw text Gmail stores, with Gmail's own sender check on top unless ``verified`` is False."""
    domain = re.findall(r"@([\w.-]+)", sender)[-1] if "@" in sender else ""
    check = f"Authentication-Results: mx.google.com;\n       dkim=pass header.i=@{domain} header.s=s1\n" if verified else ""
    return (
        f"{check}From: {sender}\nTo: {to}\nSubject: {subject}\n{headers}"
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n"
        f"{body}\n"
    ).encode()


def gmail_vouches(message):
    """mail_trust.authenticate for the .example domains these tests use, which the public suffix list does not know.

    Gmail vouches for a sender when its own check says the From domain signed the email (dkim=pass).
    """
    sender = parseaddr(str(message.get("From", "")))[1].casefold()
    domain = sender.rsplit("@", 1)[-1]
    ok = f"dkim=pass header.i=@{domain}" in str(message.get("Authentication-Results", ""))
    return Authentication(ok, "dkim" if ok else "", "" if ok else "the sender did not pass Gmail's check", sender, domain)


INBOX_AUTH = {"Authorization": "Bearer inbox-owner"}
INBOX_USER = "local-user"


class ReplyCaptureFixture:
    """The app, a connected Gmail and a scripted FakeGmail for tests of replies read from the mailbox.

    A mixin, not a TestCase, so importing it never collects tests: put it before unittest.TestCase in the bases.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        attachment = root / "Resume.pdf"
        attachment.write_bytes(PDF)
        self.key = Fernet.generate_key().decode()
        self.env = mock.patch.dict("os.environ", {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id", "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "PIPELINE_CONNECTION_KEY": self.key, "PIPELINE_OUTREACH_ACCOUNT": ACCOUNT,
            "PIPELINE_OUTREACH_COMPOSE": "gmail", "PIPELINE_OUTREACH_ATTACHMENT": str(attachment),
        })
        self.env.start()
        self.gmail = FakeGmail()
        self.factory = lambda: httpx.Client(transport=httpx.MockTransport(self.gmail.handler))
        app = create_app(
            db_path=self.platform_path, access_token="inbox-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()
        outreach_inbox._LAST_CAPTURE.clear()
        forget_gmail_backoff(self)
        vouches = mock.patch.object(outreach_inbox, "authenticate", gmail_vouches)
        vouches.start()
        self.addCleanup(vouches.stop)
        self.connect()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def connect(self, scopes=SCOPES):
        fernet = Fernet(self.key.encode())
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', ?, ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{INBOX_USER}", INBOX_USER, str(list(scopes)).replace("'", '"'),
                 fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh-token").decode(), utc_now(), utc_now()),
            )
            conn.commit()

    def sent_target(self, **overrides):
        created = self.client.post("/api/v1/outreach", headers=INBOX_AUTH, json={
            "company": "Bovi", "contact_email": "greg@bovi.example", "website": "https://bovi.example",
            "email_subject": "Robotics internship question", "email_body": "Hi Greg,\n\nShort note about Bovi.\n\nSam",
            **overrides,
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=INBOX_AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        sent = self.client.post(f"/api/v1/outreach/{approved['id']}/gmail-send", headers=INBOX_AUTH, json={
            "kind": "initial", "fingerprint": approved["draft_fingerprint"],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        return self.target(approved)

    def arrive(self, message_id, raw, received=None, labels=None):
        self.gmail.raw[message_id] = (raw, received if received is not None else now_ms(timedelta(minutes=5)))
        self.gmail.inbox_replies.append(message_id)
        if labels is not None:
            self.gmail.labels[message_id] = labels

    def arrive_in_thread(self, message_id, raw, thread_id="thread-1", received=None, labels=None):
        """A message Gmail threaded with a sent email (thread-1 is the first one sent)."""
        self.arrive(message_id, raw, received, labels)
        self.gmail.threads[message_id] = thread_id

    def check(self):
        outreach_inbox._LAST_CAPTURE.clear()
        response = self.client.post("/api/v1/outreach/inbox-check", headers=INBOX_AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def target(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=INBOX_AUTH).json()

    def replies(self, target):
        return [event["detail"] for event in self.target(target)["events"] if event["event_type"] == "reply_logged"]
