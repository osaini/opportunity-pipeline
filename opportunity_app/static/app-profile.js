// Profile: the editor, resumes, notification settings, the dossier, the account and extension sections, and the page
// loader.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const {
    announce, applicationPicker, chip, commaList, element, formatDate, humanizeKey, markNeeded, nullableBoolean,
    optionElement, plural, profileBlock, profileField, profileGroup, profileSelect, requiredMark, showError,
  } = App;

  // From app-http.js.
  const { api, isAuthError } = App;

  // From app-status.js.
  const { applyAutomationRead, startAutomationRead } = App;

  // From app-automation.js.
  const {
    AUTOMATION_LIST_KEYS, automationCheckbox, automationFeature, automationListRequests, automationSection,
  } = App;

  // From app-nav.js.
  const { loadingLine, runViewLoad, sectionSubnav, tagSection } = App;

  // From app-session.js.
  const { showAuth } = App;

  function renderProfileEditor(payload) {
    const profile = payload.profile || {};
    const card = element("section", "profile-card");
    const top = element("div", "profile-card-heading");
    const copy = element("div");
    copy.appendChild(element("h3", "", "Profile and preferences"));
    copy.appendChild(element("p", "profile-help", "Saving is an explicit confirmation of these fields. Changes feed the deterministic scoring profile."));
    const meter = element("div", "completeness-meter");
    meter.appendChild(element("strong", "", `${payload.completeness.percent}%`));
    meter.appendChild(element("span", "", "complete"));
    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = payload.completeness.percent;
    progress.setAttribute("aria-label", `Profile ${payload.completeness.percent}% complete`);
    meter.appendChild(progress);
    top.append(copy, meter);
    card.appendChild(top);

    if (payload.completeness.missing.length) {
      const labels = {
        interest_keywords: "interests",
        available_terms: "available terms",
        graduation_year: "graduation year",
        work_authorized_us: "U.S. work authorization",
        requires_sponsorship: "sponsorship needs",
        compensation_preferences: "pay preferences",
      };
      const missing = payload.completeness.missing.map((field) => labels[field] || field.replaceAll("_", " "));
      card.appendChild(element("p", "missing-note", `Still needed: ${missing.join(", ")}.`));
    }

    const form = element("form", "profile-form");
    const key = element("p", "profile-required-key");
    key.id = "profile-required-key";
    key.append(requiredMark(), document.createTextNode(" Needed to finish your profile. You can save without them; for pay, either answer counts."));
    form.appendChild(key);
    const about = profileGroup(form, "About you");
    const name = profileField(about, "Name", "name", profile.name);
    // Apply for me types these into an employer's application form, and it will not run without a confirmed email.
    const contactSaved = profile.contact && typeof profile.contact === "object" && !Array.isArray(profile.contact) ? profile.contact : {};
    const contactEmail = profileField(about, "Email for applications", "contact_email", contactSaved.email, { type: "email", placeholder: "you@example.com" });
    const contactPhone = profileField(about, "Phone for applications (optional)", "contact_phone", contactSaved.phone, { type: "tel" });
    // How your name is typed into an employer's application form (Apply for me). A name of more than two words is never split for you.
    const nameParts = profile.name_parts && typeof profile.name_parts === "object" ? profile.name_parts : {};
    const nameForApplications = element("fieldset", "profile-fieldset");
    nameForApplications.appendChild(element("legend", "", "Name for applications"));
    nameForApplications.appendChild(element("p", "profile-help", "Apply for me types these into an employer's first name and last name boxes. It never splits a longer name for you, so fill these in if your name has more than two words."));
    const firstForApplications = profileField(nameForApplications, "First name", "name_parts_first", nameParts.first);
    const lastForApplications = profileField(nameForApplications, "Last name", "name_parts_last", nameParts.last);
    const preferredForApplications = profileField(nameForApplications, "Preferred name (optional)", "name_parts_preferred", nameParts.preferred);
    about.appendChild(nameForApplications);
    const education = profileGroup(form, "Education");
    const school = profileField(education, "School", "school", profile.school);
    const degree = profileField(education, "Degree", "degree", profile.degree);
    const graduation = profileField(education, "Graduation year", "graduation_year", profile.graduation_year, { type: "number", min: 2000, max: 2200 });
    const looking = profileGroup(form, "What you're looking for");
    const skills = profileField(looking, "Skills (comma or line separated)", "skills", (profile.skills || []).join(", "), { multiline: true });
    const interests = profileField(looking, "Interests", "interest_keywords", (profile.interest_keywords || []).join(", "), { multiline: true });
    const terms = profileField(looking, "Available terms", "available_terms", (profile.available_terms || []).join(", "), { multiline: true });
    const hours = profileField(looking, "Hours per week", "hours_per_week", profile.hours_per_week, { type: "number", min: 1, max: 80 });
    const where = profileGroup(form, "Where");
    const locations = profileField(
      where,
      "Target regions",
      "regions",
      (profile.regions || []).map((region) => typeof region === "string" ? region : region.name).filter(Boolean).join(", "),
      { placeholder: "Atlanta, Bay Area" }
    );
    const breakLocation = profileField(where, "Home during breaks and summers", "break_location", profile.break_location, { placeholder: "City, ST or a target region" });
    const relocation = profileSelect(where, "Willing to relocate", "willing_to_relocate", profile.willing_to_relocate);
    const eligibility = profileGroup(form, "Work eligibility");
    const workAuthorized = profileSelect(eligibility, "Authorized to work in the U.S.", "work_authorized_us", profile.work_authorized_us);
    const citizen = profileSelect(eligibility, "U.S. citizen", "us_citizen", profile.us_citizen);
    const sponsorship = profileSelect(eligibility, "Requires sponsorship", "requires_sponsorship", profile.requires_sponsorship);
    // Pay preferences count as answered once either one is.
    const pay = profileGroup(form, "Pay", "Either answer completes your pay preferences.");
    pay.querySelector("h4").appendChild(requiredMark());
    const compensation = profile.compensation_preferences || {};
    const paidOnly = profileSelect(pay, "Paid roles only", "paid_only", compensation.paid_only);
    const minimumPay = profileField(pay, "Minimum hourly pay (USD)", "minimum_pay", compensation.minimum_hourly, { type: "number", min: 0 });
    [paidOnly, minimumPay].forEach((control) => control.setAttribute("aria-describedby", "profile-required-key"));
    // How outreach emails open, in the student's own words: "Hi Dana," or "Hello there,".
    const greetings = profileGroup(form, "Outreach emails", "How your cold emails greet someone.");
    const greetingWord = profileField(greetings, "Email greeting", "greeting_word", profile.greeting_word, { placeholder: "Hi" });
    const unnamedGreeting = profileField(greetings, "Greeting for a shared inbox", "unnamed_greeting", profile.unnamed_greeting, { placeholder: "{company} team" });
    [name, school, degree, graduation, skills, interests, terms, locations, workAuthorized, sponsorship].forEach(markNeeded);

    const statusLine = element("p", "form-status");
    statusLine.setAttribute("aria-live", "polite");
    if (state.profileStatus) {
      statusLine.textContent = state.profileStatus;
      state.profileStatus = null;
    }
    const save = element("button", "primary-button", "Save and confirm profile");
    save.type = "submit";
    const exportLink = element("a", "secondary-button profile-export", "Export scoring profile");
    exportLink.href = "/api/v1/profile/export";
    // Pinned to the bottom of the screen while the form scrolls, so Save is always in reach.
    const actions = element("div", "profile-actions");
    actions.append(statusLine, exportLink, save);
    form.appendChild(actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      statusLine.textContent = "Saving…";
      const regionNames = commaList(locations.value);
      const existingRegions = new Map((profile.regions || []).filter((item) => item && typeof item === "object").map((item) => [String(item.name).toLowerCase(), item]));
      const regions = regionNames.map((regionName) => existingRegions.get(regionName.toLowerCase()) || {
        name: regionName,
        radius: "selected",
        bonus: 10,
        state_markers: [],
        aliases: [],
        places: [regionName.toLowerCase()],
      });
      const namePartsValue = { first: firstForApplications.value.trim(), last: lastForApplications.value.trim(), preferred: preferredForApplications.value.trim() };
      // Anything else already saved under contact (from a résumé) is kept; only the email and phone are edited here.
      const contactValue = { ...contactSaved };
      [["email", contactEmail], ["phone", contactPhone]].forEach(([key, input]) => {
        if (input.value.trim()) contactValue[key] = input.value.trim();
        else delete contactValue[key];
      });
      const updates = {
        name: name.value.trim(),
        // Sent only when there is something to say, or something already saved to clear.
        ...(Object.keys(contactValue).length || profile.contact ? { contact: contactValue } : {}),
        // Sent only when there is something to say, or something already saved to clear.
        ...(Object.values(namePartsValue).some(Boolean) || profile.name_parts ? { name_parts: namePartsValue } : {}),
        school: school.value.trim(),
        degree: degree.value.trim(),
        graduation_year: graduation.value ? Number(graduation.value) : null,
        skills: commaList(skills.value),
        interest_keywords: commaList(interests.value),
        available_terms: commaList(terms.value),
        regions,
        preferred_locations: regionNames,
        break_location: breakLocation.value.trim(),
        greeting_word: greetingWord.value.trim(),
        unnamed_greeting: unnamedGreeting.value.trim(),
        hours_per_week: hours.value ? Number(hours.value) : null,
        work_authorized_us: nullableBoolean(workAuthorized.value),
        us_citizen: nullableBoolean(citizen.value),
        requires_sponsorship: nullableBoolean(sponsorship.value),
        willing_to_relocate: nullableBoolean(relocation.value),
        compensation_preferences: {
          paid_only: nullableBoolean(paidOnly.value),
          minimum_hourly: minimumPay.value ? Number(minimumPay.value) : null,
          currency: "USD",
        },
      };
      // "Not answered" is not a confirmation; the server enforces this too.
      const answered = (value) => Array.isArray(value)
        ? value.length > 0
        : value && typeof value === "object"
          ? Object.entries(value).some(([key, item]) => key !== "currency" && answered(item))
          : value !== null && value !== undefined && value !== "";
      try {
        await api("/api/v1/profile", {
          method: "PUT",
          body: JSON.stringify({ updates, confirmed_fields: Object.keys(updates).filter((field) => answered(updates[field])) }),
        });
        state.profileStatus = "Profile saved and confirmed.";
        await loadProfile();
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    card.appendChild(form);
    return card;
  }

  function createResumeCard(record) {
    const card = element("article", "resume-card");
    const heading = element("div", "resume-heading");
    const identity = element("div");
    identity.appendChild(element("strong", "", record.original_name));
    identity.appendChild(element("span", "", `${Math.ceil(record.byte_size / 1024)} KB · ${record.status}`));
    const chips = element("div", "resume-chips");
    chips.appendChild(chip(record.status === "confirmed" ? "Confirmed" : "Needs review", record.status === "confirmed" ? "is-region" : ""));
    if (record.variant_label) chips.appendChild(chip(`Variant: ${record.variant_label}`));
    heading.append(identity, chips);
    card.appendChild(heading);

    const preview = element("details", "resume-preview");
    preview.appendChild(element("summary", "", "Review extracted text"));
    preview.appendChild(element("pre", "", record.extracted_text));
    card.appendChild(preview);

    const suggestions = record.parsed?.profile_suggestions || {};
    if (record.status !== "confirmed") {
      const review = element("form", "resume-review");
      review.appendChild(element("p", "profile-help", "Select only facts you reviewed and want copied into your profile."));
      const checkboxes = [];
      Object.entries(suggestions).forEach(([field, value]) => {
        const label = element("label", "confirmation-row");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = field;
        const display = typeof value === "object" ? JSON.stringify(value) : String(value);
        label.append(checkbox, element("span", "", `${field.replaceAll("_", " ")}: ${display}`));
        review.appendChild(label);
        checkboxes.push([checkbox, field, value]);
      });
      const confirm = element("button", "secondary-button", "Confirm selected facts");
      confirm.type = "submit";
      confirm.disabled = checkboxes.length === 0;
      const reviewStatus = element("p", "form-status");
      reviewStatus.setAttribute("aria-live", "polite");
      review.append(confirm, reviewStatus);
      review.addEventListener("submit", async (event) => {
        event.preventDefault();
        const selected = checkboxes.filter(([checkbox]) => checkbox.checked);
        if (!selected.length) {
          reviewStatus.textContent = "Select at least one reviewed fact.";
          return;
        }
        confirm.disabled = true;
        try {
          const profileUpdates = Object.fromEntries(selected.map(([, field, value]) => [field, value]));
          await api(`/api/v1/resumes/${encodeURIComponent(record.id)}/confirm`, {
            method: "POST",
            body: JSON.stringify({
              confirmed_data: profileUpdates,
              profile_updates: profileUpdates,
              confirmed_profile_fields: Object.keys(profileUpdates),
            }),
          });
          await loadProfile();
        } catch (error) {
          reviewStatus.textContent = error.message;
          confirm.disabled = false;
        }
      });
      card.appendChild(review);
    }

    card.appendChild(resumeVariantForm(record));

    const actions = element("div", "resume-actions");
    const download = element("a", "secondary-button", "Download original");
    download.href = `/api/v1/resumes/${encodeURIComponent(record.id)}/file`;
    const remove = element("button", "danger-button", "Delete");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Permanently delete ${record.original_name}?`)) return;
      remove.disabled = true;
      try {
        await api(`/api/v1/resumes/${encodeURIComponent(record.id)}`, { method: "DELETE" });
        await loadProfile();
      } catch (error) {
        showError(error.message);
        remove.disabled = false;
      }
    });
    actions.append(download, remove);
    card.appendChild(actions);
    return card;
  }

  // "Use as a variant": a résumé kept for one kind of role, confirmed as a
  // document to send. Nothing from it is copied into the profile, so a second
  // variant never overwrites the facts the main résumé confirmed.
  // What the résumé section says about variants. Only a label the profile lists
  // under resume_variants, on a confirmed résumé, is ever picked (resume_variants.variant_setup).
  function resumeVariantSummary(resumesPayload) {
    const setup = resumesPayload?.variants || {};
    const list = (value) => (Array.isArray(value) ? value : []);
    const usable = list(setup.usable);
    const configured = list(setup.configured);
    const unlisted = list(setup.unlisted);
    const where = "The words that mark each kind of role go under resume_variants in your profile file.";
    const unlistedText = unlisted.length
      ? ` ${unlisted.join(", ")} ${unlisted.length === 1 ? "is not listed there, so it is" : "are not listed there, so they are"} never picked.`
      : "";
    if (usable.length) {
      return `${plural(usable.length, "résumé variant", "résumé variants")} ready: ${usable.join(", ")}. Roles you save from now on get the one that fits when the résumé variant switch is on under Automation. ${where}${unlistedText}`;
    }
    if (configured.length) {
      return `Your profile lists ${configured.join(", ")}, but no confirmed résumé carries ${configured.length === 1 ? "that label" : "those labels"} yet, so nothing is picked. Label one below with Use as a variant.${unlistedText}`;
    }
    if (unlisted.length) {
      return `${plural(unlisted.length, "résumé carries", "résumés carry")} a variant label, but your profile file lists no resume_variants yet, so nothing is picked. ${where}`;
    }
    return "No résumé variants yet. With one résumé, the app uses it for every role, as before.";
  }

  function resumeVariantForm(record) {
    const form = element("form", "resume-variant");
    const id = `resume-variant-${record.id}`;
    const label = element("label", "profile-field");
    label.htmlFor = id;
    label.appendChild(element("span", "", "Variant label"));
    const input = document.createElement("input");
    input.type = "text";
    input.id = id;
    input.maxLength = 60;
    input.placeholder = "The kind of role it is for";
    input.value = record.variant_label || "";
    label.appendChild(input);
    const help = element("p", "profile-help", record.status === "confirmed" && !record.variant_label
      ? "Keep a résumé for each kind of role? Label this one to use it as a variant. Your profile facts stay as they are."
      : "Using it as a variant confirms it as a document to send, without copying anything into your profile. The words that mark each kind of role go under resume_variants in your profile file.");
    help.id = `${id}-help`;
    input.setAttribute("aria-describedby", help.id);
    const save = element("button", "secondary-button", record.variant_label ? "Save variant label" : "Use as a variant");
    save.type = "submit";
    const status = element("p", "form-status");
    status.id = `${id}-status`;
    status.setAttribute("aria-live", "polite");
    form.append(label, help, save, status);
    const settle = () => {
      input.removeAttribute("aria-invalid");
      input.setAttribute("aria-describedby", help.id);
    };
    input.addEventListener("input", () => {
      if (input.getAttribute("aria-invalid") !== "true") return;
      settle();
      status.textContent = "";
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const wanted = input.value.trim();
      if (!wanted && !record.variant_label) {
        status.textContent = "Type a label first, such as the kind of role this résumé is for.";
        // The input names the warning too, so moving focus to it reads the warning out.
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", `${help.id} ${status.id}`);
        input.focus();
        return;
      }
      settle();
      save.disabled = true;
      status.textContent = "Saving…";
      try {
        const saved = await api(`/api/v1/resumes/${encodeURIComponent(record.id)}/variant`, {
          method: "POST",
          body: JSON.stringify({ variant_label: wanted }),
        });
        state.profileStatus = null;
        announce(saved.variant_label ? `${record.original_name} is now your ${saved.variant_label} variant.` : `${record.original_name} is no longer a variant.`);
        await loadProfile();
      } catch (error) {
        if (!isAuthError(error)) status.textContent = error.message;
        save.disabled = false;
      }
    });
    return form;
  }

  function notificationSettingsSection(connections, preferences, events, applications, automation = null) {
    const section = element("section", "profile-card connection-section");
    section.appendChild(element("h3", "", "Monitoring controls"));
    section.appendChild(element("p", "profile-help", "Provider activity is previewed before tracker changes. Google/Microsoft use least-privilege OAuth when configured; notification delivery remains sandbox-suppressed by default."));
    const connectionList = element("div", "connection-list");
    (connections.items || []).forEach((connector) => {
      const row = element("div", "connection-row");
      row.appendChild(element("strong", "", `${connector.provider} · ${connector.status}`));
      if (connector.status === "connected") {
        const disconnect = element("button", "danger-button", "Disconnect");
        disconnect.type = "button";
        disconnect.addEventListener("click", async () => {
          await api(`/api/v1/connections/${encodeURIComponent(connector.id)}`, { method: "DELETE" });
          await loadProfile();
        });
        row.appendChild(disconnect);
      }
      connectionList.appendChild(row);
    });
    if (!(connections.items || []).some((connector) => connector.status === "connected")) {
      const connect = element("button", "secondary-button", "Connect sandbox mailbox/calendar");
      connect.type = "button";
      connect.addEventListener("click", async () => {
        await api("/api/v1/connections", { method: "POST", body: JSON.stringify({ provider: "sandbox" }) });
        await loadProfile();
      });
      connectionList.appendChild(connect);
    }
    ["google", "microsoft"].forEach((provider) => {
      if ((connections.items || []).some((connector) => connector.provider === provider && connector.status === "connected")) return;
      const connect = element("button", "secondary-button", `Connect ${provider}`);
      connect.type = "button";
      connect.addEventListener("click", async () => {
        try {
          const start = await api(`/api/v1/connections/oauth/${provider}/start`);
          window.location.assign(start.authorization_url);
        } catch (error) { showError(error.message); }
      });
      connectionList.appendChild(connect);
    });
    profileBlock(section, "Accounts").appendChild(connectionList);

    const form = element("form", "notification-form");
    const timezone = profileField(form, "Timezone", "timezone", preferences.timezone);
    const quietStart = profileField(form, "Quiet hours start", "quiet_start", preferences.quiet_start, { type: "time" });
    const quietEnd = profileField(form, "Quiet hours end", "quiet_end", preferences.quiet_end, { type: "time" });
    const digestLabel = element("label", "profile-field");
    digestLabel.appendChild(element("span", "", "Digest frequency"));
    const digest = document.createElement("select");
    ["immediate", "daily", "weekly", "off"].forEach((value) => {
      digest.appendChild(optionElement(value, value, value === preferences.digest_frequency));
    });
    digestLabel.appendChild(digest);
    form.appendChild(digestLabel);
    const toggles = element("div", "notification-toggles");
    const toggleControls = {};
    ["in_app", "email", "push", "sms", "voice"].forEach((channel) => {
      const label = element("label", "confirmation-row");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(preferences[`${channel}_enabled`]);
      label.append(checkbox, element("span", "", channel.replaceAll("_", " ")));
      toggles.appendChild(label);
      toggleControls[channel] = checkbox;
    });
    form.appendChild(toggles);
    const save = element("button", "secondary-button", "Save notification preferences");
    save.type = "submit";
    const statusLine = element("p", "form-status");
    form.append(save, statusLine);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        const updates = {
          timezone: timezone.value,
          quiet_start: quietStart.value,
          quiet_end: quietEnd.value,
          digest_frequency: digest.value,
          ...Object.fromEntries(Object.entries(toggleControls).map(([channel, control]) => [`${channel}_enabled`, control.checked])),
        };
        await api("/api/v1/notification-preferences", { method: "PUT", body: JSON.stringify({ updates }) });
        statusLine.textContent = "Preferences saved.";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    const alerts = profileBlock(section, "Notifications", "When and how the app tells you about something.");
    alerts.appendChild(form);

    // Saved at once through the automation switches, outside the form above,
    // so it needs no Save press and Enter never submits the form for it.
    const desktop = automationFeature(automation?.settings, "desktop_notifications");
    if (desktop) {
      const field = element("div", "settings-field automation-desktop-setting");
      const status = element("p", "form-status");
      status.setAttribute("aria-live", "polite");
      const [label, box] = automationCheckbox(desktop, "notification-desktop-popups", "Show automation notices as desktop pop-ups", status);
      const help = element("p", "profile-help", "Windows, macOS or Linux pop-ups for notices like 'Gmail needs reconnecting'. They never include email text or links. Quiet hours apply.");
      help.id = "notification-desktop-popups-help";
      box.setAttribute("aria-describedby", help.id);
      field.append(label, box, help, status);
      alerts.appendChild(field);
    }

    const phoneForm = element("form", "phone-form");
    const phone = document.createElement("input");
    phone.placeholder = "+15125550123";
    phone.setAttribute("aria-label", "Phone number in E.164 format");
    const requestCode = element("button", "secondary-button", preferences.phone_verified ? "Phone verified" : "Verify phone in sandbox");
    requestCode.type = "submit";
    requestCode.disabled = Boolean(preferences.phone_verified);
    const phoneStatus = element("p", "form-status");
    phoneForm.append(phone, requestCode, phoneStatus);
    phoneForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      requestCode.disabled = true;
      try {
        const challenge = await api("/api/v1/phone-verifications", { method: "POST", body: JSON.stringify({ phone_e164: phone.value }) });
        const entered = window.prompt(`Sandbox verification code: ${challenge.sandbox_code}. Enter it to confirm.`);
        if (!entered) return;
        await api("/api/v1/phone-verifications/confirm", { method: "POST", body: JSON.stringify({ challenge_id: challenge.id, code: entered }) });
        await loadProfile();
      } catch (error) {
        phoneStatus.textContent = error.message;
        requestCode.disabled = false;
      }
    });
    profileBlock(section, "Phone", "Verify a number before the app may text or call it.").appendChild(phoneForm);

    const pending = (events.items || []).filter((item) => item.status === "pending");
    if (pending.length) {
      const eventList = element("div", "monitored-event-list");
      pending.forEach((item) => {
        const card = element("article", "monitored-event");
        card.dataset.eventId = item.id;
        const decidedBy = item.payload.classified_by?.source === "jev" ? "Jev suggestion" : "keyword rules";
        card.appendChild(element("strong", "", `${item.event_type.replaceAll("_", " ")} · ${Math.round(item.confidence * 100)}% · ${decidedBy}`));
        card.appendChild(element("p", "", item.payload.subject || item.payload.body_preview));
        // Anyone can write any From line: a domain Gmail did not vouch for is never named as the sender.
        if (item.payload.sender_domain) {
          card.appendChild(element("p", "automation-meta", item.payload.sender_verified === false
            ? `Claims to be from ${item.payload.sender_domain} (sender not verified)`
            : `From ${item.payload.sender_domain}`));
        }
        const candidates = Array.isArray(item.payload.candidates) ? item.payload.candidates : [];
        const select = applicationPicker(applications.items || [], candidates, item.application_id || candidates[0] || "");
        select.setAttribute("aria-label", "Application this email is about");
        const controls = element("div", "preparation-actions");
        const confirm = element("button", "secondary-button", "Confirm tracker update");
        confirm.type = "button";
        const ignore = element("button", "danger-button", "Ignore");
        ignore.type = "button";
        const cardStatus = element("p", "form-status");
        cardStatus.setAttribute("aria-live", "polite");
        // One decision at a time. A 422 means the application picked cannot take what the email says
        // (pick another); a 409 means the email was decided already, or its application changed
        // since, so the card is settled and stays closed.
        const decideCard = async (decision, applicationId) => {
          confirm.disabled = true;
          ignore.disabled = true;
          cardStatus.textContent = "Saving…";
          try {
            await api(`/api/v1/monitored-events/${encodeURIComponent(item.id)}/decision`, { method: "POST", body: JSON.stringify({ decision, application_id: applicationId }) });
          } catch (error) {
            if (isAuthError(error)) {
              confirm.disabled = false;
              ignore.disabled = false;
              cardStatus.textContent = "";
              return;
            }
            cardStatus.textContent = error.message;
            if (error.status !== 409) {
              confirm.disabled = false;
              ignore.disabled = false;
              if (error.status === 422) select.focus();
            }
            return;
          }
          await loadProfile();
        };
        confirm.addEventListener("click", () => {
          if (!select.value) {
            cardStatus.textContent = "Choose the application this email is about first.";
            select.focus();
            return;
          }
          decideCard("confirm", select.value);
        });
        ignore.addEventListener("click", () => decideCard("ignore", null));
        controls.append(confirm, ignore);
        card.append(select, controls, cardStatus);
        eventList.appendChild(card);
      });
      section.appendChild(eventList);
    }
    return section;
  }

  function dossierScalar(value) {
    if (value === true) return "Yes";
    if (value === false) return "No";
    if (value === null || value === undefined || value === "") return "Not set";
    return String(value);
  }

  // Dossier values are stored JSON; show them as readable facts, not raw JSON.
  function dossierValue(value) {
    const isScalar = (entry) => entry === null || typeof entry !== "object";
    if (isScalar(value)) return element("p", "dossier-scalar", dossierScalar(value));
    if (Array.isArray(value)) {
      if (!value.length) return element("p", "dossier-scalar", "None");
      if (value.every(isScalar)) {
        const row = element("div", "chip-row dossier-chips");
        value.forEach((entry) => row.appendChild(chip(dossierScalar(entry))));
        return row;
      }
      const list = element("ul", "dossier-entries");
      value.forEach((entry) => {
        const li = element("li", "dossier-entry");
        if (isScalar(entry)) {
          li.textContent = dossierScalar(entry);
        } else {
          const scalars = Object.entries(entry).filter(([, inner]) => isScalar(inner) && inner !== "" && inner !== null);
          const headline = scalars.slice(0, 3).map(([, inner]) => dossierScalar(inner)).join(" · ");
          li.appendChild(element("strong", "", headline || "Entry"));
          const rest = scalars.slice(3);
          if (rest.length) li.appendChild(element("p", "dossier-meta", rest.map(([key, inner]) => `${humanizeKey(key)}: ${dossierScalar(inner)}`).join(" · ")));
          Object.entries(entry).filter(([, inner]) => !isScalar(inner)).forEach(([key, inner]) => {
            const more = element("details", "dossier-more");
            more.appendChild(element("summary", "", `${humanizeKey(key)}${Array.isArray(inner) ? ` (${inner.length})` : ""}`));
            more.appendChild(dossierValue(inner));
            li.appendChild(more);
          });
        }
        list.appendChild(li);
      });
      return list;
    }
    const facts = element("dl", "dossier-facts");
    Object.entries(value).forEach(([key, inner]) => {
      facts.appendChild(element("dt", "", humanizeKey(key)));
      const dd = element("dd");
      if (isScalar(inner)) dd.textContent = dossierScalar(inner);
      else dd.appendChild(dossierValue(inner));
      facts.appendChild(dd);
    });
    return facts;
  }

  function dossierSection(payload) {
    const section = element("section", "profile-card dossier-section");
    section.appendChild(element("h3", "", "Evidence dossier and consent"));
    section.appendChild(element("p", "profile-help", "Nothing here is employer-visible until you preview and create a named, expiring share."));
    const settings = element("form", "notification-form");
    const pausedLabel = element("label", "channel-toggle");
    const paused = document.createElement("input"); paused.type = "checkbox"; paused.checked = payload.settings.paused;
    pausedLabel.append(paused, document.createTextNode(" Pause memory and sharing"));
    const retention = profileField(settings, "Retention days", "retention_days", payload.settings.retention_days, {type: "number"});
    const save = element("button", "secondary-button", "Save dossier settings"); save.type = "submit";
    settings.append(pausedLabel, save);
    settings.addEventListener("submit", async (event) => {
      event.preventDefault();
      await api("/api/v1/dossier/settings", {method: "PUT", body: JSON.stringify({paused: paused.checked, retention_days: Number(retention.value)})});
      await loadProfile();
    });
    profileBlock(section, "Memory").appendChild(settings);
    const itemForm = element("form", "notification-form");
    const kindLabel = element("label", "profile-field"); kindLabel.appendChild(element("span", "", "Item classification"));
    const kind = document.createElement("select"); ["user_opinion", "deterministic_analysis", "ai_suggestion"].forEach((value) => { kind.appendChild(optionElement(value, humanizeKey(value))); }); kindLabel.appendChild(kind); itemForm.appendChild(kindLabel);
    const fieldPath = profileField(itemForm, "Field name", "field_path", "");
    const value = profileField(itemForm, "Value", "value", "", {multiline: true});
    const add = element("button", "secondary-button", "Add classified item"); add.type = "submit"; itemForm.appendChild(add);
    itemForm.addEventListener("submit", async (event) => { event.preventDefault(); await api("/api/v1/dossier/items", {method: "POST", body: JSON.stringify({item_type: kind.value, field_path: fieldPath.value, value: value.value, evidence: []})}); await loadProfile(); });
    const itemsBlock = profileBlock(section, "Items", "Tick the items to share below.");
    itemsBlock.appendChild(itemForm);
    const items = element("div", "dossier-list");
    (payload.items || []).forEach((item) => {
      const row = element("div", "dossier-item");
      const check = document.createElement("input"); check.type = "checkbox"; check.value = item.id; check.dataset.dossierItem = "true";
      check.id = `dossier-item-${item.id}`;
      const main = element("div", "dossier-item-main");
      const title = element("label", "dossier-item-title");
      title.htmlFor = check.id;
      title.append(element("strong", "", humanizeKey(item.field_path)), chip(humanizeKey(item.item_type)));
      main.append(title, dossierValue(item.value));
      const remove = element("button", "danger-button", "Delete"); remove.type = "button";
      remove.setAttribute("aria-label", `Delete ${humanizeKey(item.field_path)}`);
      remove.addEventListener("click", async () => { if (window.confirm(`Delete ${item.field_path} and revoke shares containing it?`)) { await api(`/api/v1/dossier/items/${encodeURIComponent(item.id)}`, {method: "DELETE"}); await loadProfile(); } });
      row.append(check, main, remove);
      items.appendChild(row);
    });
    itemsBlock.appendChild(items);
    const shareForm = element("form", "notification-form");
    const recipient = profileField(shareForm, "Share recipient (exact organization name)", "recipient", "");
    const days = profileField(shareForm, "Expires in days", "expires", 30, {type: "number"});
    const share = element("button", "primary-button", "Preview and create share"); share.type = "submit";
    const shareStatus = element("p", "form-status"); shareStatus.setAttribute("aria-live", "polite");
    shareForm.append(share, shareStatus);
    shareForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const item_ids = [...items.querySelectorAll("input:checked")].map((input) => input.value);
      try {
        const preview = await api("/api/v1/dossier/shares/preview", {method: "POST", body: JSON.stringify({item_ids})});
        if (!window.confirm(`Share ${preview.items.length} previewed dossier item(s) with ${recipient.value}?`)) return;
        const result = await api("/api/v1/dossier/shares", {method: "POST", body: JSON.stringify({recipient: recipient.value, item_ids, expires_in_days: Number(days.value)})});
        shareStatus.textContent = `Share created. Copy this token now: ${result.share_token}`;
        await loadProfile();
      } catch (error) { shareStatus.textContent = error.message; }
    });
    const sharing = profileBlock(section, "Sharing", "A named, expiring share of the ticked items. You preview it first.");
    sharing.appendChild(shareForm);
    (payload.shares || []).forEach((grant) => {
      const row = element("div", "connection-row");
      row.appendChild(element("span", "", `${grant.recipient} · ${grant.status} · expires ${formatDate(grant.expires_at)} · ${plural(grant.access_log.length, "log event", "log events")}`));
      if (grant.status === "active") {
        const revoke = element("button", "danger-button", "Revoke"); revoke.type = "button";
        revoke.addEventListener("click", async () => { await api(`/api/v1/dossier/shares/${encodeURIComponent(grant.id)}`, {method: "DELETE"}); await loadProfile(); });
        row.appendChild(revoke);
      }
      sharing.appendChild(row);
    });
    const actions = element("div", "tracker-exports profile-foot");
    const exportLink = element("a", "secondary-button", "Export dossier"); exportLink.href = "/api/v1/dossier/export";
    const deleteAll = element("button", "danger-button", "Delete dossier"); deleteAll.type = "button";
    deleteAll.addEventListener("click", async () => { if (window.confirm("Delete every dossier item and revoke every share?")) { await api("/api/v1/dossier", {method: "DELETE"}); await loadProfile(); } });
    actions.append(exportLink, deleteAll); section.appendChild(actions);
    return section;
  }

  function accountSection() {
    const section = element("section", "profile-card account-section");
    section.appendChild(element("h3", "", "Export or permanently delete"));
    section.appendChild(element("p", "profile-help", "Export downloads everything the app keeps for your account. Delete removes your private data and files for good; it cannot be undone."));
    const actions = element("div", "tracker-exports profile-actions-row");
    const exportLink = element("a", "secondary-button", "Export full account"); exportLink.href = "/api/v1/account/export";
    const remove = element("button", "danger-button", "Delete account"); remove.type = "button";
    remove.addEventListener("click", async () => {
      if (window.prompt("Type DELETE to permanently remove private account data and files.") !== "DELETE") return;
      await api("/api/v1/account", {method: "DELETE", headers: {"X-Confirm-Delete": "DELETE"}});
      await api("/api/v1/session", {method: "DELETE"}); showAuth("Account deleted.");
    });
    actions.append(exportLink, remove); section.appendChild(actions); return section;
  }

  function extensionSection(devicesPayload = {items: []}) {
    const section = element("section", "profile-card extension-section");
    section.appendChild(element("h3", "", "Pair or revoke Chrome devices"));
    section.appendChild(element("p", "profile-help", "Pairing codes expire after 10 minutes and work once. The extension receives a revocable Apply-only credential, never your account token."));
    const pairingActions = element("div", "tracker-exports profile-actions-row");
    const create = element("button", "secondary-button", "Create one-time pairing code");
    create.type = "button";
    const pairingStatus = element("p", "form-status");
    pairingStatus.setAttribute("aria-live", "polite");
    create.addEventListener("click", async () => {
      create.disabled = true;
      try {
        const result = await api("/api/v1/extension/pairings", {method: "POST", body: JSON.stringify({})});
        pairingStatus.textContent = `Pairing code: ${result.code} · expires ${formatDate(result.expires_at)}. Paste it into the extension side panel now.`;
      } catch (error) {
        pairingStatus.textContent = error.message;
      } finally {
        create.disabled = false;
      }
    });
    pairingActions.appendChild(create);
    section.append(pairingActions, pairingStatus);

    const devices = devicesPayload.items || [];
    if (!devices.length) {
      section.appendChild(element("p", "empty-inline", "No Chrome devices paired."));
    } else {
      const list = element("div", "connection-list");
      devices.forEach((device) => {
        const row = element("div", "connection-row");
        const stateLabel = device.revoked_at ? `revoked ${formatDate(device.revoked_at)}` : `last used ${device.last_used_at ? formatDate(device.last_used_at) : "never"}`;
        row.appendChild(element("span", "", `${device.device_name} · ${stateLabel}`));
        if (!device.revoked_at) {
          const revoke = element("button", "danger-button", "Revoke");
          revoke.type = "button";
          revoke.addEventListener("click", async () => {
            await api(`/api/v1/extension/devices/${encodeURIComponent(device.id)}`, {method: "DELETE"});
            await loadProfile();
          });
          row.appendChild(revoke);
        }
        list.appendChild(row);
      });
      section.appendChild(list);
    }
    return section;
  }

  function renderProfile(profilePayload, resumesPayload = null, connections = { items: [] }, preferences = {}, events = { items: [] }, applications = { items: [] }, dossier = {settings: {}, items: [], shares: []}, extensionDevices = {items: []}, automation = null, automationActions = null) {
    els.results.replaceChildren();
    els.results.removeAttribute("role");
    els.resultCount.textContent = "Your career profile";
    els.pageStatus.textContent = `${profilePayload.completeness.completed} of ${profilePayload.completeness.total} onboarding fields complete`;
    // The phone bottom bar holds the eight destinations; Sign out lives here.
    const signOutRow = element("div", "mobile-signout");
    const signOut = element("button", "secondary-button", "Sign out");
    signOut.type = "button";
    signOut.id = "profile-signout";
    signOut.addEventListener("click", () => els.logout.click());
    signOutRow.appendChild(signOut);
    els.results.appendChild(signOutRow);
    els.results.appendChild(tagSection(renderProfileEditor(profilePayload), "profile", "Career profile"));
    els.results.appendChild(tagSection(automationSection(automation, automationActions, applications), "automation", "Automation"));

    const resumeSection = element("section", "profile-card resume-section");
    resumeSection.appendChild(element("h3", "", "Resume versions"));
    resumeSection.appendChild(element("p", "profile-help", "PDF and DOCX only, up to 5 MB. Parsed suggestions remain drafts until you confirm each fact."));
    resumeSection.appendChild(element("p", "profile-help resume-variant-summary", resumeVariantSummary(resumesPayload)));
    const upload = element("form", "upload-form");
    const file = document.createElement("input");
    file.type = "file";
    file.name = "resume";
    file.accept = ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    file.required = true;
    const fileLabel = element("label", "file-input-label", "Resume PDF or DOCX");
    fileLabel.appendChild(file);
    const submit = element("button", "secondary-button", "Upload and extract");
    submit.type = "submit";
    const uploadStatus = element("p", "form-status");
    uploadStatus.setAttribute("aria-live", "polite");
    upload.append(fileLabel, submit, uploadStatus);
    upload.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!file.files.length) return;
      submit.disabled = true;
      uploadStatus.textContent = "Scanning and extracting…";
      const body = new FormData();
      body.append("resume", file.files[0]);
      try {
        await api("/api/v1/resumes", { method: "POST", body });
        await loadProfile();
      } catch (error) {
        uploadStatus.textContent = error.message;
        submit.disabled = false;
      }
    });
    resumeSection.appendChild(upload);
    const resumes = resumesPayload?.items || [];
    if (!resumes.length) {
      resumeSection.appendChild(element("p", "empty-inline", "No resume versions uploaded yet."));
    } else {
      const list = element("div", "resume-list");
      resumes.forEach((record) => list.appendChild(createResumeCard(record)));
      resumeSection.appendChild(list);
    }
    els.results.appendChild(tagSection(resumeSection, "resumes", "Resumes"));
    els.results.appendChild(tagSection(notificationSettingsSection(connections, preferences, events, applications, automation), "alerts", "Connections and alerts"));
    els.results.appendChild(tagSection(dossierSection(dossier), "dossier", "Evidence dossier"));
    els.results.appendChild(tagSection(extensionSection(extensionDevices), "extension", "Browser extension"));
    els.results.appendChild(tagSection(accountSection(), "account", "Account"));
    sectionSubnav();
    els.results.setAttribute("aria-busy", "false");
  }

  async function loadProfile() {
    await runViewLoad({ views: ["profile"], placeholder: loadingLine("Loading your private profile…") }, async ({ isCurrent }) => {
      const automationRead = startAutomationRead();
      const [profile, resumes, connections, preferences, events, applications, dossier, extensionDevices, automation, ...automationLists] = await Promise.all([
        api("/api/v1/profile"),
        api("/api/v1/resumes"),
        api("/api/v1/connections"),
        api("/api/v1/notification-preferences"),
        api("/api/v1/monitored-events"),
        api("/api/v1/applications"),
        api("/api/v1/dossier"),
        api("/api/v1/extension/devices"),
        // A failure here shows in the Automation section instead of blanking the page.
        api("/api/v1/automation").catch((error) => ({ error })),
        ...automationListRequests().map((request) => request.catch((error) => ({ error }))),
      ]);
      if (!isCurrent()) return;
      let overview = automation;
      if (automation?.settings && !applyAutomationRead(automationRead, automation)) {
        // A pause or switch saved while this page loaded is newer than this
        // answer: draw the section from what is on screen, not from this.
        overview = {
          ...automation,
          settings: state.automation?.settings || automation.settings,
          health: state.automation?.health || automation.health,
        };
      }
      const automationActions = Object.fromEntries(AUTOMATION_LIST_KEYS.map((key, index) => [key, automationLists[index]]));
      renderProfile(profile, resumes, connections, preferences, events, applications, dossier, extensionDevices, overview, automationActions);
    });
  }

  // What this file does as it loads, run once by app.js in load order.
  function installProfile() {
    registerViewHandlers("profile", { load: loadProfile });
  }

  Object.assign(App, {
    installProfile,
  });
})();
