"""Lever's application form in a browser: the selectors and the reads of the one page the app fills (docs/phase5-lever-handoff-spec.md 5.3, 6.3 to 6.9).

``LeverAdapter`` is to Lever what ``GreenhouseAdapter`` is to Greenhouse: it answers the agent's questions about a form (what kind of page this is,
what each control is, what the options of a list are) and changes nothing by itself. Whatever it does to the page goes through the agent (``ops``):
the five helpers that are the only places a page is changed. So this module holds no call that types, ticks, chooses, attaches or presses, and a
static test (tests/test_apply_agent_static.py) scans every Apply for me module for any that could.

What differs from Greenhouse, and why the agent asks for it through ``AdapterBase``:

- Lever's controls are plain HTML, so the page is read with a scan of its own (``LEVER_SCAN``), not the extension's engine (``uses_engine``).
- The page reads a résumé the moment it is attached and fills fields from it (``reads_on_attach``, ``parse_state``, ``guessed_fields``); the agent
  attaches first, waits for ``parse_state``, then fills, and clears what the plan had no source for (``cleared``).
- The location is a list the page searches as the student types (``is_typeahead``, ``fill_location``).
- The page keeps fields of its own (``owns``, ``page_managed``), among them the account number the résumé read must carry (``page_facts``).
- hCaptcha runs when the student presses Submit, and its challenge can show earlier (``waits_for_challenge``). The app never presses the two
  controls in ``DENYLIST`` (``refuses``), whatever else it is told to press.

Playwright is never imported here: every call is on the frame, page and locators the agent hands in.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from . import lever
from .agent_types import MAX_LOOKUP_OPTIONS, AdapterBase
from .checks import lever_loader_paths
from .lever_form import EEO_FIELDS, PAGE_MANAGED_FIELDS

# The two controls the page's Submit is made of: the visible button that runs hCaptcha, and the hidden one the page presses when the token arrives
# (spec 3.10). The student presses Submit, never the app (D1 B). The agent's click allowlist has no purpose that could reach either, and ``refuses``
# is the second lock: the only place in the Apply for me modules that names them (tests/test_apply_lever_adapter.py scans for it).
DENYLIST = ("btn-submit", "hcaptchaSubmitBtn")

# What the page's résumé reader (``/js/parseResume.js``) may fill, by the name of the control (spec 3.8, 6.7): the fields of its own list and every
# address part. ``selectedLocation`` goes with ``location``: the page rewrites it on every read, and empties it when ``location`` is left unchosen.
PARSER_FIELDS = (
    "org", "phone", "name", "email", "location", "urls[LinkedIn]", "urls[Twitter]", "urls[Quora]", "urls[GitHub]", "urls[Other]",
)
PARSER_PREFIX = "residentialLocation["
# The page sets these itself after the read; they are the page's, and not a change the app answers for.
PAGE_SETS = frozenset({"resumeStorageId"})

# The plan's name for the four EEO questions (``lever_form.EEO_FIELDS``) back to the control's name.
_DOM_NAME = {key: name for name, key in EEO_FIELDS.items()}
_TEMPLATE_NAME = re.compile(r"(?:cards|surveysResponses)\[[^\]]*\]\[(?:baseTemplate|surveyId|candidateSelectedLocation)\]")

# A list's option on the page. Its markup was not seen on a live board (spec 11, Q4 records the reply, not the page), so the three ways a list marks one.
OPTION_SELECTOR = '.dropdown-option, [role="option"], [data-option]'

ALREADY_SUBMITTED = "This page already says the application was submitted. The app did nothing."


def _css(text: str) -> str:
    """A string for use inside a double-quoted CSS attribute selector."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _norm(text: Any) -> str:
    return " ".join(str(text if text is not None else "").split()).casefold()


# --- Read-only scripts. Constants: nothing the student typed ever enters the page's JavaScript (the static test reads every evaluate call). ---------

# One entry per control of the form, in document order, in the shape ``checks.join`` reads (name, id, type, question, label, visible_css,
# required_any, widget, group_visible). The name is the plan's: an EEO control is reported under the name the plan gives its question. A hidden
# control is the page's; the free-text box that sits beside the pronoun boxes under their name is not a second question.
LEVER_SCAN = r"""() => {
  const form = document.querySelector("form#application-form");
  if (!form) return [];
  const squash = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const shown = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const box = el.getBoundingClientRect();
    return box.width >= 2 && box.height >= 2;
  };
  const eeo = {"eeo[gender]": "gender", "eeo[race]": "race", "eeo[veteran]": "veteran_status", "eeo[disability]": "disability_status"};
  const isChoice = (el) => ["radio", "checkbox"].includes((el.type || "").toLowerCase());
  const boxOf = (el) => el.closest("li.application-question, div.application-question") || el.parentElement || form;
  const questionOf = (el) => {
    const label = boxOf(el).querySelector(".application-label");
    if (!label) return null;
    const copy = label.cloneNode(true);
    copy.querySelectorAll(".required, p.description, script, style").forEach((node) => node.remove());
    return squash(copy.textContent);
  };
  const optionOf = (el) => {
    const wrap = el.closest("label");
    const alt = wrap ? wrap.querySelector(".application-answer-alternative") : null;
    return squash(alt ? alt.textContent : (wrap ? wrap.textContent : el.value));
  };
  const starred = (el) => {
    const label = boxOf(el).querySelector(".application-label");
    return !!label && (!!label.querySelector(".required") || label.textContent.includes("✱") || label.textContent.includes("*"));
  };
  const all = Array.from(form.querySelectorAll("input, select, textarea")).filter(
    (el) => !["hidden", "submit", "button", "image", "reset"].includes((el.type || "").toLowerCase()));
  const choiceNames = new Set(all.filter(isChoice).map((el) => el.name));
  return all.filter((el) => el.name && (isChoice(el) || !choiceNames.has(el.name))).map((el) => {
    const type = (el.type || "").toLowerCase();
    const kind = el.tagName === "SELECT" ? "select" : (el.tagName === "TEXTAREA" ? "textarea" : (type || "text"));
    const question = questionOf(el);
    const required = el.hasAttribute("required") || el.getAttribute("aria-required") === "true";
    return {
      name: eeo[el.name] || el.name, id: el.id || "", type: kind, question: question,
      label: isChoice(el) ? optionOf(el) : (question || ""), visible_css: kind === "file" ? shown(boxOf(el)) : shown(el),
      required_any: required || (kind === "file" && starred(el)), widget: kind === "file" ? "file_group" : "",
      group_visible: shown(boxOf(el)),
    };
  });
}"""

# The names of the visible controls, in document order (the résumé reader's fields are picked out of them).
LEVER_NAMES = r"""() => {
  const form = document.querySelector("form#application-form");
  if (!form) return [];
  return Array.from(form.querySelectorAll("input[name], select[name], textarea[name]"))
    .filter((el) => !["hidden", "submit", "button", "image", "reset"].includes((el.type || "").toLowerCase())).map((el) => el.name);
}"""

# Each hidden control of the form and the CAPTCHA's answer boxes, as [name, value]: what the page keeps for itself.
LEVER_MANAGED = r"""() => {
  const form = document.querySelector("form#application-form");
  if (!form) return [];
  return Array.from(form.querySelectorAll('input[type="hidden"], textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]'))
    .map((el) => [el.name || "", el.value || ""]);
}"""

# Every named control of the form that holds a text value, as [name, value]; the parser's own fields are picked out of them in Python. Used only to tell which fields
# the page's reader changed, so nothing is handed to the page: the script is constant and chooses nothing.
LEVER_VALUES = r"""() => {
  const form = document.querySelector("form#application-form");
  if (!form) return [];
  return Array.from(form.querySelectorAll("input[name], select[name], textarea[name]"))
    .filter((el) => !["submit", "button", "image", "reset", "file", "checkbox", "radio"].includes((el.type || "").toLowerCase()))
    .map((el) => [el.name, String(el.value || "")]);
}"""

# Which of the four indicators the page shows for a file it was given. At most one is shown at a time.
LEVER_PARSE_STATE = r"""() => {
  const shown = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  for (const kind of ["working", "success", "failure", "oversize"]) {
    if (shown(".resume-upload-" + kind)) return kind;
  }
  return "";
}"""

# The radios or checkboxes of one name as [{label, checked}]. An option's label is the page's own text for it, not the explanation beside it.
_LEVER_CHOICES = r"""(els) => els.map((e) => {
  const wrap = e.closest('label');
  const alt = wrap ? wrap.querySelector('.application-answer-alternative') : null;
  const text = alt ? alt.textContent : (wrap ? wrap.textContent : (e.value || ''));
  return {label: text.replace(/\s+/g, ' ').trim(), checked: !!e.checked};
})"""
_LEVER_KIND = r"""(els) => {
  const e = els[0];
  if (!e) return 'missing';
  const tag = e.tagName.toLowerCase();
  const type = (e.type || '').toLowerCase();
  if (tag === 'select') return 'select';
  if (tag === 'textarea') return 'textarea';
  if (type === 'radio') return 'radio';
  if (type === 'checkbox') return 'checkbox';
  if (type === 'file') return 'file';
  return 'text';
}"""
_NATIVE_OPTIONS = "(e) => Array.from(e.options).map((o) => o.textContent.replace(/\\s+/g, ' ').trim()).filter((t) => t)"
_DENIED = "(e) => !!e.closest(" + json.dumps(", ".join(f"#{name}" for name in DENYLIST)) + ")"


class LeverAdapter(AdapterBase):
    """Deterministic selectors and reads for Lever's one-page application form. Page text never chooses an action."""

    ats = lever.ATS_LEVER
    form_page_kind = "application_form"   # what ``detect_page`` answers for a form the app fills
    uses_engine = False
    closed_on_404 = True
    waits_for_challenge = True
    required_from_load = True   # the page's script takes ``required`` off every box once one is ticked (spec 3.11)
    page_sentences = {"confirmation": ALREADY_SUBMITTED}

    # --- the posting's address ----------------------------------------------------------------------------------

    @staticmethod
    def posting_ids(url: str) -> tuple[str, str]:
        """(site, posting uuid) of a posting address, for the check that the page opened is the posting asked for; ("", "") for any other."""
        ref = lever.from_url(url)
        return (ref.site.lower(), ref.job_id) if ref is not None else ("", "")

    @staticmethod
    def lookup_token(url: str) -> str:
        """Lever's lookup is one fixed path on the posting's own host: no token fills it."""
        return ""

    @staticmethod
    def confirmation_ids(url: str) -> tuple[str, str]:
        """What the policy's confirmation rule reads of the posting's address: the site and the posting uuid, as the path writes them."""
        ref = lever.from_url(url)
        return (ref.site, ref.job_id) if ref is not None else ("", "")

    @staticmethod
    def loader_paths(html: str, url: str = "") -> tuple[str, str, str]:
        """(the posting's host, the apply path, the confirmation path), from the address the page was loaded from: the form has no ``action`` (spec 3.2)."""
        return lever_loader_paths(url)

    # --- the page ---------------------------------------------------------------------------------------------

    def form_frame(self, page: Any) -> Any:
        return page.main_frame

    def detect_page(self, page: Any) -> str:
        """application_form, confirmation, challenge, offsite or unknown (a closed posting answers 404, which the agent reads before this)."""
        parts = urlsplit(page.url)
        host = (parts.hostname or "").lower().rstrip(".")
        if host not in lever.LEVER_HOSTS:
            return "offsite"
        if page.main_frame.locator("form#application-form").count():
            return "application_form"
        if parts.path.rstrip("/").endswith("/thanks"):
            return "confirmation"
        # A page with no form on an address that is not the confirmation page is Cloudflare's check (or one the app cannot tell from it): wait for the student.
        return "challenge"

    def uploads_on_attach(self, frame: Any) -> bool:
        """Lever sends nothing to a storage address as a file is attached: the file goes to the page's own origin (``reads_on_attach``)."""
        return False

    def reads_on_attach(self, frame: Any) -> bool:
        """The page reads a file as it is attached, sending it to Lever before Submit (spec 3.8). The student's setting decides whether the app attaches one."""
        return True

    def security_code_prompt(self, frame: Any) -> bool:
        return False   # Lever emails no code (spec 6.9, Q5)

    def security_code_inputs(self, frame: Any) -> list[Any] | None:
        return None

    def captcha_widget(self, frame: Any) -> str:
        return ""      # hCaptcha is invisible until the student presses Submit; a challenge frame is waited for, not noted (``waits_for_challenge``)

    # --- what the page keeps for itself ---------------------------------------------------------------------------

    def page_facts(self, frame: Any) -> dict[str, str]:
        """The account number in the page's own hidden field, which the page's file read carries and the request rules compare it with, and the
        storage id the page keeps for a file it read (spec 6.8: only read, never written). Both are read only."""
        return {
            "account_id": self._hidden(frame, "accountId"),
            "resume_storage_id": self._hidden(frame, "resumeStorageId"),
        }

    @staticmethod
    def _hidden(frame: Any, name: str) -> str:
        box = frame.locator(f'form#application-form input[name="{_css(name)}"]')
        return box.first.input_value() if box.count() == 1 else ""

    @staticmethod
    def owns(name: str) -> bool:
        """Whether a control of this name is one the page keeps for itself (spec 5.4 item 7)."""
        return name in PAGE_MANAGED_FIELDS or bool(_TEMPLATE_NAME.fullmatch(name))

    def page_managed(self, frame: Any) -> dict[str, str]:
        """The page's own hidden fields and their values now, except the ones the page sets itself after it read a file. The app writes none of these."""
        found: dict[str, str] = {}
        for name, value in frame.evaluate(LEVER_MANAGED):
            if self.owns(name) and name not in PAGE_SETS:
                found[name] = f"{found[name]}\n{value}" if name in found else value
        return found

    # --- the scan and the controls ---------------------------------------------------------------------------------

    def scan(self, frame: Any) -> list[dict[str, Any]]:
        """Every control of the form as ``checks.join`` reads it. Read only."""
        return list(frame.evaluate(LEVER_SCAN))

    @staticmethod
    def _name(key: str) -> str:
        return _DOM_NAME.get(key, key)

    def control(self, frame: Any, key: str) -> Any:
        """The control(s) named for ``key``. A radio group or the boxes of a question are several inputs. A hidden control is never one."""
        return frame.locator(f'form#application-form [name="{_css(self._name(key))}"]:not([type="hidden"])')

    def control_kind(self, frame: Any, key: str) -> str:
        """missing, select, textarea, radio, checkbox, file or text."""
        return str(self.control(frame, key).evaluate_all(_LEVER_KIND))

    def is_react_select(self, frame: Any, key: str) -> bool:
        return False   # Lever has no react-select: every list is a native select, radios or boxes

    def field_container(self, frame: Any, key: str) -> Any:
        """The question the control belongs to: its label, controls and any list that drops from it."""
        control = self.control(frame, key).first
        found = control.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' application-question ')][1]")
        return found.first if found.count() else control.locator("xpath=..")

    def choices(self, frame: Any, key: str) -> list[dict[str, Any]]:
        """The radios or checkboxes of ``key`` as [{"label", "checked"}]."""
        return list(self.control(frame, key).evaluate_all(_LEVER_CHOICES))

    def read_options(self, ops: Any, frame: Any, key: str, *, typed: str | None = None, limit: int = MAX_LOOKUP_OPTIONS) -> list[str]:
        """The option labels a list offers, in order and without repeats. Chooses nothing, and types nothing."""
        found = self.control(frame, key)
        if not found.count():
            return []
        kind = self.control_kind(frame, key)
        if kind == "select":
            texts = list(found.first.evaluate(_NATIVE_OPTIONS))
        elif kind in ("radio", "checkbox"):
            texts = [item["label"] for item in self.choices(frame, key)]
        else:
            texts = []
        options: list[str] = []
        for text in texts:
            if text and text not in options:
                options.append(text)
        return options[:limit]

    def refuses(self, locator: Any) -> bool:
        """Whether this element is, or is inside, one of the page's two Submit controls. When the page cannot say, it is refused."""
        try:
            return bool(locator.evaluate(_DENIED))
        except Exception:  # noqa: BLE001 - an element that cannot be asked is not pressed
            return True

    # --- the location -------------------------------------------------------------------------------------------

    def is_typeahead(self, frame: Any, key: str) -> bool:
        """The current location is searched as it is typed and kept only when one of the options is chosen (spec 3.9)."""
        return key == "location"

    def fill_location(self, ops: Any, frame: Any, key: str, label: str) -> str:
        """Type the city part of the confirmed label key by key, and choose the option whose text is the whole label (never the first one).

        "" when the field holds the label and the page's hidden ``selectedLocation`` names it. Otherwise the field is emptied again (a field left with
        no option chosen empties itself, and a half-typed city is never left) and the answer is the sentence of what went wrong.
        """
        control = self.control(frame, key).first
        container = self.field_container(frame, key)
        wanted = _norm(label)
        city = label.split(",")[0].strip() or label
        try:
            ops._type(control, city, key, search=True, keys=True)
            options = container.locator(OPTION_SELECTOR)
            waited, limit_ms = 0, int(ops.timeouts.choice_settle_s * 1000)
            while not options.count() and waited < limit_ms:
                frame.page.wait_for_timeout(100)   # the page asks its lookup after a pause, and the answer draws the options
                waited += 100
            ops._settle_choice(frame.page)
            matches = [index for index, text in enumerate(options.all_inner_texts()) if _norm(text) == wanted]
            if len(matches) == 1:
                ops._click(options.nth(matches[0]), "option_pick", key)
                ops._settle_choice(frame.page)
                if self._holds(frame, key, wanted):
                    return ""
            ops._type(control, "", key)   # an empty field, left: the page empties its hidden field with it
            return ops._field_took(key)
        finally:
            ops._release()

    def _holds(self, frame: Any, key: str, wanted: str) -> bool:
        """The field shows the label and the hidden field is the JSON of an option of that name (spec 6.8)."""
        try:
            if _norm(self.control(frame, key).first.input_value()) != wanted:
                return False
            chosen = json.loads(self._selected(frame))
        except (ValueError, TypeError):
            return False
        return isinstance(chosen, dict) and _norm(chosen.get("name")) == wanted

    @staticmethod
    def _selected(frame: Any) -> str:
        box = frame.locator('form#application-form input[name="selectedLocation"]')
        return box.first.input_value() if box.count() == 1 else ""

    # --- the résumé reader ---------------------------------------------------------------------------------------

    def parse_state(self, frame: Any) -> str:
        """working, success, failure or oversize while the page shows one of them, else ""."""
        return str(frame.evaluate(LEVER_PARSE_STATE))

    def guessed_fields(self, frame: Any) -> list[str]:
        """The controls of the form that the page's reader fills (``PARSER_FIELDS`` and every address part), in the form's order, without repeats."""
        found: list[str] = []
        for name in frame.evaluate(LEVER_NAMES):
            if (name in PARSER_FIELDS or name.startswith(PARSER_PREFIX)) and name not in found:
                found.append(name)
        return found

    def parser_values(self, frame: Any) -> dict[str, str]:
        """What each field of ``guessed_fields`` holds now, with ``selectedLocation`` beside ``location`` (the page rewrites it on every read)."""
        found: dict[str, str] = {}
        for name, value in frame.evaluate(LEVER_VALUES):
            if name in PARSER_FIELDS or name.startswith(PARSER_PREFIX) or name == "selectedLocation":
                found[name] = value
        return found

    def cleared(self, frame: Any, key: str) -> bool:
        """The control holds nothing, and for the location neither does the hidden field the page keeps beside it."""
        control = self.control(frame, key)
        if not control.count():
            return True
        if control.first.input_value() != "":
            return False
        return key != "location" or self._selected(frame) == ""
