"""Lever's application form, read from the page's own HTML: what the form asks, with no browser.

A Lever posting's application page is the schema (docs/phase5-lever-handoff-spec.md, sections 3 and 5.4). The standard
fields have fixed names, and every company-written question is a "card" whose controls are named
``cards[<uuid>][field<N>]`` beside a hidden ``cards[<uuid>][baseTemplate]`` that holds the question set as JSON. A survey
is the same with ``surveysResponses[<uuid>][responses][field<N>]``. ``parse_lever_form`` is what ``policy.parse_schema`` is
for Greenhouse's listing: it yields the same ``SchemaField`` rows, so the plan, the classifier and the sensitive-answer
rules need nothing new.

The JSON says what a question is. The page says what can be filled. A field is readable only when the two agree on its
control type, on whether it is required (as the page was loaded: the page relaxes ``required`` after a tick, spec 3.11) and
on its options. A field that fails any rule is reported as unreadable and is left for the student, never guessed. A named
control the parser has no family for is reported as unknown.

Pure: ``html.parser`` over the text it is given, no I/O, no network, no browser. Nothing in the app calls it yet.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping

from pipeline_core.identity import normalized

from .policy import MAX_DESCRIPTION_CHARS, SchemaField

__all__ = [
    "EEO_FIELDS", "EEO_SIGNATURE_FIELDS", "LeverForm", "LeverPosting", "PAGE_MANAGED_FIELDS", "UnknownControl", "UnreadableField",
    "parse_lever_form",
]

# --- The limits of 5.4 item 4 ----------------------------------------------------------------------------------------

MAX_TEMPLATE_BYTES = 2 * 1024 * 1024  # a university dropdown's template was 622 KB, with 3,302 options
MAX_TEMPLATE_FIELDS = 200
MAX_FIELD_OPTIONS = 20_000
MAX_NESTED_FIELDSETS = 100  # disabled fieldsets open inside one another
MAX_OPEN_LABELS = 50  # <label> elements open inside one another (real pages have none, HTML allows none)
CARD_TYPES = ("text", "textarea", "dropdown", "multiple-choice", "multiple-select", "file-upload")

# Lever's field type -> the type the same control has in Greenhouse's listing, which ``policy.control_of`` reads.
_SCHEMA_TYPE = {
    "text": "input_text", "textarea": "textarea", "dropdown": "multi_value_single_select",
    "multiple-choice": "multi_value_single_select", "multiple-select": "multi_value_multi_select", "file-upload": "input_file",
}

# --- Names the page owns or the app knows ----------------------------------------------------------------------------

# 5.4 item 7. The app writes none of these, and none becomes a schema field, when every control under the name is hidden
# (a visible control with one of these names is a question, listed as unknown). hCaptcha's textarea is the one exception.
# (Every ``[baseTemplate]``, ``[surveyId]`` and ``[candidateSelectedLocation]`` is page-managed too; those are matched by shape below.)
_CAPTCHA = "h-captcha-response"  # hCaptcha writes its answer into a textarea; it is the page's whatever the control is
PAGE_MANAGED_FIELDS = frozenset({
    "accountId", "linkedInData", "origin", "referer", "timezone", "socialReferralKey", "socialSource", "resumeStorageId",
    _CAPTCHA, "source",
})
# 6.6: the four EEO questions, under the names Phase 5's classifier knows them by (classify._EEOC_NAMES).
EEO_FIELDS = {"eeo[gender]": "gender", "eeo[race]": "race", "eeo[veteran]": "veteran_status", "eeo[disability]": "disability_status"}
# Typed on the page by the student: a name and a date. Listed so the plan can show them, and never filled (6.6).
EEO_SIGNATURE_FIELDS = ("eeo[disabilitySignature]", "eeo[disabilitySignatureDate]")

_UUID = r"[0-9A-Za-z-]{1,64}"
_INDEX = r"(0|[1-9][0-9]{0,5})"
_TEMPLATE_NAME = re.compile(rf"(cards|surveysResponses)\[({_UUID})\]\[baseTemplate\]")
_PAGE_SUFFIX = re.compile(rf"surveysResponses\[{_UUID}\]\[(?:surveyId|candidateSelectedLocation)\]")
_CARD_FIELD = re.compile(rf"cards\[({_UUID})\]\[field{_INDEX}\]")
_SURVEY_FIELD = re.compile(rf"surveysResponses\[({_UUID})\]\[responses\]\[field{_INDEX}\]")
_URLS = re.compile(r"urls\[.+\]", re.DOTALL)
_RESIDENTIAL = re.compile(r"residentialLocation\[.+\]", re.DOTALL)

_TEXT_KINDS = frozenset({"text", "email", "tel", "url", "search"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_NOT_DATA_INPUTS = frozenset({"submit", "button", "image", "reset"})


def _collapse(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


# --- What the parser returns -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UnreadableField:
    """A field the parser will not describe: the JSON and the page disagree, a limit was passed, or the page has no control.

    ``required`` is true when either the JSON or the page says so, so an unreadable required question is a problem for
    the student to answer, not something the plan can skip. ``reason`` is plain words for the student.
    """

    name: str
    label: str
    required: bool
    reason: str


@dataclass(frozen=True)
class UnknownControl:
    """A named control outside every family the parser knows (5.4 item 6). The plan lists it and never fills it."""

    name: str
    tag: str
    type: str
    label: str
    required: bool
    disabled: bool = False


@dataclass(frozen=True)
class LeverPosting:
    """What the page says about the posting. ``company_title`` is the page's ``<title>``, ``"{Company} - {Role}"`` (3.8)."""

    company_title: str = ""

    def matches(self, company: str, title: str) -> bool:
        """Whether the page title begins with the saved company and carries the saved title after it (5.4 item 8).

        The page title is ``"{Company} - {Role}"``, so the company is the start of it: a company named only in the role
        half is another employer's posting. A role can contain " - ", so the title is never split. Both are compared as
        normalized words. When either is missing on either side the answer is no, and the student ticks that the posting
        is the one meant.
        """
        page = f" {normalized(self.company_title)} "
        employer = normalized(_collapse(company))
        role = normalized(_collapse(title))
        if not employer or not role or not page.startswith(f" {employer} "):
            return False
        return f" {role} " in page[len(employer) + 1:]


@dataclass(frozen=True)
class LeverForm:
    """The form: ``fields`` in document order (cards in the order their JSON lists them), what could not be read, and the posting."""

    fields: tuple[SchemaField, ...]
    posting: LeverPosting
    unreadable: tuple[UnreadableField, ...] = ()
    unknown: tuple[UnknownControl, ...] = ()


# --- Walking the page ------------------------------------------------------------------------------------------------


class _Open:
    """The tags still open, with a count per name so that an end tag that closes nothing is turned away at once.

    Pages are up to 4 MB and may be broken or hostile: scanning the whole stack for every stray end tag would take time in
    proportion to the square of the page.
    """

    __slots__ = ("items", "counts", "flagged")

    def __init__(self) -> None:
        self.items: list[tuple[str, bool]] = []
        self.counts: Counter[str] = Counter()
        self.flagged = 0  # how many open tags carry the flag

    def __len__(self) -> int:
        return len(self.items)

    def push(self, tag: str, flag: bool = False) -> None:
        self.items.append((tag, flag))
        self.counts[tag] += 1
        self.flagged += flag

    def close(self, tag: str) -> int | None:
        """Close the newest open ``tag`` and what was left open inside it. How many flagged tags that closed, or None if none was open."""
        if not self.counts[tag]:
            return None
        index = len(self.items) - 1
        while self.items[index][0] != tag:
            index -= 1
        removed = self.items[index:]
        del self.items[index:]
        gone = 0
        for name, flag in removed:
            self.counts[name] -= 1
            gone += flag
        self.flagged -= gone
        return gone


class _LabelEl:
    """One ``<label>`` element: where its text starts and ends in the page's shared run of text chunks, and its ``for``."""

    __slots__ = ("chunks", "start", "end", "target", "__collapsed")

    def __init__(self, chunks: list[str], target: str) -> None:
        self.chunks = chunks
        self.start = len(chunks)
        self.end: int | None = None  # None while it is open: its text runs to the end of the page
        self.target = target
        self.__collapsed: str | None = None

    def text(self) -> str:
        """The label's words with their spaces collapsed, worked out once.

        Many controls can share one label (a group inside one ``<label>``, or many controls with the same ``id`` that one
        ``label[for]`` names), and the label's text can be as long as the page. Joining and collapsing it for each control would
        take time in proportion to the controls times the text. This is read only after the page has been walked, when the
        shared run of chunks no longer grows.
        """
        if self.__collapsed is None:
            self.__collapsed = _collapse("".join(self.chunks[self.start:self.end]))
        return self.__collapsed


class _Control:
    """One control as the page wrote it. For a radio or a checkbox it is one option of its group."""

    __slots__ = ("seq", "tag", "kind", "name", "required", "disabled", "value", "label", "starred", "dom_id", "options", "span", "label_el", "outside")

    def __init__(self, seq: int, tag: str, kind: str, name: str, required: bool, disabled: bool, value: str | None, label: str,
                 starred: bool, dom_id: str, label_el: "_LabelEl | None") -> None:
        self.seq = seq
        self.tag = tag
        self.kind = kind
        self.name = name
        self.required = required
        self.disabled = disabled
        self.value = value
        self.label = label
        self.starred = starred  # the question's label shows the required marker (the page's own attribute may be missing, see _standard_field)
        self.dom_id = dom_id
        self.options: list[tuple[str, str]] = []  # a select's (value, label as the page shows it)
        self.span: list[str] | None = None  # the text of the option's own ``application-answer-alternative`` span
        self.label_el = label_el  # the text of the ``<label>`` around a radio or checkbox, when no span names the option
        self.outside = False  # written outside the form element and joined to it by its ``form`` attribute

    def option_label(self) -> str:
        found = _collapse("".join(self.span)) if self.span is not None else ""
        if not found and self.label_el is not None:
            found = self.label_el.text()
        return found or _collapse(self.value or "")

    def option_value(self) -> str:
        return "on" if self.value is None else self.value


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.title_open = False
        self.title_done = False
        self.svg_depth = 0
        self.__raw = False  # inside a script or a style, whose text is never the page's
        self.form_seen = False
        self.in_form = False
        self.__forms_open = 0  # other forms that are open: a form inside one is not a form the browser builds
        self.__fieldsets: list[list[int | None]] = []  # open ``fieldset[disabled]``: [stack index, index of its first legend, -1 once that closed]
        self.controls: list[_Control] = []
        self.for_labels: dict[str, _LabelEl] = {}
        self.__stack = _Open()  # flagged: starts a scope that owns the current label
        self.__label_div: _Open | None = None  # the tags open inside the current div.application-label; flagged: their text is not the label's
        self.__label_buf: list[str] = []
        self.__label_text = ""
        self.__label_starred = False
        self.__starred_now = False
        self.__chunks: list[str] = []  # the text seen while any <label> was open, once, shared by every label
        self.__label_els: list[_LabelEl] = []  # open <label> elements
        self.__select: _Control | None = None
        self.__option: tuple[str | None, str | None, list[str]] | None = None
        self.__pending: _Control | None = None  # the radio or checkbox waiting for its option span
        self.__span: _Open | None = None
        self.__seq = 0

    # --- tags

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a: dict[str, str | None] = {}
        for key, value in attrs:
            a.setdefault(key, value)
        classes = set((a.get("class") or "").split())
        if tag in ("script", "style"):
            self.__raw = True
        if tag == "svg":
            self.svg_depth += 1
        if tag == "title" and self.svg_depth == 0 and not self.title_done:
            self.title_open = True
        if tag == "form":
            if not self.form_seen and a.get("id") == "application-form":
                if not self.__forms_open:  # inside another form the browser drops this tag, so the page has no application form
                    self.form_seen = self.in_form = True
            elif not self.in_form:
                self.__forms_open += 1
        if not self.in_form:
            if tag in ("input", "select", "textarea") and a.get("form") == "application-form":
                self.__outside(tag, a)
            if tag not in _VOID:
                self.__stack.push(tag)
            return
        scope = "application-question" in classes or "section" in classes
        if scope:
            self.__label_text, self.__label_starred = "", False
        if tag == "legend":
            for fieldset in self.__fieldsets:
                if fieldset[1] is None and len(self.__stack) == fieldset[0] + 1:  # the fieldset's first legend, directly inside it
                    fieldset[1] = len(self.__stack)
        if tag not in _VOID:
            self.__stack.push(tag, scope)
        if tag == "fieldset" and "disabled" in a:
            if len(self.__fieldsets) >= MAX_NESTED_FIELDSETS:
                raise ValueError("too many disabled fieldsets inside one another")
            self.__fieldsets.append([len(self.__stack) - 1, None])
        if self.__label_div is not None:
            if tag not in _VOID:
                skip = tag in ("svg", "script", "style") or (tag == "span" and "required" in classes) or (tag == "p" and "description" in classes)
                self.__starred_now = self.__starred_now or (tag == "span" and "required" in classes)
                self.__label_div.push(tag, skip)
        elif tag == "div" and "application-label" in classes:
            self.__label_div = _Open()
            self.__label_div.push("div")
            self.__label_buf = []
            self.__starred_now = False
        if self.__span is not None and tag not in _VOID:
            self.__span.push(tag)
        elif tag == "span" and "application-answer-alternative" in classes and self.__pending is not None:
            self.__span = _Open()
            self.__span.push("span")
            self.__pending.span = []
        if tag == "label":
            if len(self.__label_els) >= MAX_OPEN_LABELS:
                raise ValueError("too many labels inside one another")
            self.__label_els.append(_LabelEl(self.__chunks, a.get("for") or ""))
        elif tag == "input":
            self.__input(a)
        elif tag == "select":
            self.__select = self.__control(a, "select", "select-multiple" if "multiple" in a else "select")
            self.__pending = None
        elif tag == "option" and self.__select is not None:
            self.__end_option()
            self.__option = (a.get("value"), a.get("label"), [])
        elif tag == "textarea":
            self.__control(a, "textarea", "textarea")
            self.__pending = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.__raw = False
        if tag == "svg" and self.svg_depth:
            self.svg_depth -= 1
        if tag == "title" and self.title_open:
            self.title_open = False
            self.title_done = True
        if not self.in_form:
            if tag == "form":
                self.__forms_open = max(0, self.__forms_open - 1)
            self.__pop(tag)
            return
        if tag == "option":
            self.__end_option()
        elif tag == "select":
            self.__end_option()
            self.__select = None
        elif tag == "label" and self.__label_els:
            label = self.__label_els.pop()
            label.end = len(self.__chunks)
            if label.target:
                self.for_labels.setdefault(label.target, label)
        if self.__label_div is not None:
            self.__label_div.close(tag)
            if not self.__label_div:
                self.__label_div = None
                self.__label_text, self.__label_starred = _collapse("".join(self.__label_buf)), self.__starred_now
        if self.__span is not None:
            self.__span.close(tag)
            if not self.__span:
                self.__span = None
                self.__pending = None
        scoped = self.__pop(tag)
        if scoped:
            self.__label_text, self.__label_starred = "", False
        if tag == "form":
            self.in_form = False

    def __pop(self, tag: str) -> bool:
        """Close ``tag`` and what was left open inside it. True when that closed a scope that owns the current label."""
        scoped = self.__stack.close(tag)
        if scoped is None:
            return False
        self.__close_fieldsets()
        return bool(scoped)

    def __close_fieldsets(self) -> None:
        size = len(self.__stack)
        self.__fieldsets = [fieldset for fieldset in self.__fieldsets if fieldset[0] < size]
        for fieldset in self.__fieldsets:
            if fieldset[1] is not None and fieldset[1] >= size:
                fieldset[1] = -1  # its legend has closed

    def __in_disabled_fieldset(self) -> bool:
        """True inside a ``fieldset[disabled]``, unless the control is within that fieldset's first legend (HTML)."""
        return any(fieldset[1] is None or fieldset[1] < 0 for fieldset in self.__fieldsets)

    # --- text

    def handle_data(self, data: str) -> None:
        if self.title_open:
            self.title.append(data)
        if not self.in_form or self.__raw:
            return
        if self.__label_els:
            self.__chunks.append(data)
        if self.__label_div is not None and not self.__label_div.flagged:
            self.__label_buf.append(data)
        if self.__span is not None and self.__pending is not None and self.__pending.span is not None:
            self.__pending.span.append(data)
        if self.__option is not None:
            self.__option[2].append(data)

    # --- controls

    def __outside(self, tag: str, a: Mapping[str, str | None]) -> None:
        """A control written outside the form that names it in its ``form`` attribute is submitted with it. It is only ever listed as unknown."""
        kind = (a.get("type") or "text").strip().lower() if tag == "input" else ("select-multiple" if tag == "select" and "multiple" in a else tag)
        if kind in _NOT_DATA_INPUTS:
            return
        self.__seq += 1
        control = _Control(self.__seq, tag, kind, a.get("name") or "", "required" in a, "disabled" in a, a.get("value"), "", False, a.get("id") or "", None)
        control.outside = True
        self.controls.append(control)

    def __control(self, a: Mapping[str, str | None], tag: str, kind: str) -> _Control | None:
        """The control, or None when its ``form`` attribute puts it in another form (or in none): it is not submitted with this one."""
        if "form" in a and a["form"] != "application-form":
            return None
        self.__seq += 1
        control = _Control(
            self.__seq, tag, kind, a.get("name") or "", "required" in a, "disabled" in a or self.__in_disabled_fieldset(), a.get("value"), self.__label_text,
            self.__label_starred, a.get("id") or "", self.__label_els[-1] if self.__label_els else None,
        )
        self.controls.append(control)
        return control

    def __input(self, a: Mapping[str, str | None]) -> None:
        kind = (a.get("type") or "text").strip().lower()
        self.__pending = None
        if kind in _NOT_DATA_INPUTS:
            return
        control = self.__control(a, "input", kind)
        if control is None:
            return
        if kind in ("checkbox", "radio") and control.name:
            self.__pending = control
        else:
            control.label_el = None

    def __end_option(self) -> None:
        if self.__option is not None and self.__select is not None:
            value, label_attribute, chunks = self.__option
            text = _collapse("".join(chunks))
            # HTML: the label is the ``label`` attribute when it is not empty, else the text; the value is the ``value`` attribute, else the text.
            self.__select.options.append((text if value is None else value, _collapse(label_attribute) or text))
        self.__option = None


# --- Reading the controls --------------------------------------------------------------------------------------------


def _first_label(controls: list[_Control]) -> str:
    for control in controls:
        if control.kind != "hidden" and control.label:
            return control.label
    return ""


def _group_options(controls: list[_Control]) -> list[str]:
    """The option labels a group of controls offers: a select's options, or one per radio or checkbox. An empty value is no option."""
    if len(controls) == 1 and controls[0].tag == "select":
        return [label for value, label in controls[0].options if value != ""]
    return [control.option_label() for control in controls if control.kind in ("checkbox", "radio") and control.option_value() != ""]


def _answers_match_labels(controls: list[_Control]) -> bool:
    """Whether every real option submits the answer it shows: its value, once tidied, is its label. EEO and fixed fields do not use this."""
    if len(controls) == 1 and controls[0].tag == "select":
        return all(_collapse(value) == label for value, label in controls[0].options if value != "")
    return all(_collapse(control.option_value()) == control.option_label() for control in controls
               if control.kind in ("checkbox", "radio") and control.option_value() != "")


def _required(controls: list[_Control]) -> bool | None:
    """Whether the page requires the group as it was loaded: True, False, or None when only some of its controls ask for it."""
    flags = {control.required for control in controls if control.kind != "hidden"}
    return flags.pop() if len(flags) == 1 else (False if not flags else None)


def _template(values: list[str]) -> tuple[list[Any] | None, str]:
    """(the question set's fields, "") or (None, why not). The limits of 5.4 item 4: all of them fail to unreadable."""
    if not values:
        return None, "the page has no description of these questions"
    if len(values) != 1:
        return None, "the page describes these questions twice"
    if len(values[0].encode("utf-8", "replace")) > MAX_TEMPLATE_BYTES:
        return None, "the description of these questions is too large to read"
    try:
        parsed = json.loads(values[0])
    except (ValueError, RecursionError):
        return None, "the description of these questions could not be read"
    if not isinstance(parsed, dict):
        return None, "the description of these questions is not what the app expects"
    fields = parsed.get("fields")
    if not isinstance(fields, list):
        return None, "the description of these questions lists no fields"
    if len(fields) > MAX_TEMPLATE_FIELDS:
        return None, "the description of these questions lists too many fields to read"
    return fields, ""


def _json_options(field: Mapping[str, Any]) -> list[str] | None:
    options = field.get("options", [])
    if options is None:
        options = []
    if not isinstance(options, list) or len(options) > MAX_FIELD_OPTIONS:
        return None
    labels = []
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("text"), str):
            return None
        labels.append(_collapse(option["text"]))
    return labels


def _card_field(json_field: Any, controls: list[_Control] | None, name: str, previous: str) -> SchemaField | UnreadableField:
    """One card or survey question: the JSON entry for it, joined to the page's controls under the same name."""
    dom_label = _first_label(controls) if controls else ""
    dom_required = _required(controls) if controls else False
    # Any control that asks for it makes the question required (a radio group, a box that must be ticked), even where the page is not uniform.
    page_asks = any(control.required for control in controls if control.kind != "hidden") if controls else False

    def unreadable(reason: str, label: str = "", required: bool = False) -> UnreadableField:
        return UnreadableField(name, label or dom_label or name, bool(required or page_asks), reason)

    if not isinstance(json_field, dict):
        return unreadable("the page describes this question in a way the app does not read")
    kind, text = json_field.get("type"), json_field.get("text")
    label = _collapse(text)
    json_required = json_field.get("required", False)
    if not isinstance(json_required, bool):
        return unreadable("the page describes this question in a way the app does not read", label, bool(json_required))
    if not isinstance(kind, str) or kind not in CARD_TYPES or not isinstance(text, str) or not label:
        return unreadable("the app does not read this kind of question", label, json_required)
    description = json_field.get("description", "")
    if description is not None and not isinstance(description, str):
        return unreadable("the page describes this question in a way the app does not read", label, json_required)
    listed = _json_options(json_field) if kind in ("dropdown", "multiple-choice", "multiple-select") else []
    if listed is None:
        return unreadable("this question has more options than the app reads", label, json_required)
    if not controls:
        return unreadable("the page has no control for this question", label, json_required)
    if any(control.disabled for control in controls):
        return unreadable("the page has turned this question off", label, json_required)
    # The control must be the kind the JSON says (5.4 item 5).
    kinds = {control.kind for control in controls}
    fits = {
        "dropdown": len(controls) == 1 and kinds == {"select"},
        "multiple-choice": kinds == {"radio"},
        "multiple-select": kinds == {"checkbox"},
        "text": len(controls) == 1 and kinds == {"text"},
        "textarea": len(controls) == 1 and kinds == {"textarea"},
        "file-upload": len(controls) == 1 and kinds == {"file"},
    }[kind]
    if not fits:
        return unreadable("the page's control for this question is not the kind its description says", label, json_required)
    if dom_required is None or dom_required != json_required:
        return unreadable("the page and its description disagree about whether this question is required", label, json_required)
    if kind in ("dropdown", "multiple-choice", "multiple-select") and Counter(_group_options(controls)) != Counter(listed):
        return unreadable("the page's options for this question are not the ones its description lists", label, json_required)
    if kind in ("dropdown", "multiple-choice", "multiple-select") and not _answers_match_labels(controls):
        return unreadable("the page shows answers for this question that are not the ones it submits", label, json_required)
    raw = description or ""
    return SchemaField(
        name=name, label=label, required=json_required, type=_SCHEMA_TYPE[kind], options=tuple(listed), section="custom",
        parent=previous, description=raw[:MAX_DESCRIPTION_CHARS], description_cut=len(raw) > MAX_DESCRIPTION_CHARS,
    )


def _standard_field(name: str, controls: list[_Control]) -> SchemaField | None:
    """The schema row for a fixed field, or None when the page's control is not the kind the field has (it is then unknown)."""
    kinds = {control.kind for control in controls}
    visible = [control for control in controls if control.kind != "hidden"]
    # A required résumé is marked by the "required" star in its label: the hidden file input carries no attribute (spec 3, 2026-10-08 note).
    required = any(control.required or control.starred for control in visible)
    label = _first_label(controls)
    options: tuple[str, ...] = ()
    if name == "resume":
        ok, kind = len(controls) == 1 and kinds == {"file"}, "input_file"
    elif name == "comments":
        ok, kind = len(controls) == 1 and kinds == {"textarea"}, "textarea"
    elif name == "selectedLocation":
        ok, kind = len(controls) == 1 and kinds == {"hidden"}, "input_hidden"
    elif name == "opportunityLocationId":
        ok, kind = len(controls) == 1 and kinds == {"select"}, "multi_value_single_select"
        options = tuple(_group_options(controls))
    elif name == "pronouns":
        ok, kind = "checkbox" in kinds and kinds <= {"checkbox", "text"}, "multi_value_multi_select"
        options = tuple(_group_options([control for control in controls if control.kind == "checkbox"]))
    elif name == "consent[marketing]":
        boxes = [control for control in controls if control.kind == "checkbox"]
        ok, kind = len(boxes) == 1 and kinds <= {"hidden", "checkbox"}, "multi_value_multi_select"
        options = tuple(_group_options(boxes))
        label = label or (options[0] if options else "")
    else:  # name, email, phone, location, org, urls[...], residentialLocation[...]
        ok, kind = len(controls) == 1 and kinds <= _TEXT_KINDS, "input_text"
    if not ok or any(control.disabled for control in controls):
        return None
    return SchemaField(name=name, label=label or name, required=required, type=kind, options=options, section="standard")


def _is_standard(name: str) -> bool:
    return (
        name in ("resume", "name", "email", "phone", "location", "selectedLocation", "org", "comments", "pronouns", "opportunityLocationId",
                 "consent[marketing]")
        or bool(_URLS.fullmatch(name)) or bool(_RESIDENTIAL.fullmatch(name))
    )


def _eeo_field(name: str, controls: list[_Control], parent: str) -> SchemaField | None:
    kinds = {control.kind for control in controls}
    if any(control.disabled for control in controls):
        return None
    label = _first_label(controls)
    if name in EEO_FIELDS:
        if not (kinds == {"radio"} or (len(controls) == 1 and kinds == {"select"})):
            return None
        required = _required(controls)
        if required is None:
            return None
        return SchemaField(
            name=EEO_FIELDS[name], label=label or name, required=required, type="multi_value_single_select",
            options=tuple(_group_options(controls)), section="demographic",
        )
    if len(controls) != 1 or kinds != {"text"}:
        return None
    return SchemaField(name=name, label=label or name, required=controls[0].required, type="input_text", section="demographic", parent=parent)


def _unknown(name: str, controls: list[_Control]) -> UnknownControl:
    kinds = sorted({control.kind for control in controls})
    return UnknownControl(
        name=name, tag=controls[0].tag, type=kinds[0] if len(kinds) == 1 else "mixed", label=_first_label(controls),
        required=any(control.required or control.starred for control in controls if control.kind != "hidden"), disabled=any(control.disabled for control in controls),
    )


def parse_lever_form(html: str) -> LeverForm | None:
    """The form a Lever application page carries, or None when the page has no ``form#application-form`` (5.4 item 1).

    None is "this page is not a form", which is not the same as an empty form. Walks the controls with ``html.parser``
    in document order, never a pattern over the whole page.
    """
    scanner = _Scanner()
    try:
        scanner.feed(str(html or ""))
        scanner.close()
    except (AssertionError, ValueError, RecursionError):
        return None
    if not scanner.form_seen:
        return None
    for control in scanner.controls:
        if not control.label and control.dom_id in scanner.for_labels:
            control.label = scanner.for_labels[control.dom_id].text()
    groups: dict[str, list[_Control]] = {}
    for control in scanner.controls:
        if control.name:  # a control with no name is not submitted: never filled, never listed (5.4 item 3)
            groups.setdefault(control.name, []).append(control)

    entries: list[tuple[int, int, SchemaField | UnreadableField | UnknownControl]] = []
    sets: dict[tuple[str, str], dict[str, Any]] = {}

    def question_set(family: str, uuid: str, seq: int) -> dict[str, Any]:
        found = sets.setdefault((family, uuid), {"seq": seq, "templates": [], "fields": {}})
        found["seq"] = min(found["seq"], seq)
        return found

    disability_label = _first_label(groups["eeo[disability]"]) if "eeo[disability]" in groups else ""
    for name, controls in groups.items():
        seq = controls[0].seq
        template = _TEMPLATE_NAME.fullmatch(name)
        card, survey = _CARD_FIELD.fullmatch(name), _SURVEY_FIELD.fullmatch(name)
        if (name in PAGE_MANAGED_FIELDS or _PAGE_SUFFIX.fullmatch(name)) and (name == _CAPTCHA or all(c.kind == "hidden" for c in controls)):
            continue  # the page's own hidden field; a visible control that shares its name is a question and is listed below
        if any(control.outside for control in controls):  # written outside the form: the parser cannot say what it is, so it is only listed
            entries.append((seq, 0, _unknown(name, controls)))
            continue
        if template:
            question_set(template.group(1), template.group(2), seq)["templates"].extend(control.value or "" for control in controls)
        elif card or survey:
            match, family = (card, "cards") if card else (survey, "surveysResponses")
            question_set(family, match.group(1), seq)["fields"][int(match.group(2))] = (name, controls)
        elif _is_standard(name):
            found = _standard_field(name, controls)
            entries.append((seq, 0, found if found is not None else _unknown(name, controls)))
        elif name in EEO_FIELDS or name in EEO_SIGNATURE_FIELDS:
            found = _eeo_field(name, controls, disability_label)
            entries.append((seq, 0, found if found is not None else _unknown(name, controls)))
        else:
            entries.append((seq, 0, _unknown(name, controls)))

    for (family, uuid), found in sets.items():
        listed, why = _template(found["templates"])
        placed = found["fields"]
        previous = ""
        sub = 0
        if listed is None:
            for index in sorted(placed):
                name, controls = placed[index]
                sub += 1
                entries.append((found["seq"], sub, UnreadableField(name, _first_label(controls) or name, any(c.required for c in controls), why)))
            continue
        for index, json_field in enumerate(listed):
            name = f"cards[{uuid}][field{index}]" if family == "cards" else f"surveysResponses[{uuid}][responses][field{index}]"
            dom = placed.get(index)
            result = _card_field(json_field, dom[1] if dom else None, name, previous)
            sub += 1
            entries.append((found["seq"], sub, result))
            # A follow-up takes its meaning from the question above it, whether or not that one could be read.
            previous = _collapse(json_field.get("text")) if isinstance(json_field, dict) and _collapse(json_field.get("text")) else previous
        for index in sorted(set(placed) - set(range(len(listed)))):
            name, controls = placed[index]
            sub += 1
            entries.append((found["seq"], sub, UnreadableField(
                name, _first_label(controls) or name, any(c.required for c in controls), "the page has a control that its description does not list",
            )))

    entries.sort(key=lambda entry: (entry[0], entry[1]))
    fields = tuple(item for _, _, item in entries if isinstance(item, SchemaField))
    unreadable = tuple(item for _, _, item in entries if isinstance(item, UnreadableField))
    unknown = tuple(item for _, _, item in entries if isinstance(item, UnknownControl))
    return LeverForm(fields, LeverPosting(_collapse("".join(scanner.title))), unreadable, unknown)
