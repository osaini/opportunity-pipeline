// Urgent: everything dated, grouped, with the calendar export.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { SOON_DAYS, els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const { announce, element, formatCalendarDate, groupSection, plural } = App;

  // From app-http.js.
  const { api } = App;

  // From app-status.js.
  const { applyFreshUrgentCount, urgentBadge } = App;

  // From app-nav.js.
  const { loadingLine, renderSubnav, runViewLoad, setView } = App;

  // From app-programs.js.
  const { programsMeta } = App;

  // Defined in files that load later; looked up when called.
  const openDetail = (...args) => App.openDetail(...args);

  const URGENT_KIND_LABELS = {
    posting_deadline: "Deadline",
    your_deadline: "Your deadline",
    program_deadline: "Program deadline",
    outreach_deadline: "Outreach deadline",
    email_deadline: "Deadline from an email",
    task: "Task",
    application_follow_up: "Follow-up",
    outreach_follow_up: "Outreach follow-up",
    outreach_revisit: "Outreach revisit",
    outreach_possible_reply: "Outreach possible reply",
    application_silence: "No reply yet",
    apply_needs_you: "Apply for me needs you",
    apply_no_email: "No confirmation email yet",
  };
  const URGENT_GROUPS = [
    ["overdue", "Overdue", (item) => item.days_until < 0],
    ["today", "Today", (item) => item.days_until === 0],
    ["week", "This week", (item) => item.days_until >= 1 && item.days_until <= 6],
    ["later", "Later", (item) => item.days_until >= 7],
  ];

  async function loadUrgent() {
    await runViewLoad({ views: ["urgent"], placeholder: loadingLine("Loading what is due…") }, async ({ isCurrent }) => {
      const payload = await api(`/api/v1/urgent?days=${SOON_DAYS}`);
      if (!isCurrent()) return;
      renderUrgent(payload);
      urgentBadge.generation += 1;
      applyFreshUrgentCount(payload.counts.attention);
    });
  }

  function urgentWhen(item) {
    if (item.days_until < 0) return `${plural(-item.days_until, "day", "days")} overdue`;
    if (item.days_until === 0) return "Today";
    if (item.days_until === 1) return "Tomorrow";
    return `In ${item.days_until} days`;
  }

  function urgentHeadline(item) {
    if (item.kind.startsWith("outreach_")) return [item.company, "Cold outreach"];
    if (item.kind === "task" || item.kind === "application_silence" || item.kind.startsWith("apply_")) return [item.title, [item.company, item.subtitle].filter(Boolean).join(" · ")];
    return [item.title, item.company];
  }

  function urgentAction(item) {
    const [headline, context] = urgentHeadline(item);
    if (item.kind === "posting_deadline" || item.kind === "your_deadline") {
      return ["Open role", `Open ${headline} at ${context}`, () => openDetail(item.opportunity_id)];
    }
    if (item.kind === "program_deadline") {
      return ["Open program", `Open ${headline} in ${programsMeta.label}`, () => {
        state.subtabs.programs = "all";
        state.programsFocus = item.program_id;
        setView("programs");
      }];
    }
    if (item.kind === "task" || item.kind === "application_follow_up" || item.kind === "application_silence" || item.kind === "email_deadline" || item.kind.startsWith("apply_")) {
      return ["Open application", `Open the application for ${item.company}`, () => {
        state.applicationFocus = item.application_id;
        setView("applications");
      }];
    }
    return ["Open outreach", `Open outreach for ${item.company}`, () => {
      state.outreachOpen = item.outreach_target_id;
      state.outreachFocus = item.outreach_target_id;
      setView("outreach");
    }];
  }

  function urgentRow(item, items) {
    const row = element("li", `urgent-row${item.overdue ? " is-overdue" : ""}`);
    const when = element("div", "urgent-when");
    when.appendChild(element("strong", "", formatCalendarDate(item.date)));
    when.appendChild(element("span", "", urgentWhen(item)));

    const body = element("div", "urgent-body");
    const [headline, context] = urgentHeadline(item);
    body.appendChild(element("p", "urgent-kind", URGENT_KIND_LABELS[item.kind] || item.kind));
    body.appendChild(element("h4", "", headline));
    if (context) body.appendChild(element("p", "urgent-context", context));
    // Where the date came from, and for a task the app added, where the task came from.
    const origin = item.kind === "task" && item.origin_label ? ` · ${item.origin_label}` : "";
    body.appendChild(element("p", "urgent-source", `${item.source_name ? `${item.date_source} · ${item.source_name}` : item.date_source}${origin}`));
    // A researched date can carry its own caveat ("estimated", "rolling");
    // show it so an estimate never reads as a confirmed deadline.
    if (item.date_note) body.appendChild(element("p", "urgent-source urgent-date-note", item.date_note));
    if (item.kind === "posting_deadline" || item.kind === "your_deadline") {
      const twin = items.find((other) => other !== item
        && other.opportunity_id === item.opportunity_id
        && (other.kind === "posting_deadline" || other.kind === "your_deadline"));
      if (twin) body.appendChild(element("p", "urgent-also", `Also: ${twin.date_source.toLocaleLowerCase()} ${formatCalendarDate(twin.date)}`));
    }

    const [label, name, run] = urgentAction(item);
    const action = element("button", "secondary-button urgent-action", label);
    action.type = "button";
    action.setAttribute("aria-label", name);
    action.addEventListener("click", run);
    row.append(when, body, action);
    return row;
  }

  const URGENT_TABS = [
    { id: "all", label: "Everything dated", test: () => true },
    ...URGENT_GROUPS.map(([key, label, test]) => ({ id: key, label, group: "When", tone: key === "overdue" ? "is-alert" : key === "today" ? "is-soon" : "", test })),
    { id: "deadlines", label: "Deadlines", group: "Kind", test: (item) => ["posting_deadline", "your_deadline", "program_deadline", "outreach_deadline", "email_deadline"].includes(item.kind) },
    { id: "follow-ups", label: "Follow-ups", group: "Kind", test: (item) => ["application_follow_up", "application_silence", "outreach_follow_up", "outreach_revisit", "outreach_possible_reply", "apply_needs_you", "apply_no_email"].includes(item.kind) },
    { id: "tasks", label: "Tasks", group: "Kind", test: (item) => item.kind === "task" },
  ];

  function renderUrgent(payload) {
    els.results.replaceChildren();
    els.results.removeAttribute("role");
    const tab = URGENT_TABS.find((entry) => entry.id === state.subtabs.urgent) || URGENT_TABS[0];
    renderSubnav(URGENT_TABS.map((entry) => ({ ...entry, count: payload.items.filter(entry.test).length })));
    const shown = payload.items.filter(tab.test);
    const total = payload.items.length;
    els.resultCount.textContent = total ? `${plural(total, "item", "items")} with a date` : "Nothing due";
    els.pageStatus.textContent = `${payload.counts.overdue} overdue · ${payload.counts.upcoming} in the next ${payload.window_days} days`;

    const toolbar = element("div", "urgent-toolbar");
    const zone = payload.timezone === "system-local"
      ? `this computer's time zone (UTC${payload.utc_offset})`
      : `${payload.timezone.replaceAll("_", " ")} (UTC${payload.utc_offset})`;
    toolbar.appendChild(element("p", "urgent-note", `Today is ${formatCalendarDate(payload.today)} in ${zone}.`));
    const calendar = element("button", "secondary-button", "Add to calendar (.ics)");
    calendar.type = "button";
    calendar.disabled = !total;
    calendar.addEventListener("click", () => downloadUrgentCalendar(payload));
    toolbar.appendChild(calendar);
    els.results.appendChild(toolbar);

    if (!total) {
      const empty = element("div", "empty-state urgent-empty");
      empty.appendChild(element("h3", "", "Nothing is due in the next two weeks"));
      empty.appendChild(element(
        "p",
        "",
        "Urgent fills from dated records: deadlines stated in posting text, deadlines you enter on a role, outreach deadlines, open application tasks, and follow-up dates. Most postings list no deadline, so open a role and add the one you find on the employer's site."
      ));
      els.results.appendChild(empty);
    }

    if (total && !shown.length) {
      const empty = element("div", "empty-state urgent-empty");
      empty.appendChild(element("h3", "", `Nothing under ${tab.label}`));
      empty.appendChild(element("p", "", "Everything else that is due is under Everything dated."));
      els.results.appendChild(empty);
    }

    URGENT_GROUPS.forEach(([key, label, test]) => {
      const items = shown.filter(test);
      if (!items.length) return;
      els.results.appendChild(groupSection({
        id: `urgent-group-${key}`, className: `is-${key}`, label, items, noun: "item", nouns: "items",
        row: (item) => urgentRow(item, payload.items),
      }));
    });

    const notes = [];
    if (payload.older_overdue) notes.push(`${plural(payload.older_overdue, "item is", "items are")} more than 60 days overdue and not shown.`);
    if (payload.skipped_count) {
      const named = payload.skipped.map((entry) => `${URGENT_KIND_LABELS[entry.kind] || entry.kind} for ${entry.company || entry.title || entry.key}`).join("; ");
      notes.push(`${plural(payload.skipped_count, "item has", "items have")} a date that could not be read: ${named}.`);
    }
    notes.forEach((note) => els.results.appendChild(element("p", "urgent-footnote", note)));
    els.results.setAttribute("aria-busy", "false");
  }

  // RFC 5545 text: escape, CRLF line ends, and fold at 75 octets without ever
  // splitting a UTF-8 sequence (iteration is by code point).
  function icsText(value) {
    return String(value ?? "")
      .replace(/\\/g, "\\\\")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,")
      .replace(/\r\n|\r|\n/g, "\\n");
  }

  function icsFold(line) {
    const encoder = new TextEncoder();
    if (encoder.encode(line).length <= 75) return line;
    const parts = [];
    let current = "";
    let size = 0;
    let limit = 75;
    for (const character of line) {
      const bytes = encoder.encode(character).length;
      if (size + bytes > limit) {
        parts.push(current);
        current = character;
        size = bytes;
        // A continuation line starts with a space, which counts toward 75.
        limit = 74;
      } else {
        current += character;
        size += bytes;
      }
    }
    parts.push(current);
    return parts.join("\r\n ");
  }

  function icsDate(value) {
    return value.replaceAll("-", "");
  }

  function icsNextDay(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10).replaceAll("-", "");
  }

  function buildUrgentCalendar(items, now = new Date()) {
    const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//Opportunity Pipeline//Urgent export//EN",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
    ];
    items.forEach((item) => {
      const [headline, context] = urgentHeadline(item);
      const label = URGENT_KIND_LABELS[item.kind] || item.kind;
      const summary = context && !item.kind.startsWith("outreach_")
        ? `${label}: ${headline} (${context})`
        : `${label}: ${headline}`;
      lines.push(
        "BEGIN:VEVENT",
        // Independent of the date, so re-importing after an edit updates the event.
        `UID:${icsText(item.key)}@opportunity-pipeline.local`,
        `DTSTAMP:${stamp}`,
        `DTSTART;VALUE=DATE:${icsDate(item.date)}`,
        `DTEND;VALUE=DATE:${icsNextDay(item.date)}`,
        `SUMMARY:${icsText(summary)}`,
        `DESCRIPTION:${icsText(`${item.date_source}. Exported from Opportunity Pipeline.`)}`,
        "TRANSP:TRANSPARENT",
        "END:VEVENT"
      );
    });
    lines.push("END:VCALENDAR");
    return `${lines.map(icsFold).join("\r\n")}\r\n`;
  }

  function downloadUrgentCalendar(payload) {
    const blob = new Blob([buildUrgentCalendar(payload.items)], { type: "text/calendar;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = element("a");
    link.href = url;
    link.download = `urgent-${payload.today}.ics`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    announce(`Downloaded ${plural(payload.items.length, "calendar event", "calendar events")}. Nothing was sent anywhere.`);
  }

  // What this file does as it loads, run once by app.js in load order.
  function installUrgent() {
    registerViewHandlers("urgent", { tabs: () => URGENT_TABS, load: loadUrgent });
  }

  Object.assign(App, {
    installUrgent,
  });
})();
