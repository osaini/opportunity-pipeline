"""parse_lever_form (apply/lever_form.py): a Lever application page read as its own schema, with no browser.

docs/phase5-lever-handoff-spec.md 5.4 and 10.2. Every numbered rule of 5.4 is run against the sanitized pages in
tests/fixtures/apply/lever/ (real markup, fictional companies and questions), and the limits and the DOM and JSON cross-check
against small pages built here. Every company, person and posting is fictional. Nothing here uses a browser or the network.
"""

import html
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.apply import classify as apply_classify, lever_form, policy as apply_policy
from opportunity_app.apply.lever_form import (
    EEO_FIELDS, MAX_FIELD_OPTIONS, MAX_TEMPLATE_BYTES, MAX_TEMPLATE_FIELDS, PAGE_MANAGED_FIELDS, parse_lever_form,
)
from opportunity_app.apply.lever import EEO_SIGNATURE_FIELDS
from opportunity_app.apply.policy import SchemaField, control_of

from helpers_source import apply_modules

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apply" / "lever"
CARD = "3b9c1f5e-0d2a-4c71-8e6b-7a5d4c3b2a19"
SURVEY = "9e8d7c6b-5a49-4382-b1a0-f9e8d7c6b5a4"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def read(name):
    form = parse_lever_form(fixture(name))
    assert form is not None, name
    return form


def by_name(form):
    return {item.name: item for item in form.fields}


def card_names(form):
    return [item.name for item in form.fields if item.section == "custom"]


# --- Small pages built here ---------------------------------------------------------------------------------------------


def page(*body, title="Fixture Co - Fixture Role", form_attrs='id="application-form" method="POST"'):
    return f'<!DOCTYPE html><html><head><title>{title}</title></head><body><form {form_attrs}>{"".join(body)}</form></body></html>'


def template_input(data, card=CARD, family="cards", raw=None):
    value = html.escape(raw if raw is not None else json.dumps(data), quote=True)
    return f'<input type="hidden" value="{value}" name="{family}[{card}][baseTemplate]">'


def question(kind, text, *, required=False, options=None, description=""):
    out = {"type": kind, "text": text, "description": description, "required": required, "id": "q-" + text}
    if options is not None:
        out["options"] = [{"text": option, "optionId": "o-" + option} for option in options]
    return out


def name_of(n, card=CARD, family="cards"):
    return f"{family}[{card}][field{n}]" if family == "cards" else f"{family}[{card}][responses][field{n}]"


def text_control(n, *, required=False, card=CARD, family="cards", kind="text", extra=""):
    return f'<input type="{kind}" name="{name_of(n, card, family)}"{" required" if required else ""}{extra}>'


def textarea_control(n, *, required=False, card=CARD, family="cards"):
    return f'<textarea name="{name_of(n, card, family)}"{" required" if required else ""}></textarea>'


def file_control(n, *, card=CARD, family="cards", required=False):
    return f'<input type="file" name="{name_of(n, card, family)}"{" required" if required else ""}>'


def select_control(n, options, *, required=False, placeholder="Select...", card=CARD, family="cards", extra=""):
    shown = (f'<option value="">{placeholder}</option>' if placeholder is not None else "") + "".join(
        f'<option value="{html.escape(o)}">{html.escape(o)}</option>' for o in options)
    return f'<select name="{name_of(n, card, family)}"{" required" if required else ""}{extra}>{shown}</select>'


def group_control(n, kind, options, *, required=None, card=CARD, family="cards", disabled=False):
    """Radios or checkboxes. ``required`` is a bool for every box or a list of bools, one per box."""
    flags = required if isinstance(required, list) else [bool(required)] * len(options)
    return "".join(
        f'<label><input type="{kind}" name="{name_of(n, card, family)}" value="{html.escape(o)}"{" required" if flag else ""}'
        f'{" disabled" if disabled else ""}><span class="application-answer-alternative">{html.escape(o)}</span></label>'
        for o, flag in zip(options, flags))


def card_page(fields, controls, **kwargs):
    return page(template_input({"text": "Card", "type": "posting", "id": CARD, "fields": fields}), "".join(controls), **kwargs)


def one_card(kind, text="Question?", *, required=False, options=None, control=None):
    """A page with one question whose JSON says ``kind`` and whose page control is ``control`` (the matching one by default)."""
    fields = [question(kind, text, required=required, options=options)]
    controls = {
        "text": lambda: text_control(0, required=required),
        "textarea": lambda: textarea_control(0, required=required),
        "dropdown": lambda: select_control(0, options or [], required=required),
        "multiple-choice": lambda: group_control(0, "radio", options or [], required=required),
        "multiple-select": lambda: group_control(0, "checkbox", options or [], required=required),
        "file-upload": lambda: file_control(0),
    }
    return card_page(fields, [control if control is not None else controls[kind]()])


def only(form):
    assert len(form.fields) + len(form.unreadable) == 1, (form.fields, form.unreadable, form.unknown)
    return (form.fields or form.unreadable)[0]


# ------------------------------------------------------------------------------------------------------------------------


class NoFormTests(unittest.TestCase):
    """5.4 item 1: no ``form#application-form`` is None, which is not the same as an empty form."""

    def test_pages_that_are_not_an_application_form_are_none(self):
        for name in ("thanks.html", "closed.html", "cloudflare_interstitial.html"):
            with self.subTest(page=name):
                self.assertIsNone(parse_lever_form(fixture(name)))

    def test_nothing_and_text_that_is_not_html_are_none(self):
        for text in ("", "   ", None, "plain words", "<html><body></body></html>", "<form></form>"):
            with self.subTest(text=text):
                self.assertIsNone(parse_lever_form(text))

    def test_a_form_with_another_id_or_a_lookalike_is_not_the_application(self):
        self.assertIsNone(parse_lever_form(page("<input name='name'>", form_attrs='id="search"')))
        self.assertIsNone(parse_lever_form(page("<input name='name'>", form_attrs='id="application-form-2"')))
        self.assertIsNone(parse_lever_form(page("<input name='name'>", form_attrs='class="application-form"')))

    def test_an_empty_application_form_is_a_form_with_nothing_in_it(self):
        form = parse_lever_form(page())
        self.assertEqual((form.fields, form.unreadable, form.unknown), ((), (), ()))

    def test_controls_outside_the_form_are_not_read(self):
        text = '<input name="name" type="text">' + page('<input name="email" type="email">') + '<input name="phone" type="text"><textarea name="comments"></textarea>'
        self.assertEqual([item.name for item in parse_lever_form(text).fields], ["email"])

    def test_only_the_first_application_form_is_read(self):
        text = page('<input name="email" type="email">') + page('<input name="phone" type="text">')
        self.assertEqual([item.name for item in parse_lever_form(text).fields], ["email"])


class FormMembershipTests(unittest.TestCase):
    """5.4 item 2, as a browser would read it: which controls the form submits, and which of them are turned off."""

    def test_a_control_in_a_disabled_fieldset_is_turned_off(self):
        text = card_page([question("text", "Q?")], ['<fieldset disabled><div>', text_control(0), '</div></fieldset>'])
        form = parse_lever_form(text)
        self.assertEqual((form.fields, [item.reason for item in form.unreadable]), ((), ["the page has turned this question off"]))
        fixed = parse_lever_form(page('<fieldset disabled><input name="name" type="text"></fieldset><input name="email" type="email">'))
        self.assertEqual([item.name for item in fixed.fields], ["email"])
        self.assertEqual([(item.name, item.disabled) for item in fixed.unknown], [("name", True)])

    def test_a_disabled_fieldset_closes_and_what_follows_it_is_not_turned_off(self):
        text = card_page([question("text", "Q?")], ['<fieldset disabled><input name="name" type="text"></fieldset>', text_control(0)])
        form = parse_lever_form(text)
        self.assertEqual([item.name for item in form.fields], [name_of(0)])

    def test_the_first_legend_of_a_disabled_fieldset_is_not_turned_off_but_a_second_one_is(self):
        form = parse_lever_form(page(
            '<fieldset disabled><legend><input name="name" type="text"></legend><legend><input name="email" type="email"></legend>'
            '<input name="phone" type="text"></fieldset>'))
        self.assertEqual([item.name for item in form.fields], ["name"])
        self.assertEqual(sorted(item.name for item in form.unknown), ["email", "phone"])

    def test_a_nested_fieldset_stays_turned_off_with_its_parent(self):
        form = parse_lever_form(page('<fieldset disabled><fieldset><input name="name" type="text"></fieldset></fieldset><fieldset><input name="email" type="email"></fieldset>'))
        self.assertEqual(([item.name for item in form.fields], [item.name for item in form.unknown]), (["email"], ["name"]))

    def test_disabled_fieldsets_nested_past_the_limit_are_a_page_the_parser_will_not_read(self):
        deep = lever_form.MAX_NESTED_FIELDSETS
        self.assertIsNotNone(parse_lever_form(page("<fieldset disabled>" * deep + "</fieldset>" * deep)))
        self.assertIsNone(parse_lever_form(page("<fieldset disabled>" * (deep + 1) + "</fieldset>" * (deep + 1))))

    def test_a_fieldset_that_is_not_disabled_changes_nothing(self):
        self.assertEqual([item.name for item in parse_lever_form(page('<fieldset><legend>Who</legend><input name="name" type="text"></fieldset>')).fields], ["name"])

    def test_a_control_outside_the_form_that_names_it_with_the_form_attribute_is_submitted_and_so_is_listed(self):
        for text in (
            page('<input name="name" type="text">') + '<input name="favourite" form="application-form" required>',
            '<input name="favourite" form="application-form" required>' + page('<input name="name" type="text">'),
        ):
            with self.subTest(text=text[:30]):
                form = parse_lever_form(text)
                self.assertEqual([item.name for item in form.fields], ["name"])
                self.assertEqual([(item.name, item.required) for item in form.unknown], [("favourite", True)])

    def test_an_outside_control_with_a_fixed_name_is_unknown_not_read_as_that_field(self):
        form = parse_lever_form(page('<input name="email" type="email">') + '<input name="email" form="application-form">' + '<select name="phone" form="application-form"></select>')
        self.assertEqual(([item.name for item in form.fields], sorted(item.name for item in form.unknown)), ([], ["email", "phone"]))

    def test_a_control_outside_the_form_with_another_form_or_none_is_not_read(self):
        text = page('<input name="name" type="text">') + '<input name="a" form="other"><input name="b"><input name="c" form>'
        form = parse_lever_form(text)
        self.assertEqual(([item.name for item in form.fields], form.unknown), (["name"], ()))

    def test_a_control_inside_the_form_that_belongs_to_another_form_is_not_submitted_and_is_not_read(self):
        form = parse_lever_form(page('<input name="email" type="email" form="other"><input name="name" type="text" form="application-form"><input name="phone" form="">'))
        self.assertEqual(([item.name for item in form.fields], form.unknown, form.unreadable), (["name"], (), ()))

    def test_a_select_that_belongs_to_another_form_takes_its_options_with_it(self):
        form = parse_lever_form(page('<select name="x" form="other"><option value="a">a</option></select><input name="name" type="text">'))
        self.assertEqual(([item.name for item in form.fields], form.unknown), (["name"], ()))

    def test_an_application_form_inside_another_form_is_not_a_form_the_browser_builds(self):
        self.assertIsNone(parse_lever_form('<form id="outer"><form id="application-form"><input name="name"></form></form>'))
        self.assertIsNone(parse_lever_form('<form action="/search"><input name="q"><form id="application-form"><input name="name"></form>'))

    def test_a_form_that_was_closed_before_the_application_form_is_not_its_parent(self):
        form = parse_lever_form('<form id="search"><input name="q"></form><form id="application-form"><input name="name" type="text"></form>')
        self.assertEqual([item.name for item in form.fields], ["name"])


class StandardFieldTests(unittest.TestCase):
    """5.4 item 3, on the demonstration page (every fixed field the pages carry)."""

    def setUp(self):
        self.form = read("demo_eeo_survey.html")
        self.fields = by_name(self.form)

    def test_the_fixed_fields_are_read_by_exact_name_in_document_order(self):
        standard = [item.name for item in self.form.fields if item.section == "standard"]
        self.assertEqual(standard, [
            "resume", "name", "pronouns", "email", "phone", "location", "selectedLocation", "org",
            "urls[LinkedIn]", "urls[Github]", "urls[Other Website]", "urls[Video Link ]",
        ])

    def test_each_has_the_label_the_page_shows_the_type_greenhouse_would_give_it_and_the_required_flag_as_loaded(self):
        expected = {
            "resume": ("Resume/CV", "input_file", False), "name": ("Full name", "input_text", True), "email": ("Email", "input_text", True),
            "phone": ("Phone", "input_text", True), "location": ("Current location", "input_text", False),
            "org": ("Current company", "input_text", True), "urls[LinkedIn]": ("LinkedIn URL", "input_text", False),
            "urls[Other Website]": ("Other Website URL", "input_text", False),
        }
        for name, (label, kind, required) in expected.items():
            with self.subTest(name=name):
                item = self.fields[name]
                self.assertEqual((item.label, item.type, item.required, item.section), (label, kind, required, "standard"))
                self.assertEqual((item.options, item.parent, item.description), ((), "", ""))

    def test_a_link_label_with_a_trailing_space_keeps_the_name_exactly_and_the_label_is_tidied(self):
        item = self.fields["urls[Video Link ]"]
        self.assertTrue(item.name.endswith(" ]"))
        self.assertEqual(item.label, "Video Link URL")
        self.assertNotIn("urls[Video Link]", self.fields)

    def test_the_hidden_selected_location_is_a_hidden_row_the_plan_skips(self):
        item = self.fields["selectedLocation"]
        self.assertEqual((item.type, control_of(item), item.required), ("input_hidden", "hidden", False))

    def test_pronouns_are_one_checkbox_group_without_the_nameless_custom_box_or_its_text_field(self):
        item = self.fields["pronouns"]
        self.assertEqual(control_of(item), "multiselect")
        self.assertEqual(item.options[:3], ("He/him", "She/her", "They/them"))
        self.assertEqual(item.options[-1], "Use name only")
        self.assertEqual(len(item.options), 10)
        self.assertNotIn("Custom", item.options)

    def test_every_standard_field_reads_as_the_control_the_plan_expects(self):
        controls = {name: control_of(item) for name, item in self.fields.items() if item.section == "standard"}
        self.assertEqual(controls["resume"], "file")
        self.assertEqual({controls[n] for n in ("name", "email", "phone", "location", "org", "urls[LinkedIn]")}, {"text"})

    def test_the_resume_is_required_when_its_label_shows_the_star_though_the_file_input_has_no_attribute(self):
        # Seen on live pages 2026-10-08: the hidden file input never carries `required`; the "required" star in the label is the only sign.
        for name, expected in (("demo_eeo_survey.html", False), ("cards_files_consent.html", True), ("many_cards.html", True)):
            with self.subTest(page=name):
                self.assertEqual(by_name(read(name))["resume"].required, expected)
        self.assertNotRegex(fixture("cards_files_consent.html"), r'name="resume"[^>]*required')

    def test_the_comments_box_and_the_marketing_consent_on_a_large_company_page(self):
        fields = by_name(read("many_cards.html"))
        comments, consent = fields["comments"], fields["consent[marketing]"]
        self.assertEqual((comments.label, comments.type, control_of(comments), comments.required), ("Additional information", "textarea", "textarea", False))
        self.assertEqual(control_of(consent), "checkbox", "one box: the hidden 0 beside it is not an option")
        self.assertEqual(consent.options, ("Orbital Ledger has my consent to contact me about future job opportunities.",))
        self.assertEqual(consent.label, consent.options[0])
        self.assertFalse(consent.required)

    def test_a_required_location_and_an_optional_phone_are_read_as_the_page_loaded(self):
        fields = by_name(read("many_cards.html"))
        self.assertEqual((fields["location"].required, fields["phone"].required, fields["org"].required), (True, False, False))

    def test_the_office_select_and_the_address_parts_on_the_constructed_page(self):
        fields = by_name(read("variants.html"))
        office = fields["opportunityLocationId"]
        self.assertEqual((office.type, office.required, office.options, control_of(office)), ("multi_value_single_select", True, ("Fixture City", "Sample Town"), "select"))
        self.assertEqual((fields["residentialLocation[street]"].type, fields["residentialLocation[city]"].section), ("input_text", "standard"))

    def test_a_control_with_no_name_is_never_read_and_never_listed(self):
        form = read("variants.html")
        names = {item.name for item in form.fields} | {item.name for item in form.unreadable} | {item.name for item in form.unknown}
        self.assertNotIn("", names)
        self.assertEqual(form.unknown, ())
        labels = {item.label for item in form.fields}
        self.assertNotIn("A box with no name", labels)
        self.assertNotIn("Where are you based?", labels)

    def test_a_named_control_inside_the_form_is_listed_even_when_its_label_is_missing(self):
        form = parse_lever_form(page('<input type="text" name="name">'))
        self.assertEqual([(item.name, item.label) for item in form.fields], [("name", "name")])


class ScannerNameTests(unittest.TestCase):
    """The scanner subclasses html.parser.HTMLParser, whose own private names change between patch releases (3.12.15 added
    ``_pending``, which the scanner used too and overwrote). Every private name the scanner sets or defines is mangled to
    the class (``self.__name``), so no release can collide with it, and no public one shadows the base class."""

    def test_the_scanner_keeps_no_single_underscore_name_and_shadows_nothing_of_htmlparser(self):
        import ast
        import inspect
        from html.parser import HTMLParser
        from opportunity_app.apply import lever_form

        tree = ast.parse(inspect.getsource(lever_form._Scanner))
        assigned = {node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
                    and isinstance(node.ctx, ast.Store)}
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        handlers = {name for name in defined if name.startswith("handle_")} | {"__init__"}
        single = sorted(name for name in assigned | (defined - handlers) if name.startswith("_") and not name.startswith("__"))
        self.assertEqual(single, [])
        base = HTMLParser()
        base.feed("<p>text</p>")
        base.close()
        shadowed = sorted(name for name in assigned | (defined - handlers) if not name.startswith("__") and (name in vars(base) or hasattr(HTMLParser, name)))
        self.assertEqual(shadowed, [])


class FieldKindTests(unittest.TestCase):
    """5.4 item 3: a fixed name on the wrong kind of control is not the fixed field. It is an unknown control, never filled."""

    def test_a_fixed_name_on_another_kind_of_control_is_unknown(self):
        for body, name in (
            ('<input type="checkbox" name="email" required>', "email"),
            ('<select name="name" required><option value="">x</option></select>', "name"),
            ('<input type="text" name="resume">', "resume"),
            ('<input type="text" name="comments">', "comments"),
            ('<input type="text" name="selectedLocation">', "selectedLocation"),
            ('<input type="file" name="phone">', "phone"),
            ('<input type="text" name="opportunityLocationId">', "opportunityLocationId"),
            ('<input type="text" name="consent[marketing]">', "consent[marketing]"),
            ('<input type="radio" name="urls[LinkedIn]" value="x">', "urls[LinkedIn]"),
        ):
            with self.subTest(name=name):
                form = parse_lever_form(page(body))
                self.assertEqual(form.fields, ())
                self.assertEqual([item.name for item in form.unknown], [name])

    def test_a_disabled_fixed_field_is_unknown_not_filled(self):
        form = parse_lever_form(page('<input type="text" name="email" disabled>'))
        self.assertEqual(form.fields, ())
        self.assertEqual([(item.name, item.disabled) for item in form.unknown], [("email", True)])

    def test_text_like_input_types_are_all_text_fields(self):
        for kind in ("text", "email", "tel", "url", "search"):
            with self.subTest(kind=kind):
                self.assertEqual(only(parse_lever_form(page(f'<input type="{kind}" name="phone">'))).type, "input_text")

    def test_two_controls_under_a_single_text_name_are_unknown(self):
        form = parse_lever_form(page('<input type="text" name="phone"><input type="text" name="phone">'))
        self.assertEqual((form.fields, [item.name for item in form.unknown]), ((), ["phone"]))

    def test_a_label_for_a_control_comes_from_a_label_that_names_it(self):
        form = parse_lever_form(page('<label for="x"><h4>Anything else</h4></label><textarea id="x" name="comments"></textarea>'))
        self.assertEqual(form.fields[0].label, "Anything else")

    def test_text_inside_a_script_or_style_is_never_a_label(self):
        form = parse_lever_form(page(
            '<label><span><div>I agree to be contacted.</div></span><script>var x = "not a label";</script><style>.a { color: red }</style>'
            '<input type="hidden" name="consent[marketing]" value="0"><input type="checkbox" name="consent[marketing]" value="1"></label>'))
        self.assertEqual((form.fields[0].label, form.fields[0].options), ("I agree to be contacted.", ("I agree to be contacted.",)))

    def test_a_label_never_leaks_to_a_control_in_the_next_question(self):
        form = parse_lever_form(page(
            '<ul><li class="application-question"><label><div class="application-label">Phone</div><input type="text" name="phone"></label></li></ul>'
            '<div><textarea name="comments"></textarea></div>'))
        self.assertEqual([(item.name, item.label) for item in form.fields], [("phone", "Phone"), ("comments", "comments")])


class CardTests(unittest.TestCase):
    """5.4 item 4: a card is read by its ``baseTemplate``, with ``N`` indexing ``fields``."""

    def test_a_cards_questions_come_from_its_json_in_its_order_with_the_plan_types(self):
        form = read("cards_files_consent.html")
        self.assertEqual(len([item for item in form.fields if item.name.startswith("cards[") and item.name.endswith("[field0]")]), 4, "four cards")
        card = [item for item in form.fields if item.section == "custom"][:6]
        self.assertEqual([item.type for item in card], [
            "input_file", "input_text", "multi_value_single_select", "input_text", "input_text", "multi_value_single_select"])
        self.assertEqual([control_of(item) for item in card], ["file", "text", "select", "text", "text", "select"])
        self.assertEqual([item.required for item in card], [False, False, True, True, True, True])
        self.assertEqual(card[0].label, "Cover Letter:")
        self.assertEqual(card[2].options, ("Yes", "No"))
        self.assertEqual({item.name.split("]")[0] for item in card}, {card[0].name.split("]")[0]})

    def test_the_question_text_is_the_jsons_not_the_pages(self):
        fields = by_name(parse_lever_form(card_page([question("text", "  The   JSON   text ")], [text_control(0)])))
        self.assertEqual(fields[name_of(0)].label, "The JSON text")

    def test_the_description_is_kept_and_a_very_long_one_is_cut_and_marked(self):
        form = parse_lever_form(card_page([question("text", "Q?", description="Select all that apply")], [text_control(0)]))
        self.assertEqual(form.fields[0].description, "Select all that apply")
        long = parse_lever_form(card_page([question("text", "Q?", description="x" * 2500)], [text_control(0)])).fields[0]
        self.assertEqual((len(long.description), long.description_cut), (apply_policy.MAX_DESCRIPTION_CHARS, True))
        self.assertFalse(form.fields[0].description_cut)

    def test_a_follow_up_is_filed_under_the_question_above_it_within_its_card_only(self):
        form = parse_lever_form(
            card_page([question("text", "Do you know someone here?"), question("text", "If yes, who?")], [text_control(0), text_control(1)])
            + "")
        self.assertEqual([item.parent for item in form.fields], ["", "Do you know someone here?"])
        many = read("many_cards.html")
        firsts = [item for item in many.fields if item.section == "custom" and item.name.endswith("[field0]")]
        self.assertTrue(all(item.parent == "" for item in firsts), "the first question of a card follows nothing")

    def test_an_option_list_keeps_the_jsons_order_and_its_exact_labels(self):
        item = by_name(read("many_cards.html"))[[n for n in card_names(read("many_cards.html"))][0]]
        self.assertEqual(len(item.options), 33)
        self.assertEqual(item.options[0], "Language AA (L01)")
        self.assertEqual(control_of(item), "multiselect")

    def test_a_dropdown_that_starts_with_the_empty_valued_placeholder_lists_only_the_real_options(self):
        many = read("many_cards.html")
        university = next(item for item in many.fields if item.label.startswith("Which university"))
        self.assertEqual(len(university.options), 11)
        self.assertEqual(university.options[-1], "Other (School Not Listed)")
        self.assertFalse(any(option.startswith("Click Here") or option.startswith("Select") for option in university.options))
        self.assertEqual(many.unreadable, ())

    def test_a_single_required_certification_box_is_one_checkbox_question(self):
        item = next(item for item in read("cards_files_consent.html").fields if item.label.startswith("I certify"))
        self.assertEqual((control_of(item), item.required, len(item.options)), ("checkbox", True, 1))

    def test_a_33_box_required_group_is_one_required_question(self):
        many = read("many_cards.html")
        languages = next(item for item in many.fields if item.label.startswith("Language Skill"))
        self.assertEqual((languages.required, len(languages.options)), (True, 33))
        self.assertEqual(sum(1 for item in many.fields if item.name == languages.name), 1)

    def test_every_fixture_reads_with_nothing_unreadable_and_nothing_unknown(self):
        for name in ("demo_eeo_survey.html", "cards_files_consent.html", "many_cards.html", "variants.html"):
            with self.subTest(page=name):
                form = read(name)
                self.assertEqual((form.unreadable, form.unknown), ((), ()))

    def test_a_card_with_no_template_makes_its_questions_unreadable_and_a_required_one_stays_required(self):
        form = parse_lever_form(page(text_control(0, required=True), text_control(1)))
        self.assertEqual(form.fields, ())
        self.assertEqual([(item.name, item.required) for item in form.unreadable], [(name_of(0), True), (name_of(1), False)])
        self.assertIn("no description", form.unreadable[0].reason)
        self.assertEqual(form.unknown, ())

    def test_a_card_given_two_templates_is_unreadable(self):
        data = {"text": "Card", "type": "posting", "id": CARD, "fields": [question("text", "Q?")]}
        form = parse_lever_form(page(template_input(data), template_input(data), text_control(0)))
        self.assertEqual((form.fields, [item.name for item in form.unreadable]), ((), [name_of(0)]))
        self.assertIn("twice", form.unreadable[0].reason)


class LimitTests(unittest.TestCase):
    """5.4 item 4: every limit fails to unreadable, and an unreadable question that is required stays required."""

    def unreadable_for(self, raw=None, data=None, controls=None):
        controls = controls if controls is not None else [text_control(0, required=True), text_control(1)]
        form = parse_lever_form(page(template_input(data, raw=raw), "".join(controls)))
        self.assertEqual(form.fields, ())
        return form.unreadable

    def test_a_template_over_two_megabytes_is_unreadable(self):
        field = question("text", "Q?")
        field["description"] = "x" * MAX_TEMPLATE_BYTES
        found = self.unreadable_for(data={"fields": [field]})
        self.assertEqual([(item.name, item.required) for item in found], [(name_of(0), True), (name_of(1), False)])
        self.assertIn("too large", found[0].reason)

    def test_a_template_at_the_limit_is_still_read(self):
        data = {"fields": [question("text", "Q?")], "pad": ""}
        data["pad"] = "x" * (MAX_TEMPLATE_BYTES - len(json.dumps(data).encode()))
        raw = json.dumps(data)
        self.assertEqual(len(raw.encode()), MAX_TEMPLATE_BYTES)
        form = parse_lever_form(page(template_input(None, raw=raw), text_control(0)))
        self.assertEqual((len(form.fields), form.unreadable), (1, ()))
        over = json.dumps({**data, "pad": data["pad"] + "x"})
        self.assertEqual(len(parse_lever_form(page(template_input(None, raw=over), text_control(0))).unreadable), 1)

    def test_text_that_is_not_json_or_not_an_object_is_unreadable(self):
        for raw in ("not json", "", "[1, 2]", '"text"', "null", "42", '{"fields": ', "[" * 5000 + "]" * 5000):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(len(self.unreadable_for(raw=raw)), 2)

    def test_fields_that_is_not_a_list_is_unreadable(self):
        for fields in (None, {}, "x", 3):
            with self.subTest(fields=fields):
                self.assertEqual(len(self.unreadable_for(data={"fields": fields})), 2)
        self.assertEqual(len(self.unreadable_for(data={"text": "no fields key"})), 2)

    def test_a_template_of_more_than_200_fields_is_unreadable_and_exactly_200_is_read(self):
        many = [question("text", f"Q{i}?") for i in range(MAX_TEMPLATE_FIELDS + 1)]
        found = self.unreadable_for(data={"fields": many}, controls=[text_control(i) for i in range(MAX_TEMPLATE_FIELDS + 1)])
        self.assertEqual(len(found), MAX_TEMPLATE_FIELDS + 1)
        self.assertIn("too many fields", found[0].reason)
        fine = parse_lever_form(card_page(many[:MAX_TEMPLATE_FIELDS], [text_control(i) for i in range(MAX_TEMPLATE_FIELDS)]))
        self.assertEqual((len(fine.fields), fine.unreadable), (MAX_TEMPLATE_FIELDS, ()))

    def test_a_field_that_is_not_an_object_is_unreadable_and_its_neighbours_are_still_read(self):
        form = parse_lever_form(page(template_input({"fields": ["text", question("text", "Fine?")]}), text_control(0, required=True), text_control(1)))
        self.assertEqual([item.name for item in form.fields], [name_of(1)])
        self.assertEqual([(item.name, item.required) for item in form.unreadable], [(name_of(0), True)])

    def test_a_field_type_outside_the_six_is_unreadable(self):
        for kind in ("date", "number", "rating", "", None, 5, ["text"], "TEXT", "Text", "multiple_choice"):
            with self.subTest(kind=kind):
                bad = question("text", "Q?", required=True)
                bad["type"] = kind
                form = parse_lever_form(card_page([bad], [text_control(0, required=True)]))
                self.assertEqual((form.fields, [(item.name, item.required) for item in form.unreadable]), ((), [(name_of(0), True)]))

    def test_a_field_text_that_is_not_a_string_or_is_empty_is_unreadable(self):
        for text in (None, 5, ["Q?"], {"a": 1}, "", "   ", True):
            with self.subTest(text=text):
                bad = question("text", "Q?")
                bad["text"] = text
                form = parse_lever_form(card_page([bad], [text_control(0)]))
                self.assertEqual((form.fields, [item.name for item in form.unreadable]), ((), [name_of(0)]))

    def test_a_required_flag_that_is_not_a_boolean_is_unreadable(self):
        for flag in ("true", 1, "yes", [True]):
            with self.subTest(flag=flag):
                bad = question("text", "Q?")
                bad["required"] = flag
                form = parse_lever_form(card_page([bad], [text_control(0)]))
                self.assertEqual((form.fields, len(form.unreadable)), ((), 1))

    def test_a_missing_required_flag_means_not_required(self):
        field = question("text", "Q?")
        del field["required"]
        self.assertFalse(parse_lever_form(card_page([field], [text_control(0)])).fields[0].required)

    def test_a_description_that_is_not_text_is_unreadable_but_a_missing_one_is_fine(self):
        for description in (5, ["x"], {"a": 1}):
            with self.subTest(description=description):
                bad = question("text", "Q?")
                bad["description"] = description
                self.assertEqual(len(parse_lever_form(card_page([bad], [text_control(0)])).unreadable), 1)
        field = question("text", "Q?")
        del field["description"]
        self.assertEqual(len(parse_lever_form(card_page([field], [text_control(0)])).fields), 1)

    def test_a_field_with_more_than_20000_options_is_unreadable_and_exactly_20000_is_read(self):
        over = question("dropdown", "Pick one", required=True, options=[f"o{i}" for i in range(MAX_FIELD_OPTIONS + 1)])
        form = parse_lever_form(card_page([over], [select_control(0, ["only"], required=True)]))
        self.assertEqual((form.fields, [(item.name, item.required) for item in form.unreadable]), ((), [(name_of(0), True)]))
        self.assertIn("more options", form.unreadable[0].reason)
        exact = question("dropdown", "Pick one", options=[f"o{i}" for i in range(MAX_FIELD_OPTIONS)])
        fine = parse_lever_form(card_page([exact], [select_control(0, [f"o{i}" for i in range(MAX_FIELD_OPTIONS)])]))
        self.assertEqual((len(fine.fields[0].options), fine.unreadable), (MAX_FIELD_OPTIONS, ()))

    def test_an_option_that_is_not_an_object_with_text_is_unreadable(self):
        for options in (["Yes"], [{"text": 5}], [{"optionId": "x"}], "Yes", [None], [{"text": "Yes"}, 7]):
            with self.subTest(options=options):
                bad = question("dropdown", "Q?")
                bad["options"] = options
                form = parse_lever_form(card_page([bad], [select_control(0, ["Yes"])]))
                self.assertEqual((form.fields, len(form.unreadable)), ((), 1))

    def test_a_dropdown_template_with_3000_options_and_over_600_kilobytes_is_read_quickly(self):
        options = [f"University number {i:04d} " + "of the fictional examples " * 6 for i in range(3000)]
        text = card_page([question("dropdown", "Which university?", required=True, options=options)], [select_control(0, options, required=True)])
        self.assertGreater(len(text), 600_000)
        started = time.monotonic()
        form = parse_lever_form(text)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((len(form.fields), form.unreadable), (1, ()))
        self.assertEqual(len(form.fields[0].options), 3000)

    def test_a_page_of_nearly_two_megabytes_with_script_is_read_without_trouble(self):
        # The script is inside the form, where a control would be read if its text were taken for markup.
        script = "<script>" + "var a = '<input name=\"hack\">';" * 60000 + "</script>"
        text = page(text_control(0), script)
        self.assertGreater(len(text), 1_800_000)
        self.assertEqual(parse_lever_form(text).unknown, (), "a control written inside a script is not a control")

    def test_a_control_written_inside_a_script_or_a_style_inside_the_form_is_not_a_control(self):
        for tag in ("script", "style"):
            with self.subTest(tag=tag):
                form = parse_lever_form(page(f'<{tag}>var a = "<input name=hack required><label>', f'</{tag}>', '<input name="real">'))
                self.assertEqual([item.name for item in form.unknown], ["real"])


class LabelMemoParityTests(unittest.TestCase):
    """Reading a label's words once changed how long the parser takes, not what it reads: the frozen parser from before it (tests/frozen_pre_label_memo.py)
    and the real one give the same form for every fixture and for pages where many controls share one label."""

    @staticmethod
    def same(text):
        import dataclasses

        import frozen_pre_label_memo as old

        before, after = old.parse_lever_form(text), parse_lever_form(text)
        if before is None or after is None:
            return before, after
        return dataclasses.asdict(before), dataclasses.asdict(after)

    def test_every_fixture_page_reads_the_same(self):
        for name in sorted(path.name for path in FIXTURES.glob("*.html")):
            with self.subTest(page=name):
                before, after = self.same(fixture(name))
                self.assertEqual(before, after)

    def test_pages_where_controls_share_a_label_read_the_same(self):
        words = "a long sentence of words " * 18  # under MAX_LABEL_CHARS: a longer label is cut on purpose, which the tests of that limit cover
        pages = (
            page(f'<label for="shared">{words}</label>', *(f'<input id="shared" name="extra{i}">' for i in range(30))),
            page(f"<label>{words}" + "".join(f'<input type="checkbox" name="{name_of(0)}" value="v{i}">' for i in range(30)) + "</label>"),
            page("<label>unclosed " + "".join(f'<input type="radio" name="{name_of(1)}" value="v{i}"> option {i} ' for i in range(10))),
            page('<label for="a">  spaced 	 out   words </label><input id="a" name="x"><label><b>nested</b> <i>marks</i><input name="y"></label>'),
        )
        for index, text in enumerate(pages):
            with self.subTest(page=index):
                before, after = self.same(text)
                self.assertEqual(before, after)
                self.assertIsNotNone(after)


class MalformedMarkupTests(unittest.TestCase):
    """Deep or unbalanced markup costs time in proportion to its size, never in proportion to its size squared."""

    def timed(self, *body, bound=3):
        text = page(*body)
        self.assertLess(len(text), 400_000)
        started = time.monotonic()
        form = parse_lever_form(text)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, bound, "a page of this size was read in %.1f seconds" % elapsed)
        return form

    def test_end_tags_that_match_nothing_under_deep_open_tags_are_cheap(self):
        form = self.timed("<div>" * 20000 + "</span>" * 20000)
        self.assertEqual((form.fields, form.unknown), ((), ()))

    def test_labels_left_open_past_the_limit_are_a_page_the_parser_will_not_read_and_it_says_so_at_once(self):
        self.assertIsNone(self.timed("<label>" * 20000 + "<b>x</b>" * 20000, text_control(0)))
        limit = lever_form.MAX_OPEN_LABELS
        self.assertIsNotNone(parse_lever_form(page("<label>" * limit + "</label>" * limit)))
        self.assertIsNone(parse_lever_form(page("<label>" * (limit + 1))))

    def test_thousands_of_labels_that_close_are_read_in_one_pass(self):
        form = self.timed("<label>x</label>" * 20000, "<input name=\"favourite\">")
        self.assertEqual(len(form.unknown), 1)

    def test_one_very_long_label_shared_by_thousands_of_controls_is_read_in_one_pass(self):
        # One label[for] names an id that many controls repeat, and the label's text is long: the words are worked out once, not once per control.
        words = "a long sentence of words " * 6000
        expected = " ".join(words.split())
        form = self.timed(f'<label for="shared">{words}</label>', *(f'<input id="shared" name="extra{i}">' for i in range(6000)), bound=0.5)
        self.assertEqual(len(form.unreadable), 1)  # 6,000 controls is over the form's budget of text, however short each label is cut
        # A few of them are inside the budget, and each then carries the label cut short.
        few = self.timed(f'<label for="shared">{words}</label>', *(f'<input id="shared" name="extra{i}">' for i in range(50)), bound=0.5)
        self.assertEqual(len(few.unknown), 50)
        self.assertTrue(all(expected.startswith(item.label.rstrip("…")) and len(item.label) <= lever_form.MAX_SHOWN_LABEL_CHARS + 1 for item in few.unknown))

    def test_a_label_longer_than_the_app_reads_is_cut_and_the_control_is_left_to_the_student(self):
        words = "a long sentence of words " * 400
        limit = lever_form.MAX_LABEL_CHARS
        self.assertGreater(len(words), limit)
        plain = f'<input id="shared" name="extra">'
        form = parse_lever_form(page(f'<label for="shared">{words}</label>', plain, text_control(0)))
        self.assertEqual(len(form.unknown), 1)
        self.assertLessEqual(len(form.unknown[0].label), lever_form.MAX_SHOWN_LABEL_CHARS + 1)
        short = parse_lever_form(page('<label for="shared">Favourite colour</label>', plain))
        self.assertEqual(short.unknown[0].label, "Favourite colour")
        # A fixed field whose label is too long is no longer a field the plan fills: it is an unreadable one, with the reason.
        grouped = parse_lever_form(page(f'<label for="name">{words}</label><input id="name" name="name">'))
        self.assertEqual(grouped.fields, ())
        self.assertEqual(len(grouped.unreadable), 1)
        self.assertEqual(grouped.unreadable[0].name, "name")
        self.assertIn("too long", grouped.unreadable[0].reason)
        self.assertLessEqual(len(grouped.unreadable[0].label), lever_form.MAX_SHOWN_LABEL_CHARS + 1)

    def test_the_whole_check_of_a_page_with_one_long_label_shared_by_many_controls_is_cheap(self):
        # The cost of the plan is the controls times the label each carries, so the parse alone is not the measure.
        from opportunity_app.apply import ats as apply_ats
        from opportunity_app.apply.policy import build_plan
        from helpers_apply import sources
        words = "a long sentence of words " * 1500
        radios = "".join(f'<input type="radio" name="eeo[race]" value="v{i}">' for i in range(300))
        pages = {
            "controls": page(f'<label for="shared">{words}</label>', *(f'<input id="shared" name="extra{i}">' for i in range(400))),
            "options": page(f"<label>{words}{radios}</label>"),
        }
        for kind, text in pages.items():
            with self.subTest(page=kind):
                started = time.monotonic()
                form = parse_lever_form(text)
                schema = apply_ats.lever_parse_schema({"lever_form": form})
                plan = build_plan(schema, None, sources(), "Fixture Co", "handoff", ats_name="Lever", ats="lever")
                elapsed = time.monotonic() - started
                self.assertGreater(len(plan.fields), 0)
                self.assertLess(elapsed, 3.0, "the check of a page of %d bytes took %.1f seconds (0.3 is usual, 25 or more is the quadratic cost)" % (len(text), elapsed))
                self.assertLess(len(json.dumps([item.question for item in plan.fields])), 150_000)

    def test_the_whole_check_of_a_page_whose_controls_share_a_label_at_the_limit_is_cheap(self):
        # The label cap bounds one label, not how many controls carry it: the form as a whole has a budget of text too.
        from opportunity_app.apply import ats as apply_ats
        from opportunity_app.apply.policy import build_plan
        from helpers_apply import sources
        label = ("word " * 200)[:lever_form.MAX_LABEL_CHARS]
        self.assertEqual(len(label), lever_form.MAX_LABEL_CHARS)
        radios = "".join(f'<input type="radio" name="eeo[race]" value="v{i}">' for i in range(4000))
        pages = {
            "controls": page(f'<label for="shared">{label}</label>', *(f'<input id="shared" name="extra{i}" required>' for i in range(4000))),
            "options": page(f"<label>{label}{radios}</label>"),
        }
        for kind, text in pages.items():
            with self.subTest(page=kind):
                started = time.monotonic()
                form = parse_lever_form(text)
                schema = apply_ats.lever_parse_schema({"lever_form": form})
                plan = build_plan(schema, None, sources(), "Fixture Co", "handoff", ats_name="Lever", ats="lever")
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 3.0, "the check of a page of %d bytes took %.1f seconds" % (len(text), elapsed))
                self.assertLess(len(json.dumps([item.question for item in plan.fields])), 150_000)
                # The page is too big to read: one question the student answers, and the reason in words.
                self.assertEqual((form.fields, form.unknown), ((), ()))
                self.assertEqual(len(form.unreadable), 1)
                self.assertTrue(form.unreadable[0].required)
                self.assertIn("too much", form.unreadable[0].reason)

    def test_a_form_with_a_great_many_short_controls_is_over_the_budget_too(self):
        form = parse_lever_form(page(*(f'<input name="extra{i}">' for i in range(5000))))
        self.assertEqual(len(form.unreadable), 1)
        self.assertEqual((form.fields, form.unknown), ((), ()))

    def test_a_form_over_the_budget_names_itself_so_the_sentence_to_the_student_reads_whole(self):
        from opportunity_app.apply import ats as apply_ats
        from opportunity_app.apply.policy import build_plan
        from helpers_apply import sources
        form = parse_lever_form(page(*(f'<input name="extra{i}">' for i in range(5000))))
        self.assertTrue(form.unreadable[0].label.strip(), "a blank label leaves the student a question with no name")
        schema = apply_ats.lever_parse_schema({"lever_form": form})
        plan = build_plan(schema, None, sources(), "Fixture Co", "handoff", ats_name="Lever", ats="lever")
        problem = plan.fields[0].problem
        self.assertNotIn(": (", problem)
        self.assertNotIn("()", problem)
        self.assertNotIn("so every question is left for you", problem, "the sentence already says the student answers it")
        self.assertIn("too much text", problem)

    def test_a_form_of_ordinary_size_is_inside_the_budget(self):
        label = "w" * lever_form.MAX_LABEL_CHARS
        form = parse_lever_form(page(f'<label for="shared">{label}</label>', *(f'<input id="shared" name="extra{i}">' for i in range(100))))
        self.assertEqual((len(form.unknown), form.unreadable), (100, ()))
        for path in sorted(FIXTURES.glob("*.html")):
            form = parse_lever_form(fixture(path.name))
            if form is not None:
                self.assertFalse([item for item in form.unreadable if "too much" in item.reason], path.name)

    def test_deeply_nested_label_and_answer_markup_is_cheap(self):
        form = self.timed('<div class="application-label">' + "<i>" * 20000 + "</u>" * 20000, '<span class="application-answer-alternative">' + "<i>" * 20000 + "</u>" * 20000)
        self.assertEqual(form.fields, ())

    def test_a_radio_is_named_by_the_label_around_it_when_there_is_no_answer_span(self):
        name = name_of(0)
        control = (f'<label><input type="radio" name="{name}" value="Yes"> Yes </label>'
                   f'<label for="no"><b>No</b></label><label><input type="radio" id="no" name="{name}" value="No"></label>')
        form = parse_lever_form(one_card("multiple-choice", options=["Yes", "No"], control=control))
        self.assertEqual((len(form.fields), form.unreadable), (1, ()))


class CrossCheckTests(unittest.TestCase):
    """5.4 item 5: the JSON says what the question is, the page says what can be filled. Any mismatch marks the field unreadable."""

    def test_a_matching_control_of_every_type_is_readable(self):
        for kind, options in (("text", None), ("textarea", None), ("dropdown", ["A", "B"]), ("multiple-choice", ["A", "B"]),
                              ("multiple-select", ["A", "B"]), ("file-upload", None)):
            with self.subTest(kind=kind):
                form = parse_lever_form(one_card(kind, options=options))
                self.assertEqual((len(form.fields), form.unreadable), (1, ()))

    def test_each_json_type_against_each_wrong_control_is_unreadable(self):
        controls = {
            "text": text_control(0), "textarea": textarea_control(0), "dropdown": select_control(0, ["A", "B"]),
            "multiple-choice": group_control(0, "radio", ["A", "B"]), "multiple-select": group_control(0, "checkbox", ["A", "B"]),
            "file-upload": file_control(0),
        }
        for kind in controls:
            for other, control in controls.items():
                if kind == other:
                    continue
                with self.subTest(json=kind, page=other):
                    form = parse_lever_form(one_card(kind, options=["A", "B"], control=control))
                    self.assertEqual((form.fields, len(form.unreadable)), ((), 1))
                    self.assertIn("not the kind", form.unreadable[0].reason)

    def test_other_input_types_and_a_multiple_select_element_are_not_a_text_or_a_dropdown(self):
        for control in (text_control(0, kind="email"), text_control(0, kind="number"), text_control(0, kind="date"), text_control(0, kind="hidden")):
            with self.subTest(control=control):
                self.assertEqual(len(parse_lever_form(one_card("text", control=control)).unreadable), 1)
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=["A"], control=select_control(0, ["A"], extra=" multiple"))).unreadable), 1)

    def test_two_controls_where_the_json_says_one_are_unreadable(self):
        for kind, control in (("text", text_control(0) + text_control(0)), ("dropdown", select_control(0, ["A"]) + select_control(0, ["A"])),
                              ("textarea", textarea_control(0) + textarea_control(0))):
            with self.subTest(kind=kind):
                self.assertEqual(len(parse_lever_form(one_card(kind, options=["A"], control=control)).unreadable), 1)

    def test_a_mixed_group_of_radios_and_checkboxes_is_unreadable(self):
        control = group_control(0, "radio", ["A"]) + group_control(0, "checkbox", ["B"])
        self.assertEqual(len(parse_lever_form(one_card("multiple-choice", options=["A", "B"], control=control)).unreadable), 1)

    def test_required_in_the_json_but_not_on_the_page_and_the_other_way_round_are_unreadable(self):
        for kind, options in (("text", None), ("textarea", None), ("dropdown", ["A", "B"]), ("multiple-choice", ["A", "B"]), ("multiple-select", ["A", "B"])):
            for json_required in (True, False):
                with self.subTest(kind=kind, json_required=json_required):
                    controls = {
                        "text": text_control(0, required=not json_required), "textarea": textarea_control(0, required=not json_required),
                        "dropdown": select_control(0, options or [], required=not json_required),
                        "multiple-choice": group_control(0, "radio", options or [], required=not json_required),
                        "multiple-select": group_control(0, "checkbox", options or [], required=not json_required),
                    }
                    form = parse_lever_form(one_card(kind, required=json_required, options=options, control=controls[kind]))
                    self.assertEqual((form.fields, len(form.unreadable)), ((), 1))
                    self.assertTrue(form.unreadable[0].required if json_required else True)
                    self.assertIn("required", form.unreadable[0].reason)

    def test_an_unreadable_question_is_required_if_either_side_says_so(self):
        json_only = parse_lever_form(one_card("text", required=True, control=text_control(0, required=False))).unreadable[0]
        page_only = parse_lever_form(one_card("text", required=False, control=text_control(0, required=True))).unreadable[0]
        self.assertEqual((json_only.required, page_only.required), (True, True))

    def test_an_unreadable_group_where_only_some_controls_ask_for_required_is_still_required(self):
        # In HTML a radio group with any required radio is required, and each required box must be ticked. The no-template path agrees.
        for kind, builder in (("multiple-choice", "radio"), ("multiple-select", "checkbox")):
            with self.subTest(kind=kind):
                control = group_control(0, builder, ["A", "B"], required=[True, False])
                form = parse_lever_form(one_card(kind, required=False, options=["A", "B"], control=control))
                self.assertEqual((form.fields, [item.required for item in form.unreadable]), ((), [True]))
                bare = parse_lever_form(page(control))
                self.assertEqual([item.required for item in bare.unreadable], [True])

    def test_an_unreadable_question_stays_required_when_the_jsons_flag_is_not_a_boolean_or_the_field_is_not_an_object(self):
        control = text_control(0, required=False)
        for flag, expected in (("true", True), (1, True), ("yes", True), (0, False), ("", False), (None, False)):
            with self.subTest(flag=flag):
                field = question("text", "Q?")
                field["required"] = flag
                self.assertEqual([item.required for item in parse_lever_form(card_page([field], [control])).unreadable], [expected])
        broken = parse_lever_form(card_page(["not an object"], [text_control(0, required=True)]))
        self.assertEqual([item.required for item in broken.unreadable], [True])
        self.assertEqual([item.required for item in parse_lever_form(card_page(["not an object"], [control])).unreadable], [False])

    def test_a_group_where_only_some_boxes_ask_for_required_is_unreadable_even_if_the_json_says_required(self):
        control = group_control(0, "checkbox", ["A", "B", "C"], required=[True, False, True])
        form = parse_lever_form(one_card("multiple-select", required=True, options=["A", "B", "C"], control=control))
        self.assertEqual((form.fields, len(form.unreadable)), ((), 1))

    def test_required_is_read_as_the_page_was_loaded_never_after_a_tick(self):
        # The page's script removes `required` from every checkbox once any one is ticked (3.11). The parser sees HTML as served,
        # so a required group is required, and a group the script has already relaxed would disagree with its JSON and be unreadable.
        loaded = parse_lever_form(one_card("multiple-select", required=True, options=["A", "B"]))
        self.assertTrue(loaded.fields[0].required)
        relaxed = parse_lever_form(one_card("multiple-select", required=True, options=["A", "B"], control=group_control(0, "checkbox", ["A", "B"], required=False)))
        self.assertEqual((relaxed.fields, len(relaxed.unreadable)), ((), 1))

    def test_an_option_the_page_has_and_the_json_lacks_and_the_reverse_are_unreadable(self):
        for kind in ("dropdown", "multiple-choice", "multiple-select"):
            builders = {"dropdown": lambda o: select_control(0, o), "multiple-choice": lambda o: group_control(0, "radio", o),
                        "multiple-select": lambda o: group_control(0, "checkbox", o)}
            for json_options, page_options in ((["A", "B"], ["A", "B", "C"]), (["A", "B", "C"], ["A", "B"]), (["A", "B"], ["A", "C"]),
                                               (["A", "B"], ["A", "b"]), (["A", "B"], ["A", "B ?"]), (["A", "B"], [])):
                with self.subTest(kind=kind, json=json_options, page=page_options):
                    form = parse_lever_form(one_card(kind, options=json_options, control=builders[kind](page_options)))
                    self.assertEqual((form.fields, len(form.unreadable)), ((), 1))
                    self.assertIn("options" if page_options or kind == "dropdown" else "no control", form.unreadable[0].reason)

    def test_the_same_options_in_another_order_are_readable_and_keep_the_jsons_order(self):
        form = parse_lever_form(one_card("dropdown", options=["A", "B", "C"], control=select_control(0, ["C", "A", "B"])))
        self.assertEqual(form.fields[0].options, ("A", "B", "C"))

    def test_a_repeated_option_must_repeat_on_both_sides(self):
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=["A", "A", "B"], control=select_control(0, ["A", "B"]))).unreadable), 1)
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=["A", "B"], control=select_control(0, ["A", "A", "B"]))).unreadable), 1)
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=["A", "A", "B"], control=select_control(0, ["A", "A", "B"]))).fields), 1)

    def test_option_whitespace_and_character_references_are_compared_as_the_reader_sees_them(self):
        fields = [question("dropdown", "Q?", options=["Arts & Sciences", "Two  spaces"])]
        control = '<select name="' + name_of(0) + '"><option value="">Select...</option><option value="Arts &amp; Sciences">Arts &amp; Sciences</option><option value="Two spaces">Two spaces</option></select>'
        form = parse_lever_form(card_page(fields, [control]))
        self.assertEqual((len(form.fields), form.fields[0].options), (1, ("Arts & Sciences", "Two spaces")))

    def test_an_option_with_an_empty_value_is_ignored_whatever_its_text_and_one_with_a_value_is_not(self):
        fields = [question("dropdown", "Q?", options=["A", "B"])]
        ignored = '<select name="' + name_of(0) + '"><option value="">Anything at all here</option><option value="A">A</option><option value="B">B</option></select>'
        self.assertEqual(len(parse_lever_form(card_page(fields, [ignored])).fields), 1)
        counted = '<select name="' + name_of(0) + '"><option value="none">Select...</option><option value="A">A</option><option value="B">B</option></select>'
        self.assertEqual(len(parse_lever_form(card_page(fields, [counted])).unreadable), 1)

    def test_an_option_with_no_value_attribute_counts_as_its_text(self):
        fields = [question("dropdown", "Q?", options=["A", "B"])]
        control = '<select name="' + name_of(0) + '"><option>A</option><option>B</option></select>'
        self.assertEqual(len(parse_lever_form(card_page(fields, [control])).fields), 1)

    def test_an_unclosed_option_still_reads(self):
        fields = [question("dropdown", "Q?", options=["A", "B"])]
        control = '<select name="' + name_of(0) + '"><option value="">Select<option value="A">A<option value="B">B</select>'
        self.assertEqual(len(parse_lever_form(card_page(fields, [control])).fields), 1)

    def test_a_card_choice_whose_submitted_value_is_not_the_label_it_shows_is_unreadable(self):
        # The page shows one answer and would submit another: the parser does not understand it, so the student answers (5.4 item 2).
        name = name_of(0)
        yes_no = ["Yes", "No"]
        swapped_select = f'<select name="{name}"><option value="">Select...</option><option value="No">Yes</option><option value="Yes">No</option></select>'
        swapped_radio = (f'<label><input type="radio" name="{name}" value="No"><span class="application-answer-alternative">Yes</span></label>'
                         f'<label><input type="radio" name="{name}" value="Yes"><span class="application-answer-alternative">No</span></label>')
        swapped_box = swapped_radio.replace("radio", "checkbox")
        for kind, control in (("dropdown", swapped_select), ("multiple-choice", swapped_radio), ("multiple-select", swapped_box)):
            with self.subTest(kind=kind):
                form = parse_lever_form(one_card(kind, options=yes_no, control=control))
                self.assertEqual((form.fields, len(form.unreadable)), ((), 1))
                self.assertIn("answers", form.unreadable[0].reason)

    def test_an_option_label_attribute_is_what_the_page_shows_and_must_match_the_json_and_the_value(self):
        name = name_of(0)
        yes_no = ["Yes", "No"]

        def select(*options):
            return f'<select name="{name}"><option value="">Select...</option>{"".join(options)}</select>'

        shown_differs = select('<option value="No" label="Yes">No</option>', '<option value="Yes" label="No">Yes</option>')
        same = select('<option value="Yes" label="Yes">Elsewhere</option>', '<option value="No" label="No">Elsewhere</option>')
        value_only = select('<option label="Yes">Yes</option>', '<option label="No">No</option>')
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=yes_no, control=shown_differs)).unreadable), 1)
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=yes_no, control=same)).fields), 1)
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=yes_no, control=value_only)).fields), 1)
        # No value attribute: the value is the option's text, so a label attribute that says something else is a different answer.
        text_is_the_value = select('<option label="Yes">No</option>', '<option label="No">Yes</option>')
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=yes_no, control=text_is_the_value)).unreadable), 1)

    def test_a_choice_value_that_differs_from_its_label_only_in_spacing_still_reads_and_a_radio_with_no_value_does_not(self):
        name = name_of(0)
        spaced = f'<select name="{name}"><option value=" Yes ">Yes</option><option value="No  ">No</option></select>'
        self.assertEqual(len(parse_lever_form(one_card("dropdown", options=["Yes", "No"], control=spaced)).fields), 1)
        no_value = (f'<label><input type="radio" name="{name}"><span class="application-answer-alternative">Yes</span></label>'
                    f'<label><input type="radio" name="{name}"><span class="application-answer-alternative">No</span></label>')
        self.assertEqual(len(parse_lever_form(one_card("multiple-choice", options=["Yes", "No"], control=no_value)).unreadable), 1)

    def test_the_eeo_and_office_selects_keep_a_label_that_is_not_their_value(self):
        for name in ("demo_eeo_survey.html", "variants.html"):
            with self.subTest(page=name):
                self.assertEqual(read(name).unreadable, ())
        self.assertTrue(any(item.name == "veteran_status" for item in read("demo_eeo_survey.html").fields))

    def test_a_radio_option_is_named_by_its_own_span_not_the_text_around_it(self):
        fields = [question("multiple-choice", "Q?", options=["Yes", "No"])]
        control = ('<label><input type="radio" name="' + name_of(0) + '" value="Yes"><span class="eeo-option-text application-answer-alternative">Yes</span>'
                   '<div class="eeo-option-description">A long note.</div></label>'
                   '<label><input type="radio" name="' + name_of(0) + '" value="No"><span class="application-answer-alternative">No</span></label>')
        self.assertEqual(len(parse_lever_form(card_page(fields, [control])).fields), 1)

    def test_a_json_field_the_page_has_no_control_for_is_unreadable_and_a_page_control_the_json_does_not_list_is_too(self):
        fields = [question("text", "First?", required=True), question("text", "Second?")]
        missing = parse_lever_form(card_page(fields, [text_control(0, required=True)]))
        self.assertEqual([item.name for item in missing.fields], [name_of(0)])
        self.assertEqual([(item.name, item.label) for item in missing.unreadable], [(name_of(1), "Second?")])
        extra = parse_lever_form(card_page(fields[:1], [text_control(0, required=True), text_control(1, required=True)]))
        self.assertEqual([item.name for item in extra.fields], [name_of(0)])
        self.assertEqual([(item.name, item.required) for item in extra.unreadable], [(name_of(1), True)])
        self.assertIn("does not list", extra.unreadable[0].reason)

    def test_a_json_field_with_no_control_that_is_required_stays_required(self):
        form = parse_lever_form(card_page([question("text", "Needed?", required=True)], []))
        self.assertEqual([(item.name, item.required) for item in form.unreadable], [(name_of(0), True)])

    def test_a_disabled_control_is_unreadable(self):
        form = parse_lever_form(card_page([question("text", "Q?")], [text_control(0, extra=" disabled")]))
        self.assertEqual((form.fields, len(form.unreadable)), ((), 1))
        self.assertIn("turned this question off", form.unreadable[0].reason)
        radios = parse_lever_form(one_card("multiple-choice", options=["A"], control=group_control(0, "radio", ["A"], disabled=True)))
        self.assertEqual(len(radios.unreadable), 1)

    def test_one_bad_question_does_not_take_its_neighbours_with_it(self):
        fields = [question("text", "Good one?"), question("dropdown", "Bad one?", options=["A"]), question("textarea", "Good two?")]
        controls = [text_control(0), select_control(1, ["Z"]), textarea_control(2)]
        form = parse_lever_form(card_page(fields, controls))
        self.assertEqual([item.label for item in form.fields], ["Good one?", "Good two?"])
        self.assertEqual([item.label for item in form.unreadable], ["Bad one?"])

    def test_a_follow_ups_parent_is_the_question_above_even_when_that_one_is_unreadable(self):
        fields = [question("dropdown", "Do you need sponsorship?", options=["Yes", "No"]), question("text", "If yes, explain")]
        form = parse_lever_form(card_page(fields, [select_control(0, ["Nope"]), text_control(1)]))
        self.assertEqual((len(form.unreadable), form.fields[0].parent), (1, "Do you need sponsorship?"))

    def test_the_survey_template_type_and_options_with_no_option_id_are_normal(self):
        text = page(
            template_input({"type": "survey", "text": "Survey", "fields": [
                {"type": "multiple-choice", "text": "Which?", "required": False, "options": [{"text": "A"}, {"text": "B", "optionId": "x"}]}]},
                card=SURVEY, family="surveysResponses"),
            group_control(0, "radio", ["A", "B"], card=SURVEY, family="surveysResponses"))
        form = parse_lever_form(text)
        self.assertEqual((len(form.fields), form.unreadable, form.fields[0].section), (1, (), "custom"))
        self.assertTrue(form.fields[0].name.startswith(f"surveysResponses[{SURVEY}][responses][field0]"))

    def test_a_card_fields_index_must_be_a_plain_number(self):
        for suffix in ("field01", "field-1", "field1.5", "fieldx", "field", "field1 ", "field99999999"):
            with self.subTest(suffix=suffix):
                form = parse_lever_form(card_page([question("text", "Q?")], [f'<input type="text" name="cards[{CARD}][{suffix}]">']))
                self.assertEqual([item.name for item in form.unknown], [f"cards[{CARD}][{suffix}]"])


class UnknownControlTests(unittest.TestCase):
    """5.4 item 6: a named control outside the families is recorded, not guessed."""

    def test_a_required_unknown_control_is_listed_with_its_name_type_label_and_required_flag(self):
        form = parse_lever_form(page(
            '<li class="application-question"><label><div class="application-label">Favourite colour <span class="required">&#10033;</span></div>'
            '<input type="text" name="favourite" required></label></li>'))
        self.assertEqual(form.fields, ())
        self.assertEqual(form.unknown, (lever_form.UnknownControl("favourite", "input", "text", "Favourite colour", True),))

    def test_an_unknown_control_whose_label_shows_the_star_is_required_though_it_has_no_attribute(self):
        # Seen on live pages: a hidden file input never carries `required`; the star in its label is the only sign (spec 3, 2026-10-08 note).
        form = parse_lever_form(page(
            '<li class="application-question resume"><label><div class="application-label">Portfolio/Work sample <span class="required">&#10033;</span></div>'
            '<input name="portfolio" type="file" tabindex="-1"></label></li>'
            '<li class="application-question"><label><div class="application-label">Resume/CV <span class="required">&#10033;</span></div>'
            '<input name="resume" type="file" tabindex="-1"></label></li>'
            '<li class="application-question"><label><div class="application-label">Notes</div><input name="notes" type="text"></label></li>'))
        self.assertEqual([(item.name, item.required) for item in form.fields], [("resume", True)])
        self.assertEqual([(item.name, item.required) for item in form.unknown], [("portfolio", True), ("notes", False)])

    def test_an_optional_unknown_control_is_listed_not_required(self):
        found = parse_lever_form(page('<textarea name="extra"></textarea>')).unknown
        self.assertEqual([(item.name, item.tag, item.type, item.required) for item in found], [("extra", "textarea", "textarea", False)])

    def test_every_kind_of_named_control_is_recorded(self):
        found = parse_lever_form(page(
            '<input type="number" name="n"><input type="date" name="d"><input type="hidden" name="h"><select name="s"><option>x</option></select>'
            '<input type="radio" name="r" value="1"><input type="checkbox" name="c" value="1">')).unknown
        self.assertEqual([(item.name, item.type) for item in found],
                         [("n", "number"), ("d", "date"), ("h", "hidden"), ("s", "select"), ("r", "radio"), ("c", "checkbox")])

    def test_a_name_used_by_different_kinds_is_one_unknown_of_type_mixed(self):
        found = parse_lever_form(page('<input type="text" name="m"><input type="checkbox" name="m" value="1">')).unknown
        self.assertEqual([(item.name, item.type) for item in found], [("m", "mixed")])

    def test_buttons_and_submits_are_not_controls(self):
        form = parse_lever_form(page('<input type="submit" name="go"><input type="button" name="b"><button type="submit" name="x"></button><input type="image" name="i"><input type="reset" name="r">'))
        self.assertEqual((form.fields, form.unknown, form.unreadable), ((), (), ()))

    def test_an_eeo_name_the_app_does_not_know_and_an_odd_card_name_are_unknown(self):
        form = parse_lever_form(page(
            '<input type="text" name="eeo[sexuality]"><input type="text" name="cards[abc][other]"><input type="text" name="surveysResponses[abc][other]">'
            f'<input type="text" name="cards[{CARD}][field0][nested]">'))
        self.assertEqual([item.name for item in form.unknown], ["eeo[sexuality]", "cards[abc][other]", "surveysResponses[abc][other]", f"cards[{CARD}][field0][nested]"])

    def test_the_unknown_controls_keep_document_order(self):
        found = parse_lever_form(page('<input type="text" name="b"><input type="text" name="a"><input type="text" name="c">')).unknown
        self.assertEqual([item.name for item in found], ["b", "a", "c"])


class PageManagedFieldTests(unittest.TestCase):
    """5.4 item 7: the page's own hidden fields are never schema, never unknown, and never written."""

    def test_the_constant_is_the_list_in_the_spec(self):
        self.assertEqual(PAGE_MANAGED_FIELDS, frozenset({
            "accountId", "linkedInData", "origin", "referer", "timezone", "socialReferralKey", "socialSource", "resumeStorageId",
            "h-captcha-response", "source"}))

    def test_none_of_them_becomes_a_schema_field_or_an_unknown_control_on_any_fixture(self):
        for name in ("demo_eeo_survey.html", "cards_files_consent.html", "many_cards.html", "variants.html"):
            with self.subTest(page=name):
                self.assertIn('name="accountId"', fixture(name), "the fixture really carries the page's hidden fields")
                form = read(name)
                seen = {item.name for item in form.fields} | {item.name for item in form.unknown} | {item.name for item in form.unreadable}
                self.assertFalse(seen & PAGE_MANAGED_FIELDS)
                self.assertFalse([n for n in seen if n.endswith(("[baseTemplate]", "[surveyId]", "[candidateSelectedLocation]"))])

    def test_the_templates_survey_ids_and_selected_locations_are_page_managed_whether_named_or_not(self):
        form = parse_lever_form(page(
            template_input({"fields": []}), template_input({"fields": []}, card=SURVEY, family="surveysResponses"),
            f'<input type="hidden" name="surveysResponses[{SURVEY}][surveyId]" value="x">',
            f'<input type="hidden" name="surveysResponses[{SURVEY}][candidateSelectedLocation]" value="">',
            '<input type="hidden" name="h-captcha-response"><input type="hidden" name="source" value="x"><input type="hidden" name="resumeStorageId">'))
        self.assertEqual((form.fields, form.unknown, form.unreadable), ((), (), ()))

    def test_the_captcha_answer_is_page_managed_even_in_the_textarea_hcaptcha_makes(self):
        form = parse_lever_form(page('<textarea name="h-captcha-response"></textarea><input type="hidden" name="timezone">'))
        self.assertEqual((form.fields, form.unknown, form.unreadable), ((), (), ()))

    def test_a_visible_control_with_a_page_managed_name_is_a_question_and_is_listed_as_unknown(self):
        # Item 7 is about hidden fields. A visible, required question must not vanish because it shares a name with one.
        form = parse_lever_form(page(
            '<li class="application-question"><label><div class="application-label">How did you hear about us?<span class="required">&#10033;</span></div>'
            '<input type="text" name="source" required></label></li>',
            '<select name="timezone" required><option value="">Select</option><option value="UTC">UTC</option></select>',
            '<input type="hidden" name="origin" value="x">'))
        self.assertEqual(form.fields, ())
        self.assertEqual([(item.name, item.type, item.required) for item in form.unknown], [("source", "text", True), ("timezone", "select", True)])

    def test_a_page_managed_name_shared_by_a_hidden_and_a_visible_control_is_listed(self):
        form = parse_lever_form(page('<input type="hidden" name="source" value="x"><input type="text" name="source">'))
        self.assertEqual([(item.name, item.type) for item in form.unknown], [("source", "mixed")])

    def test_selected_location_is_not_page_managed_it_is_the_location_pair(self):
        self.assertNotIn("selectedLocation", PAGE_MANAGED_FIELDS)
        self.assertIn("selectedLocation", by_name(read("demo_eeo_survey.html")))


class SectionTests(unittest.TestCase):
    """5.4 item 9: the section decides how the classifier reads a field."""

    def setUp(self):
        self.form = read("demo_eeo_survey.html")
        self.fields = by_name(self.form)

    def test_the_four_eeo_selects_are_demographic_and_carry_the_names_phase_5_knows(self):
        self.assertEqual(EEO_FIELDS, {"eeo[gender]": "gender", "eeo[race]": "race", "eeo[veteran]": "veteran_status", "eeo[disability]": "disability_status"})
        for name in ("gender", "race", "veteran_status", "disability_status"):
            with self.subTest(name=name):
                self.assertEqual(self.fields[name].section, "demographic")
                self.assertIn(name, apply_classify._EEOC_NAMES)
        self.assertFalse([n for n in self.fields if n.startswith("eeo[") and n not in EEO_SIGNATURE_FIELDS])

    def test_an_eeo_select_and_an_eeo_radio_group_read_alike(self):
        radios = self.fields["race"]
        select = by_name(read("cards_files_consent.html"))["race"]
        self.assertEqual((radios.type, control_of(radios)), (select.type, control_of(select)))
        self.assertIn("Decline to self-identify", radios.options)
        self.assertEqual(radios.label, "Race")

    def test_an_eeo_option_is_named_by_its_label_not_its_value(self):
        veteran = self.fields["veteran_status"]
        self.assertIn("I decline to self-identify for protected veteran status", veteran.options)
        self.assertNotIn("Decline to self-identify", veteran.options)
        disability = self.fields["disability_status"]
        self.assertIn("I do not want to answer", disability.options)
        self.assertNotIn("I do not want to answer ", disability.options, "the value has a trailing space, the label does not")

    def test_the_disability_signature_and_its_date_are_demographic_text_fields_filed_under_the_disability_question(self):
        for name in EEO_SIGNATURE_FIELDS:
            with self.subTest(name=name):
                item = self.fields[name]
                self.assertEqual((item.section, item.type, item.required, item.parent), ("demographic", "input_text", False, "Disability status"))

    def test_without_the_disability_block_there_are_no_signature_fields(self):
        names = set(by_name(read("cards_files_consent.html")))
        self.assertFalse(names & set(EEO_SIGNATURE_FIELDS))
        self.assertNotIn("disability_status", names)

    def test_no_eeo_field_can_be_planned_without_a_stored_decline(self):
        for item in self.form.fields:
            if item.section == "demographic":
                with self.subTest(name=item.name):
                    self.assertIsNotNone(apply_classify.classify_item(item, control_of(item)), "the classifier treats it as sensitive")
        self.assertEqual(apply_classify.classify_sensitive("Name", (), "demographic", "eeo[disabilitySignature]"), "uncategorized")
        self.assertEqual(apply_classify.classify_sensitive("Gender", (), "demographic", "gender"), "eeo_gender")

    def test_cards_and_surveys_are_custom_and_the_fixed_fields_are_standard(self):
        sections = {item.name: item.section for item in self.form.fields}
        self.assertEqual({sections[n] for n in ("resume", "name", "email", "pronouns", "selectedLocation", "urls[LinkedIn]")}, {"standard"})
        for name in card_names(self.form):
            self.assertEqual(sections[name], "custom")
        self.assertEqual(len(card_names(self.form)), 4)

    def test_a_survey_question_that_names_a_demographic_topic_is_never_left_to_the_plan(self):
        survey = [item for item in self.form.fields if item.name.startswith("surveysResponses[")]
        self.assertEqual(len(survey), 3)
        for item in survey:
            with self.subTest(question=item.label):
                self.assertIsNotNone(apply_classify.classify_item(item, control_of(item)))

    def test_the_work_authorization_and_sponsorship_questions_are_ordinary_custom_questions(self):
        many = read("many_cards.html")
        authorized = next(item for item in many.fields if item.label.startswith("Are you legally authorized"))
        sponsorship = next(item for item in many.fields if "sponsorship" in item.label)
        self.assertEqual((authorized.section, authorized.options, authorized.required), ("custom", ("Yes", "No"), True))
        self.assertEqual(apply_classify.classify_item(authorized, control_of(authorized)), "work_authorization")
        self.assertEqual(apply_classify.classify_item(sponsorship, control_of(sponsorship)), "sponsorship")


class PostingTests(unittest.TestCase):
    """5.4 item 8: the posting's facts come from ``<title>``, and the check never splits it."""

    def test_the_title_is_the_page_title_tidied(self):
        self.assertEqual(read("demo_eeo_survey.html").posting.company_title, "Harbor Demo Labs - Customer Success Lead")
        self.assertEqual(parse_lever_form(page(title="  Two\n   words  ")).posting.company_title, "Two words")
        self.assertEqual(parse_lever_form(page(title="")).posting.company_title, "")

    def test_a_title_inside_an_inline_picture_is_not_the_pages_title(self):
        text = '<html><head><title>Real - Title</title></head><body><svg><title>Picture</title></svg><form id="application-form"></form></body></html>'
        self.assertEqual(parse_lever_form(text).posting.company_title, "Real - Title")
        in_body = '<html><body><svg><title>Picture</title></svg><form id="application-form"></form><title>Later</title></body></html>'
        self.assertEqual(parse_lever_form(in_body).posting.company_title, "Later")

    def test_the_saved_company_and_the_saved_title_each_appear_in_the_page_title(self):
        posting = read("variants.html").posting
        self.assertTrue(posting.matches("Tidewater Games", "Associate Producer - Summer Intern"), "a role may contain ' - '; the check does not split")
        self.assertTrue(posting.matches("Tidewater Games", "Associate Producer"))
        self.assertTrue(posting.matches("tidewater  games", "ASSOCIATE producer, summer intern"))

    def test_a_different_company_or_a_different_role_asks_for_the_students_tick(self):
        posting = read("variants.html").posting
        for company, title in (
            ("Other Games", "Associate Producer"), ("Tidewater Games", "Senior Producer"), ("Tidewater Games Inc", "Associate Producer"),
            ("Tidewater", "Producer Intern"), ("Tidewater Games", "Associate Prod"), ("", "Associate Producer"), ("Tidewater Games", ""),
            ("   ", "   "), ("Games Tidewater", "Associate Producer"),
        ):
            with self.subTest(company=company, title=title):
                self.assertFalse(posting.matches(company, title))

    def test_the_company_must_be_the_one_the_title_begins_with_not_a_word_in_another_employers_role(self):
        # "{Company} - {Role}": a saved company named only in the role half is another employer's posting.
        posting = parse_lever_form(page(title="Northwind Traders - Acme Integration Engineer Intern")).posting
        self.assertFalse(posting.matches("Acme", "Integration Engineer Intern"))
        self.assertTrue(posting.matches("Northwind Traders", "Integration Engineer Intern"))
        self.assertTrue(posting.matches("northwind  traders", "acme integration engineer intern"))
        # The company may still be repeated in the role, and the title is still found anywhere after it.
        again = parse_lever_form(page(title="Acme - Acme Integration Engineer Intern")).posting
        self.assertTrue(again.matches("Acme", "Acme Integration Engineer Intern"))
        # And the saved title has to be found after the company, not inside its name.
        named = parse_lever_form(page(title="Intern Works - Data Engineer")).posting
        self.assertFalse(named.matches("Intern Works", "Intern"))
        self.assertTrue(named.matches("Intern Works", "Data Engineer"))

    def test_a_word_must_be_whole_not_a_piece_of_a_longer_word(self):
        posting = parse_lever_form(page(title="Marketplace Robotics - Engineering Intern")).posting
        self.assertFalse(posting.matches("Market", "Engineering Intern"))
        self.assertTrue(posting.matches("Marketplace", "Engineering Intern"))

    def test_a_page_with_no_title_matches_nothing(self):
        posting = parse_lever_form('<form id="application-form"></form>').posting
        self.assertEqual(posting.company_title, "")
        self.assertFalse(posting.matches("Anything", "Anything"))

    def test_matches_survives_text_that_is_not_text(self):
        posting = read("variants.html").posting
        self.assertFalse(posting.matches(None, None))
        self.assertFalse(posting.matches(5, ["x"]))


class DocumentOrderTests(unittest.TestCase):
    def test_fields_follow_the_page_with_cards_in_place_and_the_surveys_after_the_eeo_block(self):
        names = [item.name for item in read("demo_eeo_survey.html").fields]
        position = {name: names.index(name) for name in ("resume", "org", "gender", "disability_status", "eeo[disabilitySignatureDate]")}
        self.assertEqual(sorted(position.values()), list(position.values()))
        first_card = next(i for i, name in enumerate(names) if name.startswith("cards["))
        first_survey = next(i for i, name in enumerate(names) if name.startswith("surveysResponses["))
        self.assertTrue(position["org"] < first_card < position["gender"] < position["eeo[disabilitySignatureDate]"] < first_survey)

    def test_the_marketing_consent_and_comments_come_after_the_last_card(self):
        names = [item.name for item in read("many_cards.html").fields]
        self.assertEqual(names[-2:], ["comments", "consent[marketing]"])

    def test_cards_are_grouped_by_card_and_ordered_by_field_number(self):
        names = card_names(read("cards_files_consent.html"))
        numbers = [int(n.rsplit("field", 1)[1].rstrip("]")) for n in names]
        self.assertEqual(numbers, [0, 1, 2, 3, 4, 5, 0, 0, 0, 1])


class SchemaRowTests(unittest.TestCase):
    """The rows are the ones policy.parse_schema gives for Greenhouse, so build_plan and the classifier read them unchanged."""

    def test_every_row_is_a_schema_field_of_a_type_the_plan_understands(self):
        known = {"input_text", "textarea", "input_file", "input_hidden", "multi_value_single_select", "multi_value_multi_select"}
        for name in ("demo_eeo_survey.html", "cards_files_consent.html", "many_cards.html", "variants.html"):
            for item in read(name).fields:
                with self.subTest(page=name, field=item.name):
                    self.assertIsInstance(item, SchemaField)
                    self.assertIn(item.type, known)
                    self.assertNotEqual(control_of(item), "unknown")
                    self.assertTrue(item.label)

    def test_no_two_rows_share_a_name(self):
        form = read("cards_files_consent.html")
        self.assertEqual(len({item.name for item in form.fields}), len(form.fields), "no two rows share a name")

    def test_the_result_is_immutable(self):
        form = read("variants.html")
        self.assertIsInstance(form.fields, tuple)
        with self.assertRaises(Exception):
            form.fields = ()
        with self.assertRaises(Exception):
            form.fields[0].label = "changed"


class StaticScanTests(unittest.TestCase):
    """Scans of the shipped modules (through helpers_source, a directory, never one file)."""

    def test_the_parser_does_no_io_and_imports_nothing_that_could(self):
        text = apply_modules()["apply/lever_form.py"]
        for word in ("urllib", "socket", "requests", "subprocess", "sqlite3", "playwright", "open(", "pathlib", "os.", "sys.", "__file__", "time."):
            with self.subTest(word=word):
                self.assertNotIn(word, text)

    def test_the_parser_never_runs_a_pattern_over_the_whole_page(self):
        text = apply_modules()["apply/lever_form.py"]
        self.assertIn("from html.parser import HTMLParser", text)
        self.assertNotRegex(text, r"re\.(?:search|match|findall|finditer|sub|split|fullmatch)\([^)]*\bhtml\b")
        self.assertNotRegex(text, r"\.(?:findall|finditer|search)\(\s*html\b")

    def test_the_submit_button_and_its_hidden_twin_are_named_nowhere_in_apply_but_the_adapters_denylist(self):
        # 10.5: the app never presses Submit, so no module under apply/ may name the controls that do (the adapter's denylist aside).
        for relative, text in apply_modules().items():
            if relative == "apply/lever_adapter.py":
                continue
            with self.subTest(module=relative):
                self.assertNotIn("btn-submit", text)
                self.assertNotIn("hcaptchaSubmitBtn", text)

    def test_the_new_modules_are_in_the_apply_scan_so_these_guards_read_them(self):
        modules = apply_modules()
        self.assertIn("apply/lever.py", modules)
        self.assertIn("apply/lever_form.py", modules)


if __name__ == "__main__":
    unittest.main()
