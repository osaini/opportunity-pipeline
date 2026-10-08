// Small helpers every page draws with: elements, links, options, form fields, formatted dates, announcements and
// errors, the two-click ask-then-act button, and the select that saves as you choose.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { SOON_DAYS, els } = App;

  function plural(count, one, many) {
    return `${Number(count).toLocaleString()} ${count === 1 ? one : many}`;
  }

  // Selects that save the moment they change. Chromium fires `change` on every
  // ArrowUp/ArrowDown (and type-ahead letter) on a focused, closed select, so
  // saving on `change` alone would record each value a keyboard user passes
  // through. Keyboard browsing only marks the select dirty; Enter or leaving
  // the select commits it, Escape puts the saved value back. A pointer choice
  // from the open list still saves at once. `commit(value, trigger)` runs at
  // most once at a time; trigger is "pointer", "enter", or "blur".
  //
  // Where the open list is the app's own (appearance: base-select in
  // styles.css), arrows on the closed select open the list and only move
  // through it, so nothing changes until a pick there (Enter, Space, or a
  // click), which saves at once. A typed letter still changes a closed select
  // directly, so it is still only browsing.
  const SELECT_BROWSE_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);

  function autoSaveSelect(select, { saved, commit }) {
    let browsing = false;
    let dirty = false;
    let busy = false;
    let keyPick = false;
    const run = async (trigger) => {
      browsing = false;
      dirty = false;
      if (busy || select.value === saved()) return;
      busy = true;
      try {
        await commit(select.value, trigger);
      } finally {
        busy = false;
      }
    };
    select.addEventListener("pointerdown", () => { browsing = false; keyPick = false; });
    select.addEventListener("keydown", (event) => {
      // A key on an option: the app's own list is open.
      if (event.target !== select) {
        if (event.key === "Enter" || event.key === " ") {
          browsing = false;
          keyPick = true;
        }
        return;
      }
      if (event.key === "Enter") {
        if (dirty || select.value !== saved()) {
          event.preventDefault();
          run("enter");
        }
        return;
      }
      if (event.key === "Escape") {
        if (!busy && select.value !== saved()) select.value = saved();
        browsing = false;
        dirty = false;
        return;
      }
      // Alt+Arrow and F4 open the list; a choice made there is a deliberate pick.
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (SELECT_BROWSE_KEYS.has(event.key) || (event.key.length === 1 && event.key !== " ")) browsing = true;
    });
    select.addEventListener("change", () => {
      if (browsing) {
        dirty = true;
        return;
      }
      const trigger = keyPick ? "enter" : "pointer";
      keyPick = false;
      run(trigger);
    });
    select.addEventListener("blur", (event) => {
      // Focus going into the app's own open list is not leaving the select either.
      if (event.relatedTarget && select.contains(event.relatedTarget)) return;
      // A list rebuild removes the focused select, which fires blur; that is
      // not the user leaving it, so do not save a choice they are still making.
      queueMicrotask(() => {
        if (!select.isConnected) return;
        if (dirty || select.value !== saved()) run("blur");
        else browsing = false;
      });
    });
  }

  function announce(message) {
    els.actionStatus.textContent = message;
    els.actionStatus.hidden = !message;
  }

  // The gate and the detail panel are modal: while either is open nothing
  // behind it may take focus or reach a screen reader. Each holds its own claim
  // so closing one never releases the other.
  const inertClaims = new Set();

  function setBackgroundInert(claim, active) {
    if (active) inertClaims.add(claim);
    else inertClaims.delete(claim);
    const inert = inertClaims.size > 0;
    [els.appShell, els.skipLink].forEach((node) => {
      node.inert = inert;
      if (inert) node.setAttribute("aria-hidden", "true");
      else node.removeAttribute("aria-hidden");
    });
  }

  // Empty or invalid reads as "".
  function formatWeekdayDateTime(stamp) {
    if (!stamp) return "";
    const moment = new Date(stamp);
    if (Number.isNaN(moment.getTime())) return "";
    return WEEKDAY_DATE_TIME_FORMAT.format(moment);
  }

  // The banner sits at the top of the page, so an error raised while the student
  // is scrolled down would go unseen. When the in-place banner is not fully on
  // screen it floats over the page (and over the detail panel) until it is
  // cleared or clicked, and the viewport edge flashes red once to draw the eye.
  const ERROR_FLASH_MS = 1600;
  let errorFlashTimer = 0;

  function flashViewportEdge() {
    const body = document.body;
    body.classList.remove("error-flash");
    void body.offsetWidth;
    body.classList.add("error-flash");
    clearTimeout(errorFlashTimer);
    errorFlashTimer = setTimeout(() => body.classList.remove("error-flash"), ERROR_FLASH_MS);
  }

  function showError(message) {
    const banner = els.error;
    banner.classList.remove("is-floating");
    banner.textContent = message;
    banner.hidden = false;
    const rect = banner.getBoundingClientRect();
    if (rect.top < 0 || rect.bottom > window.innerHeight) {
      banner.classList.add("is-floating");
      flashViewportEdge();
    }
  }

  function clearError() {
    els.error.textContent = "";
    els.error.hidden = true;
    els.error.classList.remove("is-floating");
  }

  els.error.addEventListener("click", () => {
    if (els.error.classList.contains("is-floating")) clearError();
  });

  // One formatter per shape, built on first use and then kept: they run per
  // card and per timeline row, and the first Intl formatter costs a few
  // milliseconds (locale data), which a page that shows no date should not pay
  // at load. The locale is the browser's own, which does not change mid-session,
  // but the time zone can (travel, a system change on a long-lived tab), and a
  // formatter keeps the zone it was built with while calendarDate() reads dates
  // in the current one. So a kept formatter is rebuilt when the zone changes.
  // Each function below keeps its own answer for an empty or invalid value, and
  // they differ on purpose (see each one).
  function lazyFormat(make) {
    let formatter = null;
    let zone;
    return {
      format: (...args) => {
        const current = browserTimeZone();
        if (!formatter || current !== zone) {
          formatter = make();
          zone = current;
        }
        return formatter.format(...args);
      },
    };
  }

  const DATE_FORMAT = lazyFormat(() => new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }));
  const DATE_TIME_FORMAT = lazyFormat(() => new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
  const WEEKDAY_DATE_TIME_FORMAT = lazyFormat(() => new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
  const WEEKDAY_DAY_FORMAT = lazyFormat(() => new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }));
  const CLOCK_FORMAT = lazyFormat(() => new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }));
  const RELATIVE_FORMAT = lazyFormat(() => new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }));

  // The browser's time zone name, or undefined when it reports none. The two
  // callers that send it choose their own fallback.
  function browserTimeZone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  }

  // Empty reads as "Not listed"; a value that is not a date comes back as it was.
  function formatDate(value) {
    if (!value) return "Not listed";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return DATE_FORMAT.format(date);
  }

  // Deadlines and follow-ups are calendar days. Some arrive date-only and some
  // as UTC midnight; reading either as an instant shows the previous day west
  // of UTC, contradicting the posting text beside it.
  function calendarDateParts(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})(?:T00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:00))?$/.exec(String(value || ""));
    return match ? [Number(match[1]), Number(match[2]) - 1, Number(match[3])] : null;
  }

  function calendarDate(value) {
    const parts = calendarDateParts(value);
    return parts ? new Date(...parts) : new Date(value);
  }

  function formatCalendarDate(value) {
    if (!value) return "Not listed";
    const date = calendarDate(value);
    if (Number.isNaN(date.getTime())) return value;
    return DATE_FORMAT.format(date);
  }

  function deadlineState(value) {
    if (!value) return null;
    const date = calendarDate(value);
    if (Number.isNaN(date.getTime())) return null;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const days = Math.round((target - today) / 86_400_000);
    return days < 0 ? "passed" : days <= SOON_DAYS ? "soon" : "open";
  }

  const NEW_WINDOW_MS = 48 * 60 * 60 * 1000;

  // First seen in the last 48 hours. A timestamp in the future (source clock
  // skew) is never "new".
  function isNewlySeen(value, now = Date.now()) {
    if (!value) return false;
    const seen = new Date(value).getTime();
    if (Number.isNaN(seen)) return false;
    const age = now - seen;
    return age >= 0 && age <= NEW_WINDOW_MS;
  }

  function uniqueLabels(labels) {
    const seen = new Set();
    return labels.filter((label) => {
      const key = String(label).trim().toLocaleLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  // A datetime-local input silently renders empty for anything but
  // YYYY-MM-DDTHH:MM in local time, and an empty input then reads as "clear".
  function toLocalInputValue(value) {
    if (!value) return "";
    const date = calendarDate(value);
    if (Number.isNaN(date.getTime())) return "";
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // An <a> that opens in a new tab. It does not vet the address: every caller
  // keeps the URL policy it always had (safeExternalUrl, a scheme test, or
  // none), because those policies deliberately differ.
  function externalLink(href, text, { className = "", ariaLabel = "" } = {}) {
    const link = element("a", className, text);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    if (ariaLabel) link.setAttribute("aria-label", ariaLabel);
    return link;
  }

  // The "Open in Gmail" link on a mail row; only an address inside Gmail itself gets one.
  function gmailOpenLink(url, subject) {
    if (typeof url !== "string" || !url.startsWith("https://mail.google.com/")) return null;
    return externalLink(url, "Open in Gmail", { className: "text-button", ariaLabel: `Open in Gmail: ${subject}` });
  }

  // An <option>. selected is left untouched when omitted, so a call site that
  // never set it keeps the browser's own default.
  function optionElement(value, text, selected) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = text;
    if (selected !== undefined) node.selected = selected;
    return node;
  }

  function chip(text, tone = "") {
    return element("span", `chip ${tone}`.trim(), text);
  }

  function skeletons() {
    els.results.replaceChildren();
    for (let i = 0; i < 8; i += 1) {
      const card = element("article", "opportunity-card skeleton");
      card.innerHTML = "<div></div><div></div><div></div><div></div>";
      els.results.appendChild(card);
    }
  }

  function announceWithUndo(message, undo, { automatic = false } = {}) {
    announce(message);
    const button = element("button", "text-button status-undo", "Undo");
    button.type = "button";
    // An automatic announcement's Undo may be replaced by the next one; the student's own never is.
    if (automatic) button.dataset.automatic = "true";
    button.addEventListener("click", () => {
      announce("");
      undo();
    }, { once: true });
    els.actionStatus.append(" ", button);
  }

  // Arriving from Urgent: bring what the row was about into view and onto the keyboard.
  function revealRequested(node) {
    node.setAttribute("tabindex", "-1");
    node.classList.add("is-requested");
    node.scrollIntoView({ block: "center" });
    node.focus({ preventScroll: true });
  }

  function safeExternalUrl(value) {
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
      if (!["http:", "https:"].includes(parsed.protocol) || !host || parsed.username || parsed.password) return null;
      if (host === "localhost" || host.endsWith(".localhost") || host.includes(":") || /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) return null;
      return parsed.href;
    } catch (_error) {
      return null;
    }
  }

  // With Gmail connected, the approved draft can be sent straight from here.
  // A sent email cannot be taken back, so the first click only asks: the
  // button turns into "Send to <address>?" and a second click sends. Leaving
  // it, pressing Escape, or waiting a few seconds puts it back.
  const SEND_CONFIRM_MS = 8000;

  // Gives a button the two-click ask: the first click arms it (the label turns
  // into the question, the prompt is announced, and the arm lapses after
  // SEND_CONFIRM_MS), a second click runs onConfirm. Leaving the button or
  // pressing Escape disarms it. dataset.confirming stays set until reset(), so
  // it is still set while onConfirm is in flight. beforeClick returns true to
  // refuse the click outright; shouldArm false makes a click confirm at once.
  // Returns reset(), for onConfirm to put the button back after a failure.
  function armConfirm(button, { idleLabel, armedLabel, prompt, idleAriaLabel, armedAriaLabel, confirmValue = "true", shouldArm = () => true, beforeClick, onConfirm }) {
    let timer = null;
    const reset = () => {
      clearTimeout(timer);
      timer = null;
      delete button.dataset.confirming;
      button.textContent = idleLabel();
      if (idleAriaLabel) button.setAttribute("aria-label", idleAriaLabel());
    };
    button.addEventListener("blur", () => { if (button.dataset.confirming) reset(); });
    button.addEventListener("keydown", (event) => { if (event.key === "Escape" && button.dataset.confirming) reset(); });
    button.addEventListener("click", async () => {
      if (beforeClick?.()) return;
      if (shouldArm() && !button.dataset.confirming) {
        button.dataset.confirming = confirmValue;
        button.textContent = armedLabel();
        if (armedAriaLabel) button.setAttribute("aria-label", armedAriaLabel());
        announce(prompt());
        timer = setTimeout(reset, SEND_CONFIRM_MS);
        return;
      }
      clearTimeout(timer);
      await onConfirm();
    });
    return reset;
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.setAttribute("readonly", "");
    scratch.className = "sr-only";
    document.body.appendChild(scratch);
    scratch.select();
    const copied = document.execCommand("copy");
    scratch.remove();
    if (!copied) throw new Error("Copy is blocked in this browser; select the draft text instead.");
  }

  // No empty-value guard: null is the epoch (new Date(null) is valid), while ""
  // and undefined come back as "".
  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    return DATE_TIME_FORMAT.format(date);
  }

  function profileField(form, labelText, name, value, options = {}) {
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", labelText));
    const control = options.multiline ? document.createElement("textarea") : document.createElement("input");
    control.name = name;
    if (!options.multiline) control.type = options.type || "text";
    if (options.placeholder) control.placeholder = options.placeholder;
    if (options.autocomplete) control.autocomplete = options.autocomplete;
    if (options.min !== undefined) control.min = String(options.min);
    if (options.max !== undefined) control.max = String(options.max);
    control.value = value ?? "";
    label.appendChild(control);
    form.appendChild(label);
    return control;
  }

  function profileSelect(form, labelText, name, value) {
    const label = element("label", "profile-field");
    label.appendChild(element("span", "", labelText));
    const select = document.createElement("select");
    select.name = name;
    [["", "Not answered"], ["true", "Yes"], ["false", "No"]].forEach(([optionValue, text]) => {
      select.appendChild(optionElement(optionValue, text, value === null || value === undefined ? optionValue === "" : optionValue === String(value)));
    });
    label.appendChild(select);
    form.appendChild(label);
    return select;
  }

  // One titled group of the career profile form.
  function profileGroup(form, title, help = "") {
    const group = element("div", "profile-group");
    group.appendChild(element("h4", "", title));
    if (help) group.appendChild(element("p", "profile-help", help));
    form.appendChild(group);
    return group;
  }

  // A titled part of a Profile card, set off from the one above it.
  function profileBlock(section, title, help = "") {
    const block = element("div", "profile-block");
    block.appendChild(element("h4", "", title));
    if (help) block.appendChild(element("p", "profile-help", help));
    section.appendChild(block);
    return block;
  }

  // The red asterisk on a field the profile counts toward being complete
  // (profile.COMPLETENESS_FIELDS). The key above the form says what it means;
  // a screen reader hears that key as the field's description instead.
  function requiredMark() {
    const mark = element("span", "required-mark", "*");
    mark.setAttribute("aria-hidden", "true");
    return mark;
  }

  function markNeeded(control) {
    control.closest("label").querySelector("span").appendChild(requiredMark());
    control.setAttribute("aria-describedby", "profile-required-key");
  }

  function commaList(value) {
    if (!value) return [];
    return value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
  }

  function nullableBoolean(value) {
    if (value === "") return null;
    return value === "true";
  }

  // Applications for a picker: the ones an email or a proposal matched first, in
  // their ranked order, then every other by company and role. ``selected`` is
  // preselected; with none, the picker asks for a choice.
  function applicationPicker(applications, candidates, selected) {
    const select = document.createElement("select");
    const rank = new Map((Array.isArray(candidates) ? candidates : []).map((id, index) => [id, index]));
    const sorted = [...applications].sort((a, b) => {
      const ra = rank.has(a.id) ? rank.get(a.id) : Infinity;
      const rb = rank.has(b.id) ? rank.get(b.id) : Infinity;
      if (ra !== rb) return ra - rb;
      return `${a.company} ${a.title}`.localeCompare(`${b.company} ${b.title}`, undefined, { sensitivity: "base" });
    });
    if (!selected || !sorted.some((application) => application.id === selected)) {
      select.appendChild(optionElement("", "Choose an application"));
    }
    sorted.forEach((application) => {
      select.appendChild(optionElement(application.id, `${application.company} — ${application.title}${rank.has(application.id) ? " (matched)" : ""}`));
    });
    select.value = sorted.some((application) => application.id === selected) ? selected : "";
    return select;
  }

  function humanizeKey(key) {
    const text = String(key || "").replace(/[_.]+/g, " ").trim();
    return text ? text[0].toUpperCase() + text.slice(1) : "";
  }

  // How a sentence names the job system a record came from: the server's ats_name (apply/ats.py display_name). A record written
  // before the name was kept is Greenhouse's, because Greenhouse was the only one then.
  function atsName(record) {
    return (record && typeof record.ats_name === "string" && record.ats_name) || "Greenhouse";
  }

  function timeAgo(stamp) {
    const moment = new Date(stamp);
    if (!stamp || Number.isNaN(moment.getTime())) return "";
    const seconds = Math.round((moment.getTime() - Date.now()) / 1000);
    for (const [unit, size] of [["year", 31_536_000], ["month", 2_592_000], ["week", 604_800], ["day", 86_400], ["hour", 3_600], ["minute", 60]]) {
      if (Math.abs(seconds) >= size) return RELATIVE_FORMAT.format(Math.round(seconds / size), unit);
    }
    return "just now";
  }

  // One dated group on Urgent or on Programs: a titled count over a list of
  // rows. Each page passes its own element id and class name.
  function groupSection({ id, className, label, items, noun, nouns, row }) {
    const section = element("section", `urgent-group ${className}`);
    const heading = element("h3", "urgent-group-title", label);
    heading.id = id;
    const count = element("span", "urgent-count", String(items.length));
    count.setAttribute("aria-label", plural(items.length, noun, nouns));
    heading.appendChild(count);
    section.setAttribute("aria-labelledby", heading.id);
    const list = element("ul", "urgent-list");
    items.forEach((item) => list.appendChild(row(item)));
    section.append(heading, list);
    return section;
  }

  // Only web addresses become links in the Apply panel (the page's own wording comes from the employer).
  const HTTP_ADDRESS = /^https?:\/\//i;

  function webAddresses(links) {
    return (links || []).filter((address) => HTTP_ADDRESS.test(address));
  }

  // Each address as a link: the first after firstLead, the rest after sep.
  function appendLinks(parent, addresses, firstLead, sep = ", ") {
    addresses.forEach((address, index) => {
      parent.append(index ? sep : firstLead);
      parent.appendChild(externalLink(address, address));
    });
  }

  // A page draws part of itself after it opens. Looks for what find() returns
  // every `ms` for `tries` looks, and hands the first hit to onFound; gives up
  // quietly after the last look. Scroll and focus stay the caller's business.
  function whenPresent(find, onFound, { tries = 25, ms = 200 } = {}) {
    let looks = 0;
    const look = () => {
      const found = find();
      if (found) onFound(found);
      else if ((looks += 1) < tries) setTimeout(look, ms);
    };
    setTimeout(look, ms);
  }

  Object.assign(App, {
    CLOCK_FORMAT, HTTP_ADDRESS, WEEKDAY_DAY_FORMAT, announce, announceWithUndo, appendLinks, applicationPicker,
    armConfirm, atsName, autoSaveSelect, browserTimeZone, chip, clearError, commaList, copyText, deadlineState, element,
    externalLink, formatCalendarDate, formatDate, formatDateTime, formatWeekdayDateTime, gmailOpenLink, groupSection,
    humanizeKey, inertClaims, isNewlySeen, markNeeded, nullableBoolean, optionElement, plural, profileBlock,
    profileField, profileGroup, profileSelect, requiredMark, revealRequested, safeExternalUrl, setBackgroundInert,
    showError, skeletons, timeAgo, toLocalInputValue, uniqueLabels, webAddresses, whenPresent,
  });
})();
