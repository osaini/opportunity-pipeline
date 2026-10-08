"""FakeLever, the local Lever board (tests/apply_fake_ats.py), against what docs/phase5-lever-handoff-spec.md section 3 says a Lever form does.

The first half needs no browser: the replies, the multipart reading, the switches and the fixtures. The second half drives FakeLever in a real
headless Chromium (skipped without it, required in CI's `browser-python`, as the other browser modules are). It proves the fake is a fair
stand-in: the résumé reader posts on attach and fills the parser's own fields by the rules of item 8, the location field behaves as item 9 says,
Submit goes through the stand-in hCaptcha and posts the form once to its own URL (item 10), the page-wide required-checkbox rule (item 11) and the
Cloudflare interstitial are there, and each scenario answers as configured. Nothing here fills a form by an adapter: the test plays the student.
Every company, person and posting is fictional. Nothing reaches the network.
"""

import hashlib
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from apply_fake_ats import (
    FAKE_LEVER_HOSTS,
    LEVER_APPLY_PATH,
    LEVER_APPLY_URL,
    LEVER_CLOUDFLARE_BEACON_PATH,
    LEVER_EU_APPLY_URL,
    LEVER_HCAPTCHA_HOSTS,
    LEVER_NOISE_HOSTS,
    LEVER_PARSE_MODES,
    LEVER_SCENARIOS,
    LEVER_THANKS_PATH,
    LEVER_THANKS_URL,
    FakeLever,
    LeverReply,
    lever_fixture_text,
    lever_parse_reply,
    lever_search_options,
    parse_multipart,
)
from browser_support import requires_chromium

from opportunity_app.apply.lever_form import parse_lever_form

FORM_FIXTURES = ("demo_eeo_survey.html", "cards_files_consent.html", "many_cards.html", "variants.html", "two_required_groups.html")
RESUME_BYTES = b"%PDF-1.4 a fictional resume for the Lever fake"
RESUME = {"name": "Sam Rivera Resume.pdf", "mimeType": "application/pdf", "buffer": RESUME_BYTES}
SAFE_RESUME_NAME = "Sam_Rivera_Resume.pdf"
CHALLENGE_FRAME = 'iframe[title="Main content of the hCaptcha challenge"]'


def multipart_body(parts):
    """A multipart/form-data body from (name, filename or None, bytes) triples, and its content type."""
    boundary = "----fakeLeverBoundary"
    chunks = []
    for name, filename, payload in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else "")
        chunks.append(f"--{boundary}\r\nContent-Disposition: {disposition}\r\n".encode() + (b"Content-Type: application/pdf\r\n" if filename else b"") + b"\r\n" + payload + b"\r\n")
    return b"".join(chunks) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class FakeLeverReplyTests(unittest.TestCase):
    """What FakeLever answers, with no browser."""

    def get(self, fake, url):
        return fake.answer("GET", url)

    def test_unknown_scenario_and_parse_mode_are_refused(self):
        with self.assertRaises(ValueError):
            FakeLever("no_such_scenario")
        with self.assertRaises(ValueError):
            FakeLever(parse_mode="no_such_mode")
        self.assertEqual(len(set(LEVER_SCENARIOS)), len(LEVER_SCENARIOS))
        self.assertEqual(len(set(LEVER_PARSE_MODES)), len(LEVER_PARSE_MODES))

    def test_the_form_page_is_the_fixture_plus_what_a_lever_page_does_by_script(self):
        fake = FakeLever()
        reply = self.get(fake, LEVER_APPLY_URL)
        self.assertEqual(reply.status, 200)
        self.assertIn('<form id="application-form"', reply.body)
        for expected in ('src="/js/parseResume.js"', 'src="/js/application.js"', "https://js.hcaptcha.com/1/api.js?onload=hcaptchaOnLoad", "window.__fakeLever",
                         'class="cc-window', "/cdn-cgi/challenge-platform/scripts/jsd/main.js"):
            self.assertIn(expected, reply.body)
        self.assertIn("__cf_bm=", reply.headers["set-cookie"])
        self.assertEqual(self.get(fake, LEVER_APPLY_URL.replace("jobs.lever.co", "jobs.eu.lever.co")).body, reply.body)
        self.assertEqual(fake.non_get_requests(), [])

    def test_additions_add_no_control_the_page_reader_would_see(self):
        for name in FORM_FIXTURES:
            with self.subTest(name):
                fake = FakeLever(page=name)
                served = self.get(fake, LEVER_APPLY_URL).body
                raw = lever_fixture_text(name)
                self.assertEqual(parse_lever_form(served), parse_lever_form(raw))
                self.assertIsNotNone(parse_lever_form(raw), name)

    def test_the_page_carries_the_tag_manager_script_and_a_bugsnag_beacon_unless_switched_off(self):
        fake = FakeLever()
        body = self.get(fake, LEVER_APPLY_URL).body
        self.assertIn('src="https://www.googletagmanager.com/gtm.js?id=GTM-FAKE"', body)
        self.assertIn("https://notify.bugsnag.com/", body)
        fake.third_party_noise = False
        quiet = self.get(fake, LEVER_APPLY_URL).body
        self.assertNotIn("googletagmanager.com", quiet)
        self.assertNotIn("bugsnag.com", quiet)
        self.assertEqual(set(LEVER_NOISE_HOSTS), {"www.googletagmanager.com", "notify.bugsnag.com"})
        beacon = fake.answer("POST", "https://notify.bugsnag.com/", b"{}")
        self.assertEqual(beacon.status, 200)
        self.assertEqual(fake.non_get_requests(), [fake.requests[-1]])
        self.assertEqual(fake.non_get_requests(noise=False), [])

    def test_the_banner_and_the_extra_scripts_are_switches(self):
        fake = FakeLever()
        fake.cookie_banner = False
        fake.inject = ["window.extraMarker = 1;"]
        body = self.get(fake, LEVER_APPLY_URL).body
        self.assertNotIn("cc-window", body)
        self.assertIn("<script>window.extraMarker = 1;</script>", body)

    def test_other_paths(self):
        fake = FakeLever()
        thanks = self.get(fake, LEVER_THANKS_URL)
        self.assertEqual((thanks.status, "Application submitted!" in thanks.body, "application-form" in thanks.body), (200, True, False))
        posting = self.get(fake, LEVER_APPLY_URL.removesuffix("/apply"))
        self.assertEqual(posting.status, 200)
        gone = self.get(fake, "https://jobs.lever.co/harbordemo/00000000-0000-4000-8000-000000000000/apply")
        self.assertEqual(gone.status, 404)
        self.assertNotIn("application-form", gone.body)
        fake.closed = True
        self.assertEqual(self.get(fake, LEVER_APPLY_URL).status, 404)
        self.assertEqual(self.get(fake, LEVER_THANKS_URL).status, 404)
        other = FakeLever()
        other.pages[("othersite", "abc")] = "variants.html"
        self.assertEqual(self.get(other, "https://jobs.lever.co/othersite/abc/apply").status, 200)
        other.any_posting = True
        self.assertEqual(self.get(other, "https://jobs.eu.lever.co/anything/at-all/apply").status, 200)

    def test_a_multipart_body_is_read_into_parts_and_recorded(self):
        body, content_type = multipart_body([("resume", "cv.pdf", RESUME_BYTES), ("accountId", None, b"338ccb85"), ("note", None, "café".encode())])
        parts = parse_multipart(content_type, body)
        self.assertEqual([part.name for part in parts], ["resume", "accountId", "note"])
        self.assertEqual((parts[0].filename, parts[0].size, parts[0].sha256), ("cv.pdf", len(RESUME_BYTES), hashlib.sha256(RESUME_BYTES).hexdigest()))
        self.assertEqual((parts[0].content_type, parts[0].text), ("application/pdf", ""))
        self.assertEqual((parts[1].filename, parts[1].text, parts[2].text), (None, "338ccb85", "café"))
        self.assertEqual(parse_multipart("text/plain", b"x"), [])
        self.assertEqual(parse_multipart(content_type, b""), [])
        fake = FakeLever(parse_mode="success")
        fake.parse_delay_s = 0
        fake.answer("POST", "https://jobs.lever.co/parseResume", body, content_type=content_type, headers={"content-type": content_type})
        (seen,) = fake.parse_posts()
        self.assertEqual((seen.status, seen.part("accountId").text, seen.text_values()["accountId"]), (200, "338ccb85", ["338ccb85"]))
        self.assertEqual(fake.non_get_requests(), [seen])

    def test_parse_modes(self):
        fake = FakeLever()
        fake.parse_delay_s = 0
        url = "https://jobs.lever.co/parseResume"
        ok = fake.answer("POST", url)
        self.assertEqual((ok.status, json.loads(ok.body)), (200, lever_parse_reply()))
        fake.parse_mode = "failure"
        self.assertEqual(fake.answer("POST", url).status, 422)
        fake.parse_mode = "timeout"
        self.assertIsNone(fake.answer("POST", url))
        fake.parse_mode = "held"
        held = fake.answer("POST", url)
        self.assertFalse(hasattr(held, "status"))
        self.assertIsNotNone(held)
        self.assertEqual([seen.status for seen in fake.parse_posts()], [200, 422, 0, 0])

    def test_the_parse_delay_is_the_pages_to_keep_and_the_handler_never_sleeps(self):
        fake = FakeLever()
        fake.parse_delay_s = 30.0
        started = time.monotonic()
        reply = fake.answer("POST", "https://jobs.lever.co/parseResume")
        self.assertLess(time.monotonic() - started, 5.0)   # the reply is given at once; the page shows "working" for the delay
        self.assertEqual(reply.status, 200)
        self.assertIn('"parseDelayMs": 30000', self.get(fake, LEVER_APPLY_URL).body)

    def test_the_canned_profile_is_wrong_on_purpose_and_fictional(self):
        reply = lever_parse_reply()
        self.assertEqual(reply["position"], "Night Shift Supervisor")
        text = json.dumps(reply)
        for value in [reply["email"], *reply["urls"].values()]:
            self.assertRegex(value, r"(example\.test|\.example\.test)")
        self.assertNotIn("sam.rivera", text.lower())
        self.assertTrue(all("Example State" in option["name"] or "Sample Province" in option["name"] for option in lever_search_options()))

    def test_search_locations_filters_by_the_text_and_can_refuse(self):
        fake = FakeLever()
        found = json.loads(fake.answer("GET", "https://jobs.lever.co/searchLocations?text=Spring").body)
        self.assertEqual([option["name"].split(",")[0] for option in found], ["Springfield", "Springdale"])
        self.assertEqual(json.loads(fake.answer("GET", "https://jobs.lever.co/searchLocations?text=zzz").body), [])
        self.assertEqual(json.loads(fake.answer("GET", "https://jobs.lever.co/searchLocations").body), [])
        fake.search_status = 403
        self.assertEqual(fake.answer("GET", "https://jobs.lever.co/searchLocations?text=Spring").status, 403)
        self.assertEqual(len(fake.search_gets()), 4)

    def test_each_submit_scenario_answers_as_configured(self):
        expected = {"to_thanks": 200, "thanks_in_place": 200, "form_again": 200, "refused_4xx": 422, "server_5xx": 500}
        for scenario, status in expected.items():
            with self.subTest(scenario):
                fake = FakeLever(scenario)
                reply = fake.answer("POST", LEVER_APPLY_URL, b"", content_type="multipart/form-data; boundary=x")
                self.assertEqual(reply.status, status)
                (seen,) = fake.apply_posts()
                self.assertEqual(seen.status, status)
        to_thanks = FakeLever("to_thanks").answer("POST", LEVER_APPLY_URL).body
        self.assertIn(f'location.replace("{LEVER_THANKS_PATH}")', to_thanks)
        self.assertNotIn("application-form", to_thanks)
        refused = FakeLever("refused_4xx")
        refused.refused_status, refused.invalid_field = 400, "phone"
        body = refused.answer("POST", LEVER_APPLY_URL)
        self.assertEqual(body.status, 400)
        self.assertRegex(body.body, r'<input[^>]*name="phone" aria-invalid="true"')
        self.assertEqual(body.body.count('aria-invalid="true"'), 1)
        again = FakeLever("form_again").answer("POST", LEVER_APPLY_URL).body
        self.assertIn('<form id="application-form"', again)
        self.assertNotIn("aria-invalid", again)
        server = FakeLever("server_5xx")
        server.server_status = 503
        self.assertEqual(server.answer("POST", LEVER_APPLY_URL).status, 503)

    def test_a_post_to_any_other_path_is_not_a_submit(self):
        fake = FakeLever()
        self.assertEqual(fake.answer("POST", "https://jobs.lever.co/harbordemo/other/apply").status, 404)
        self.assertEqual(fake.answer("POST", f"https://jobs.lever.co{LEVER_APPLY_PATH}/extra").status, 404)
        self.assertEqual(fake.answer("POST", f"https://jobs.lever.co{LEVER_CLOUDFLARE_BEACON_PATH}").status, 204)
        self.assertEqual([seen.status for seen in fake.requests], [404, 404, 204])

    def test_the_hcaptcha_hosts(self):
        fake = FakeLever()
        script = fake.answer("GET", "https://js.hcaptcha.com/1/api.js?onload=hcaptchaOnLoad&render=explicit")
        self.assertEqual((script.status, "window.hcaptcha" in script.body), (200, True))
        for challenge in (False, True):
            fake.challenge = challenge
            found = json.loads(fake.answer("GET", "https://api.hcaptcha.com/checksiteconfig?v=fake").body)
            self.assertEqual((found["pass"], found["challenge"]), (True, challenge))
        frame = fake.answer("GET", "https://newassets.hcaptcha.com/captcha/v1/fake/hcaptcha.html#frame=challenge")
        self.assertEqual((frame.status, 'id="solve"' in frame.body), (200, True))
        fake.hcaptcha_loads = False
        refused = fake.answer("GET", "https://js.hcaptcha.com/1/api.js")
        self.assertIsInstance(refused, LeverReply)
        self.assertTrue(refused.abort)
        self.assertEqual(set(LEVER_HCAPTCHA_HOSTS), {"js.hcaptcha.com", "api.hcaptcha.com", "newassets.hcaptcha.com"})
        self.assertEqual(set(FAKE_LEVER_HOSTS), {"jobs.lever.co", "jobs.eu.lever.co"})

    def test_the_cloudflare_script_posts_a_beacon_only_when_asked(self):
        fake = FakeLever()
        quiet = fake.answer("GET", "https://jobs.lever.co/cdn-cgi/challenge-platform/scripts/jsd/main.js").body
        self.assertNotIn(LEVER_CLOUDFLARE_BEACON_PATH, quiet)
        fake.cloudflare_beacon = True
        self.assertIn(LEVER_CLOUDFLARE_BEACON_PATH, fake.answer("GET", "https://jobs.lever.co/cdn-cgi/challenge-platform/scripts/jsd/main.js").body)

    def test_the_interstitial_lasts_as_long_as_it_is_told_to(self):
        fake = FakeLever()
        fake.interstitial_s = 0.3
        first = self.get(fake, LEVER_APPLY_URL)
        self.assertIn("Just a moment", first.body)
        self.assertNotIn("application-form", first.body)
        self.assertNotIn("http-equiv", first.body)
        time.sleep(0.35)
        self.assertIn('<form id="application-form"', self.get(fake, LEVER_APPLY_URL).body)
        self.assertEqual(fake.interstitials_served, 1)

    def test_the_stand_in_files_keep_to_what_the_spec_records(self):
        parse = lever_fixture_text("parseResume.js")
        for name in ("org", "phone", "name", "email", "location", "urls[LinkedIn]", "urls[Twitter]", "urls[Quora]", "urls[GitHub]", "urls[Other]", "residentialLocation["):
            self.assertIn(name, parse)
        self.assertIn('"change"', parse)
        self.assertIn('"paste"', parse)
        application = lever_fixture_text("application.js")
        self.assertIn("#btn-submit", application)
        self.assertIn("hcaptchaSubmitBtn", application)
        self.assertIn("/searchLocations", application)


@requires_chromium
class FakeLeverBrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._playwright = sync_playwright().start()
        cls._browser = cls._playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._playwright.stop()

    def open(self, fake=None, url=LEVER_APPLY_URL, *, wait_for_widget=True):
        """A fresh page on the fake, loaded and (unless hCaptcha is switched off) with the stand-in widget rendered."""
        fake = fake or FakeLever()
        context = self._browser.new_context(service_workers="block")
        self.addCleanup(context.close)
        self.addCleanup(fake.drop_unanswered)   # runs first: nothing is left open when the context closes
        fake.install(context)
        page = context.new_page()
        page.set_default_timeout(8_000)
        page.goto(url, wait_until="load")
        if wait_for_widget and fake.hcaptcha_loads and page.locator("#btn-submit").count():
            page.wait_for_selector('iframe[title^="Widget containing checkbox"]', state="attached")
        return fake, page

    @staticmethod
    def values(page, *names):
        return page.evaluate("""(names) => Object.fromEntries(names.map((name) => {
            const element = document.querySelector('[name="' + name + '"]');
            return [name, element ? element.value : null];
        }))""", list(names))

    @staticmethod
    def fill_required(page):
        page.fill('input[name="name"]', "Sam Rivera")
        page.fill('input[name="email"]', "sam.rivera@example.test")
        page.fill('input[name="phone"]', "555 0101")
        page.fill('input[name="org"]', "Harbor Test Co")

    @staticmethod
    def wait_until(page, condition, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            page.wait_for_timeout(50)
        return bool(condition())

    @staticmethod
    def indicators(page):
        """Whether the working, success, failure and oversize indicators are shown, read in one step."""
        return page.evaluate("""() => ['working', 'success', 'failure', 'oversize'].map((kind) => {
            const box = document.querySelector('.resume-upload-' + kind).getBoundingClientRect();
            return box.width > 0 && box.height > 0;
        })""")

    def attach(self, page, wait=True):
        page.set_input_files('input[name="resume"]', RESUME)
        if wait:
            page.wait_for_selector(".resume-upload-success", state="visible")

    def type_location(self, page, text):
        page.locator("#location-input").press_sequentially(text, delay=15)
        page.wait_for_selector(".dropdown-option", state="visible")


class ResumeReaderBrowserTests(FakeLeverBrowserCase):
    """Item 8: attaching sends the file, and the reply fills what the user has not made their own."""

    def test_attaching_posts_once_and_fills_the_parsers_fields(self):
        fake, page = self.open()
        page.evaluate("document.querySelector('input[name=\"resume\"]').dataset.before = '1'")
        account = self.values(page, "accountId")["accountId"]
        self.attach(page)
        reply = lever_parse_reply()
        found = self.values(page, "name", "email", "phone", "org", "location", "selectedLocation", "urls[LinkedIn]", "urls[Github]", "resumeStorageId")
        self.assertEqual((found["name"], found["email"], found["phone"], found["org"]), (reply["name"], reply["email"], reply["phone"], reply["org"]))
        self.assertEqual(found["location"], reply["location"]["name"])
        self.assertEqual(json.loads(found["selectedLocation"]), reply["location"])
        self.assertEqual((found["urls[LinkedIn]"], found["resumeStorageId"]), (reply["urls"]["LinkedIn"], reply["resumeStorageId"]))
        # The parser fills urls[GitHub], not this board's urls[Github]: only its own list is filled.
        self.assertEqual(found["urls[Github]"], "")
        self.assertNotIn(reply["position"], json.dumps(page.evaluate("Array.from(document.forms[0].elements).map((element) => element.value)")))
        self.assertEqual(page.locator(".visible-resume-upload .filename").inner_text(), RESUME["name"])
        (post,) = fake.parse_posts()
        self.assertEqual([part.name for part in post.parts], ["resume", "accountId"])
        resume = post.part("resume")
        self.assertEqual((resume.filename, resume.sha256, resume.content_type), (SAFE_RESUME_NAME, hashlib.sha256(RESUME_BYTES).hexdigest(), "application/pdf"))
        self.assertEqual((post.part("accountId").text, post.status, post.host, post.path), (account, 200, "jobs.lever.co", "/parseResume"))
        self.assertEqual(fake.non_get_requests(noise=False), [post])

    def test_the_parser_fills_the_github_link_on_a_board_that_names_it_that_way(self):
        fake, page = self.open(FakeLever(page="cards_files_consent.html"))
        self.attach(page)
        found = self.values(page, "urls[GitHub]", "urls[Twitter]", "urls[Other]", "urls[Portfolio]")
        reply = lever_parse_reply()["urls"]
        self.assertEqual(found, {"urls[GitHub]": reply["GitHub"], "urls[Twitter]": reply["Twitter"], "urls[Other]": reply["Other"], "urls[Portfolio]": ""})

    def test_residential_location_parts_are_filled_where_the_form_has_them(self):
        fake, page = self.open(FakeLever(page="variants.html"))
        self.attach(page)
        found = self.values(page, "residentialLocation[street]", "residentialLocation[city]")
        self.assertEqual(found, {"residentialLocation[street]": "1 Guess Street", "residentialLocation[city]": "Guessville"})

    def test_working_then_success_one_at_a_time(self):
        fake = FakeLever()
        fake.parse_delay_s = 0.5
        fake, page = self.open(fake)
        self.assertEqual(self.indicators(page), [False] * 4)
        self.attach(page, wait=False)
        self.assertEqual(self.indicators(page), [True, False, False, False])
        page.wait_for_selector(".resume-upload-success", state="visible")
        self.assertEqual(self.indicators(page), [False, True, False, False])

    def test_the_page_keeps_working_while_the_read_is_pending_and_the_reply_overwrites_a_fill_made_meanwhile(self):
        fake = FakeLever()
        fake.parse_delay_s = 3.0
        fake, page = self.open(fake)
        started = time.monotonic()
        self.attach(page, wait=False)
        page.fill('input[name="name"]', "Sam Rivera")   # no blur: not the user's own yet, so the reply may take it
        page.wait_for_timeout(100)
        self.assertLess(time.monotonic() - started, 2.0)   # the browser was not held while the read was pending
        self.assertEqual(self.indicators(page), [True, False, False, False])
        self.assertEqual(self.values(page, "name")["name"], "Sam Rivera")
        self.assertEqual(fake.parse_posts()[0].status, 200)   # the reply was given at once
        page.wait_for_selector(".resume-upload-success", state="visible")
        self.assertEqual(self.values(page, "name")["name"], lever_parse_reply()["name"])

    def test_a_field_the_user_changed_or_pasted_into_is_left_alone(self):
        fake, page = self.open()
        page.fill('input[name="name"]', "Sam Rivera")
        page.locator('input[name="name"]').blur()
        page.evaluate("""() => {
            const org = document.querySelector('input[name="org"]');
            org.value = 'Pasted Co';
            org.dispatchEvent(new Event('paste', {bubbles: true}));
        }""")
        self.attach(page)
        reply = lever_parse_reply()
        found = self.values(page, "name", "org", "email", "phone")
        self.assertEqual(found, {"name": "Sam Rivera", "org": "Pasted Co", "email": reply["email"], "phone": reply["phone"]})

    def test_typing_without_a_change_or_a_paste_is_not_the_users_yet(self):
        fake, page = self.open()
        page.fill('input[name="email"]', "typed.not.left@example.test")   # still focused: no change event has fired
        self.assertEqual(page.evaluate("document.activeElement.name"), "email")
        self.attach(page)
        self.assertEqual(self.values(page, "email")["email"], lever_parse_reply()["email"])

    def test_a_field_the_user_emptied_again_is_the_parsers_to_fill(self):
        fake, page = self.open()
        page.fill('input[name="name"]', "Sam Rivera")
        page.locator('input[name="name"]').blur()
        page.fill('input[name="name"]', "")
        page.locator('input[name="name"]').blur()
        self.attach(page)
        self.assertEqual(self.values(page, "name")["name"], lever_parse_reply()["name"])

    def test_selected_location_is_always_rewritten_even_when_the_location_was_chosen_by_the_user(self):
        fake, page = self.open()
        self.type_location(page, "Spring")
        page.locator(".dropdown-option", has_text="Springdale").click()
        chosen = self.values(page, "location", "selectedLocation")
        self.assertEqual(chosen["location"], "Springdale, Example State, United States")
        self.assertEqual(json.loads(chosen["selectedLocation"])["id"], "fake-place-springdale")
        self.attach(page)
        after = self.values(page, "location", "selectedLocation")
        self.assertEqual(after["location"], "Springdale, Example State, United States")
        self.assertEqual(json.loads(after["selectedLocation"]), lever_parse_reply()["location"])

    def test_a_failed_read_leaves_the_form_as_it_was(self):
        fake, page = self.open(FakeLever(parse_mode="failure"))
        page.fill('input[name="name"]', "Sam Rivera")
        page.set_input_files('input[name="resume"]', RESUME)
        page.wait_for_selector(".resume-upload-failure", state="visible")
        self.assertFalse(page.locator(".resume-upload-working").is_visible())
        found = self.values(page, "name", "email", "org", "selectedLocation", "resumeStorageId")
        self.assertEqual(found, {"name": "Sam Rivera", "email": "", "org": "", "selectedLocation": "", "resumeStorageId": ""})
        self.assertEqual(fake.parse_posts()[0].status, 422)

    def test_a_read_that_never_answers_stays_working_and_changes_nothing(self):
        fake, page = self.open(FakeLever(parse_mode="timeout"))
        self.attach(page, wait=False)
        self.assertTrue(self.wait_until(page, lambda: fake.parse_posts()))
        page.wait_for_timeout(600)
        self.assertTrue(page.locator(".resume-upload-working").is_visible())
        self.assertFalse(page.locator(".resume-upload-success").is_visible() or page.locator(".resume-upload-failure").is_visible())
        self.assertEqual(self.values(page, "name", "selectedLocation"), {"name": "", "selectedLocation": ""})
        self.assertEqual(fake.parse_posts()[0].status, 0)

    def test_a_late_reply_rewrites_what_the_page_has_left_empty_and_never_what_the_user_owns(self):
        fake, page = self.open(FakeLever(parse_mode="held"))
        self.attach(page, wait=False)
        self.assertTrue(self.wait_until(page, lambda: fake.held))
        page.fill('input[name="name"]', "Sam Rivera")
        page.locator('input[name="name"]').blur()
        self.assertEqual(fake.release_held(), 1)
        page.wait_for_selector(".resume-upload-success", state="visible")
        reply = lever_parse_reply()
        self.assertEqual(self.values(page, "name", "email", "selectedLocation")["name"], "Sam Rivera")
        self.assertEqual(self.values(page, "email")["email"], reply["email"])
        self.assertEqual(fake.release_held(), 0)

    def test_a_reply_released_after_the_window_closed_is_dropped_quietly(self):
        fake = FakeLever(parse_mode="held")
        context = self._browser.new_context(service_workers="block")
        fake.install(context)
        page = context.new_page()
        page.goto(LEVER_APPLY_URL, wait_until="load")
        self.attach(page, wait=False)
        self.assertTrue(self.wait_until(page, lambda: fake.held))
        context.close()
        self.assertEqual(fake.release_held(), 1)
        self.assertEqual([seen.status for seen in fake.parse_posts()], [0])   # nothing reached a page: the record does not say it did

    def test_releasing_held_replies_never_marks_a_request_that_was_never_answered(self):
        fake, page = self.open(FakeLever(parse_mode="timeout"))
        self.attach(page, wait=False)
        self.assertTrue(self.wait_until(page, lambda: fake.unanswered))
        fake.parse_mode = "held"
        page.set_input_files('input[name="resume"]', {**RESUME, "name": "Second Resume.pdf"})
        self.assertTrue(self.wait_until(page, lambda: fake.held))
        self.assertEqual(fake.release_held(), 1)
        self.assertEqual([seen.status for seen in fake.parse_posts()], [0, 200])
        self.assertEqual(len(fake.unanswered), 1)

    def test_a_file_over_the_limit_is_not_sent(self):
        fake = FakeLever()
        fake.max_upload_bytes = 10
        fake, page = self.open(fake)
        self.attach(page, wait=False)
        page.wait_for_selector(".resume-upload-oversize", state="visible")
        page.wait_for_timeout(300)
        self.assertEqual(fake.parse_posts(), [])
        self.assertFalse(page.locator(".resume-upload-working").is_visible())


class LocationBrowserTests(FakeLeverBrowserCase):
    """Item 9: the location field is a typeahead that forgets."""

    def test_keys_start_a_search_after_a_pause_and_choosing_fills_both_fields(self):
        fake, page = self.open()
        started = time.monotonic()
        self.type_location(page, "Spring")
        self.assertGreaterEqual(time.monotonic() - started, 0.45)
        (search,) = fake.search_gets()
        self.assertEqual((search.query, search.host, search.status), ("text=Spring", "jobs.lever.co", 200))
        self.assertEqual(page.locator(".dropdown-option").all_inner_texts(), ["Springfield, Example State, United States", "Springdale, Example State, United States"])
        page.locator(".dropdown-option", has_text="Springfield").click()
        found = self.values(page, "location", "selectedLocation")
        self.assertEqual(found["location"], "Springfield, Example State, United States")
        self.assertEqual(json.loads(found["selectedLocation"]), {"name": "Springfield, Example State, United States", "id": "fake-place-springfield"})
        self.assertFalse(page.locator(".dropdown-container").is_visible())
        page.locator("#location-input").blur()
        self.assertEqual(self.values(page, "location")["location"], "Springfield, Example State, United States")

    def test_one_search_for_a_burst_of_keys_and_the_text_is_cut_at_100_characters(self):
        fake, page = self.open()
        page.locator("#location-input").press_sequentially("Spr", delay=10)
        page.locator("#location-input").press_sequentially("ing", delay=10)
        page.wait_for_selector(".dropdown-option", state="visible")
        self.assertEqual([search.query for search in fake.search_gets()], ["text=Spring"])
        page.locator("#location-input").fill("")
        page.evaluate("() => { const box = document.querySelector('#location-input'); box.maxLength = 500; }")
        page.locator("#location-input").press_sequentially("x" * 120, delay=0)
        self.assertTrue(self.wait_until(page, lambda: len(fake.search_gets()) == 2))
        self.assertEqual(len(fake.search_gets()[-1].query.removeprefix("text=")), 100)

    def test_fill_alone_starts_no_search(self):
        fake = FakeLever()
        fake.search_debounce_ms = 100
        fake, page = self.open(fake)
        page.fill("#location-input", "Spring")
        page.wait_for_timeout(500)
        self.assertEqual(fake.search_gets(), [])
        self.assertTrue(page.locator(".dropdown-container").is_visible())   # the input event opened the container

    def test_leaving_the_field_without_choosing_empties_both(self):
        fake, page = self.open()
        self.type_location(page, "Spring")
        page.locator("#location-input").blur()
        self.assertEqual(self.values(page, "location", "selectedLocation"), {"location": "", "selectedLocation": ""})
        self.assertFalse(page.locator(".dropdown-container").is_visible())

    def test_a_fill_then_a_blur_empties_both_through_the_page_own_handler(self):
        fake, page = self.open()
        self.attach(page)
        self.assertNotEqual(self.values(page, "selectedLocation")["selectedLocation"], "")
        page.fill("#location-input", "")
        self.assertEqual(self.values(page, "selectedLocation")["selectedLocation"] != "", True)   # fill alone leaves the hidden field
        page.locator("#location-input").blur()
        self.assertEqual(self.values(page, "location", "selectedLocation"), {"location": "", "selectedLocation": ""})
        self.assertEqual(fake.search_gets(), [])

    def test_a_blur_with_the_container_closed_does_nothing(self):
        fake, page = self.open()
        self.attach(page)
        page.locator("#location-input").focus()
        page.locator("#location-input").blur()
        self.assertEqual(self.values(page, "location")["location"], lever_parse_reply()["location"]["name"])
        self.assertNotEqual(self.values(page, "selectedLocation")["selectedLocation"], "")

    def test_arrow_keys_and_enter_choose_an_option_and_enter_does_not_submit(self):
        fake, page = self.open()
        self.fill_required(page)
        self.type_location(page, "Spring")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        self.assertEqual(self.values(page, "location")["location"], "Springdale, Example State, United States")
        page.wait_for_timeout(300)
        self.assertEqual(fake.apply_posts(), [])
        self.assertEqual(self.clicks(page)["submit"], 0)

    def test_a_refused_lookup_shows_nothing_to_choose(self):
        fake = FakeLever()
        fake.search_status = 403
        fake, page = self.open(fake)
        page.locator("#location-input").press_sequentially("Spring", delay=10)
        page.wait_for_selector(".dropdown-container.empty", state="visible")
        self.assertEqual(page.locator(".dropdown-option").count(), 0)
        self.assertEqual(fake.search_gets()[0].status, 403)

    @staticmethod
    def clicks(page):
        return FakeLever.clicks(page)


class SubmitBrowserTests(FakeLeverBrowserCase):
    """Item 10: Submit runs hCaptcha, then the hidden submit, then the browser's own checks, then one POST to the page's own URL."""

    def press(self, page, fake, seconds=5.0):
        page.click("#btn-submit")
        return self.wait_until(page, lambda: fake.apply_posts(), seconds)

    def submit(self, scenario, **switches):
        fake = FakeLever(scenario)
        for name, value in switches.items():
            setattr(fake, name, value)
        fake, page = self.open(fake)
        self.fill_required(page)
        page.evaluate("window.__oldDocument = true")
        self.assertTrue(self.press(page, fake))
        return fake, page

    @staticmethod
    def new_document(page):
        """Wait until the document the test filled has been replaced by the one the POST answered with."""
        page.wait_for_function("() => !window.__oldDocument && document.readyState === 'complete'")

    def test_pressing_submit_posts_the_form_once_to_its_own_url(self):
        fake, page = self.submit("to_thanks")
        page.wait_for_url(f"**{LEVER_THANKS_PATH}")
        (post,) = fake.apply_posts()
        self.assertEqual((post.host, post.path, post.query, post.status, post.resource_type), ("jobs.lever.co", LEVER_APPLY_PATH, "", 200, "document"))
        self.assertTrue(post.content_type.startswith("multipart/form-data"))
        values = post.text_values()
        self.assertEqual((values["name"], values["email"], values["org"]), (["Sam Rivera"], ["sam.rivera@example.test"], ["Harbor Test Co"]))
        self.assertRegex(values["h-captcha-response"][0], r"^P1_fake-hcaptcha-token-\d+$")
        self.assertEqual(values["accountId"], ["338ccb85-3e92-5a8c-987b-51a090bf4ff9"])
        self.assertEqual((post.part("resume").filename, post.part("resume").size), ("", 0))
        self.assertEqual(page.locator("form#application-form").count(), 0)
        self.assertIn("Application submitted!", page.content())

    def test_the_students_press_is_counted_and_the_pages_own_click_on_the_hidden_submit_is_not(self):
        fake = FakeLever()
        # Keep the page in place after the submit event so its counters can be read (a navigation starts them again).
        fake.inject = ['document.getElementById("application-form").addEventListener("submit", function (e) { e.preventDefault(); window.__submitted = (window.__submitted || 0) + 1; });']
        fake, page = self.open(fake)
        self.fill_required(page)
        self.assertEqual(FakeLever.clicks(page), {"submit": 0, "hiddenSubmit": 0, "cookie": 0, "challenge": 0})
        page.click("#btn-submit")
        self.assertTrue(self.wait_until(page, lambda: page.evaluate("window.__submitted || 0") == 1))
        self.assertEqual(FakeLever.clicks(page), {"submit": 1, "hiddenSubmit": 0, "cookie": 0, "challenge": 0})
        page.evaluate("document.getElementById('hcaptchaSubmitBtn').click()")   # an agent that pressed it: counted
        self.assertEqual(FakeLever.clicks(page)["hiddenSubmit"], 1)

    def test_thanks_in_place_leaves_the_path_at_apply(self):
        fake, page = self.submit("thanks_in_place")
        self.new_document(page)
        page.wait_for_selector("text=Application submitted!")
        self.assertEqual((fake.apply_posts()[0].status, page.url, page.locator("form#application-form").count()), (200, LEVER_APPLY_URL, 0))

    def test_form_again_is_a_200_with_the_form_still_there(self):
        fake, page = self.submit("form_again")
        self.new_document(page)
        self.assertEqual((fake.apply_posts()[0].status, page.url, page.locator("form#application-form").count()), (200, LEVER_APPLY_URL, 1))
        self.assertEqual(page.locator('[aria-invalid="true"]').count(), 0)

    def test_refused_is_a_4xx_with_the_form_and_a_marked_field(self):
        fake, page = self.submit("refused_4xx")
        self.new_document(page)
        page.wait_for_selector('input[name="email"][aria-invalid="true"]')
        self.assertEqual((fake.apply_posts()[0].status, page.url, page.locator("form#application-form").count()), (422, LEVER_APPLY_URL, 1))

    def test_server_error_is_a_5xx_without_the_form(self):
        fake, page = self.submit("server_5xx")
        page.wait_for_selector("text=Something went wrong")
        self.assertEqual((fake.apply_posts()[0].status, page.locator("form#application-form").count()), (500, 0))

    def test_the_browsers_own_required_checks_run_before_anything_is_posted(self):
        fake, page = self.open()
        page.fill('input[name="name"]', "Sam Rivera")
        page.click("#btn-submit")
        page.wait_for_timeout(600)
        self.assertEqual(fake.apply_posts(), [])
        self.assertEqual(self.token(page), "P1_fake-hcaptcha-token-1")   # hCaptcha ran first
        self.assertEqual(FakeLever.clicks(page)["submit"], 1)

    @staticmethod
    def token(page):
        return page.evaluate("document.getElementById('hcaptchaResponseInput').value")

    def test_enter_in_a_text_field_goes_through_hcaptcha_and_posts_once(self):
        fake, page = self.open()
        self.fill_required(page)
        page.locator('input[name="org"]').press("Enter")
        self.assertTrue(self.wait_until(page, lambda: fake.apply_posts()))
        self.assertRegex(fake.apply_posts()[0].text_values()["h-captcha-response"][0], r"^P1_fake-hcaptcha-token-")
        self.assertEqual(len(fake.apply_posts()), 1)

    def test_without_the_hcaptcha_script_submit_does_nothing_at_all(self):
        fake = FakeLever()
        fake.hcaptcha_loads = False
        fake, page = self.open(fake, wait_for_widget=False)
        self.fill_required(page)
        page.click("#btn-submit")
        page.wait_for_timeout(600)
        self.assertEqual(fake.apply_posts(), [])
        self.assertEqual(len(fake.requests_to(host="js.hcaptcha.com")), 1)
        self.assertEqual(page.evaluate("typeof window.hcaptcha"), "undefined")

    def test_a_challenge_waits_for_a_press_inside_its_frame_and_then_the_form_posts(self):
        fake = FakeLever()
        fake.challenge = True
        fake, page = self.open(fake)
        self.fill_required(page)
        page.click("#btn-submit")
        page.wait_for_selector(CHALLENGE_FRAME, state="visible")
        self.assertEqual(FakeLever.challenge_frames(page), 1)
        page.wait_for_timeout(400)
        self.assertEqual(fake.apply_posts(), [])
        self.assertEqual(FakeLever.clicks(page)["challenge"], 0)
        page.frame_locator(CHALLENGE_FRAME).locator("#solve").click()
        self.assertTrue(self.wait_until(page, lambda: fake.apply_posts()))
        self.assertEqual(len(fake.apply_posts()), 1)

    def test_closing_the_challenge_posts_nothing_and_the_switch_can_be_turned_off(self):
        fake = FakeLever("form_again")
        fake.challenge = True
        fake, page = self.open(fake)
        self.fill_required(page)
        page.click("#btn-submit")
        page.wait_for_selector(CHALLENGE_FRAME, state="visible")
        page.frame_locator(CHALLENGE_FRAME).locator("#close").click()
        self.assertTrue(self.wait_until(page, lambda: FakeLever.challenge_frames(page) == 0))
        self.assertEqual(fake.apply_posts(), [])
        self.assertEqual(FakeLever.clicks(page)["challenge"], 1)   # a press inside the frame is counted, whoever made it
        fake.challenge = False
        page.click("#btn-submit")
        self.assertTrue(self.wait_until(page, lambda: fake.apply_posts()))
        self.assertEqual(len(fake.apply_posts()), 1)

    def test_a_challenge_frame_shown_during_the_fill_is_not_a_submit(self):
        fake = FakeLever()
        fake.challenge_during_fill = (0.2, 1.4)
        fake, page = self.open(fake)
        page.wait_for_selector(CHALLENGE_FRAME, state="visible")
        self.fill_required(page)
        self.assertEqual(fake.apply_posts(), [])
        page.frame_locator(CHALLENGE_FRAME).locator("#solve").click()   # a passive challenge: solving it asks for no token
        self.assertTrue(self.wait_until(page, lambda: FakeLever.challenge_frames(page) == 0))
        page.wait_for_timeout(300)
        self.assertEqual((fake.apply_posts(), self.token(page)), ([], ""))

    def test_the_challenge_can_be_drawn_and_removed_from_the_test(self):
        fake, page = self.open()
        FakeLever.show_challenge(page)
        self.assertEqual(FakeLever.challenge_frames(page), 1)
        FakeLever.hide_challenge(page)
        self.assertEqual(FakeLever.challenge_frames(page), 0)

    def test_the_invisible_checkbox_frame_is_not_a_challenge(self):
        fake, page = self.open()
        self.assertEqual(page.locator('iframe[title^="Widget containing checkbox"]').count(), 1)
        self.assertEqual(FakeLever.challenge_frames(page), 0)

    def test_a_second_press_after_the_first_posts_again(self):
        fake, page = self.open(FakeLever("form_again"))
        self.fill_required(page)
        self.assertTrue(self.press(page, fake))
        page.wait_for_load_state("load")
        self.fill_required(page)
        page.click("#btn-submit")
        self.assertTrue(self.wait_until(page, lambda: len(fake.apply_posts()) == 2))


class PageBrowserTests(FakeLeverBrowserCase):
    """The rest of the page: the banner, Cloudflare, the other hosts and paths, the page-wide rules."""

    def test_the_cookie_banner_is_there_and_loading_the_page_clicks_nothing(self):
        fake, page = self.open()
        self.assertTrue(page.locator(".cc-window").is_visible())
        self.assertEqual(page.locator(".cc-window .cc-btn").all_inner_texts(), ["Deny", "Accept"])
        self.assertEqual(FakeLever.clicks(page), {"submit": 0, "hiddenSubmit": 0, "cookie": 0, "challenge": 0})
        page.locator(".cc-allow").click()   # the test plays someone who does click it
        self.assertEqual(FakeLever.clicks(page)["cookie"], 1)

    def test_the_first_response_sets_the_cloudflare_cookie(self):
        fake, page = self.open()
        self.assertIn("__cf_bm", [cookie["name"] for cookie in page.context.cookies()])

    def test_the_page_makes_the_tag_manager_and_bugsnag_requests_a_live_page_makes(self):
        fake, page = self.open()
        self.assertTrue(self.wait_until(page, lambda: fake.requests_to(host="notify.bugsnag.com") and fake.requests_to(host="www.googletagmanager.com")))
        (tag,) = fake.requests_to(host="www.googletagmanager.com")
        (beacon,) = fake.requests_to(host="notify.bugsnag.com")
        self.assertEqual((tag.method, tag.path, tag.status, tag.resource_type), ("GET", "/gtm.js", 200, "script"))
        self.assertEqual((beacon.method, beacon.status), ("POST", 200))
        self.assertEqual(fake.non_get_requests(), [beacon])
        self.assertEqual(fake.non_get_requests(noise=False), [])

    def test_the_cloudflare_script_loads_on_the_page_and_posts_a_beacon_when_asked(self):
        fake = FakeLever()
        fake.cloudflare_beacon = True
        fake, page = self.open(fake)
        self.assertTrue(self.wait_until(page, lambda: fake.requests_to(path=LEVER_CLOUDFLARE_BEACON_PATH)))
        (beacon,) = fake.requests_to(path=LEVER_CLOUDFLARE_BEACON_PATH)
        self.assertEqual((beacon.method, beacon.status), ("POST", 204))
        self.assertEqual(len(fake.requests_to(path="/cdn-cgi/challenge-platform/scripts/jsd/main.js")), 1)

    def test_the_interstitial_shows_until_the_form_appears(self):
        fake = FakeLever()
        fake.interstitial_s = 0.9
        context = self._browser.new_context(service_workers="block")
        self.addCleanup(context.close)
        self.addCleanup(fake.drop_unanswered)
        fake.install(context)
        page = context.new_page()
        page.set_default_timeout(8_000)
        page.goto(LEVER_APPLY_URL, wait_until="commit")
        page.wait_for_selector("form#application-form", state="attached")
        self.assertGreaterEqual(fake.interstitials_served, 1)
        self.assertGreaterEqual(len(fake.requests_to(path=LEVER_APPLY_PATH, method="GET")), 2)

    def test_a_missing_posting_answers_404_and_the_thanks_path_shows_its_page_with_no_post(self):
        fake = FakeLever()
        fake.closed = True
        context = self._browser.new_context(service_workers="block")
        self.addCleanup(context.close)
        fake.install(context)
        page = context.new_page()
        self.assertEqual(page.goto(LEVER_APPLY_URL).status, 404)
        self.assertEqual(page.locator("form#application-form").count(), 0)
        fake.closed = False
        fake, page = self.open(fake, LEVER_THANKS_URL)
        self.assertIn("Application submitted!", page.content())
        self.assertEqual((page.locator("form#application-form").count(), fake.non_get_requests()), (0, []))

    def test_the_eu_host_serves_the_same_form_and_reads_a_resume_on_its_own_host(self):
        fake, page = self.open(FakeLever(), LEVER_EU_APPLY_URL)
        self.attach(page)
        (post,) = fake.parse_posts()
        self.assertEqual(post.host, "jobs.eu.lever.co")
        self.assertEqual(self.values(page, "name")["name"], lever_parse_reply()["name"])

    def test_a_search_goes_to_the_pages_own_host(self):
        fake, page = self.open(FakeLever(), LEVER_EU_APPLY_URL)
        self.type_location(page, "Harbor")
        self.assertEqual({search.host for search in fake.search_gets()}, {"jobs.eu.lever.co"})

    def test_extra_scripts_run_and_their_requests_reach_the_fake_to_be_recorded(self):
        fake = FakeLever()
        fake.inject = [
            'fetch("https://telemetry.example.test/collect", {method: "POST", body: "x"}).catch(function () {});',
            'try { new WebSocket("wss://socket.example.test/live"); } catch (error) {}',
        ]
        fake, page = self.open(fake)
        self.assertTrue(self.wait_until(page, lambda: fake.requests_to(host="telemetry.example.test") and fake.websockets))
        self.assertEqual(fake.websockets, ["wss://socket.example.test/live"])
        self.assertEqual([seen.method for seen in fake.requests_to(host="telemetry.example.test")], ["POST"])

    def test_one_required_tick_relaxes_every_required_checkbox_on_the_page(self):
        fake, page = self.open(FakeLever(page="two_required_groups.html"))
        boxes = page.locator(".required-field input[type=checkbox]")
        required = lambda: page.evaluate("Array.from(document.querySelectorAll('.required-field input[type=checkbox]')).map((box) => box.required)")
        self.assertEqual(required(), [True] * 4)
        boxes.nth(0).check()
        self.assertEqual(required(), [False] * 4)
        boxes.nth(0).uncheck()
        self.assertEqual(required(), [True] * 4)
        boxes.nth(2).check()
        self.assertEqual(required(), [False] * 4)

    def test_any_disability_answer_makes_the_signature_and_date_required(self):
        fake, page = self.open()
        field = lambda name: page.evaluate("(name) => document.querySelector('[name=\"' + name + '\"]').required", name)
        self.assertEqual((field("eeo[disabilitySignature]"), field("eeo[disabilitySignatureDate]")), (False, False))
        select = page.locator('select[name="eeo[disability]"]')
        options = select.locator("option").evaluate_all("(items) => items.map((item) => item.value)")
        select.select_option(options[-1])   # the decline, or whatever is last: any answer counts
        self.assertEqual((field("eeo[disabilitySignature]"), field("eeo[disabilitySignatureDate]")), (True, True))
        select.select_option("")
        self.assertEqual((field("eeo[disabilitySignature]"), field("eeo[disabilitySignatureDate]")), (False, False))

    def test_the_page_fills_its_own_timezone_and_touches_no_other_hidden_field(self):
        fake, page = self.open()
        found = self.values(page, "timezone", "origin", "referer", "linkedInData", "socialSource", "source", "resumeStorageId", "h-captcha-response")
        self.assertNotEqual(found.pop("timezone"), "")
        self.assertEqual(set(found.values()), {""})


if __name__ == "__main__":
    unittest.main()
