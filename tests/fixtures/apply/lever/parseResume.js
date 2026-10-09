/* A stand-in for Lever's /js/parseResume.js, for FakeLever (tests/apply_fake_ats.py). It is written from the behaviour
   docs/phase5-lever-handoff-spec.md section 3 item 8 records from public boards on 2026-10-04, not copied from Lever's
   script and not shaped around any adapter:

   - The résumé file input posts the file to POST /parseResume on the page's own origin as soon as it changes. The request
     has two parts, `resume` (under a file name the page sanitizes: Lever's own rule, `lever.posted_file_name`) and `accountId` (the page's own hidden field). A file
     over the limit is not sent and the oversize message shows.
   - On success the reply fills org, phone, name, email, location, the urls[...] fields and the residentialLocation[...]
     fields it has a value for, and the hidden resumeStorageId.
   - A field counts as the user's only if it was changed or pasted into AND holds a value when the reply arrives, and
     only the parser's own field list is protected at all. selectedLocation is not in that list, so every reply
     rewrites it. A field the user emptied again is the parser's to fill.
   - Working, success, failure and oversize are four indicators, one shown at a time.
   - The reply is applied only once `parseDelayMs` (window.__fakeLever) has passed since the file was sent, as on a live board
     whose reader takes a while: "working" stays shown and the browser is free meanwhile. The fake answers at once and the page
     keeps the wait, so nothing blocks the browser. A reply that arrives later than that (parse_mode "held") is applied when it comes.

   What the reply looks like is not known (the spec marks it unseen): tests/fixtures/apply/lever/parse_resume_reply.json
   is invented. The `urls[...]` names the parser fills are its own list: a form that calls the field `urls[Github]`
   gets nothing from a parser that fills `urls[GitHub]`, as on the demonstration board. */
(function () {
  "use strict";
  var config = window.__fakeLever || {};
  var form = document.getElementById("application-form");
  if (!form) return;
  var input = form.querySelector('input[name="resume"]');
  if (!input) return;

  var PARSED_FIELDS = ["org", "phone", "name", "email", "location", "urls[LinkedIn]", "urls[Twitter]", "urls[Quora]", "urls[GitHub]", "urls[Other]"];
  var RESIDENTIAL = "residentialLocation[";
  var limit = typeof config.maxUploadBytes === "number" ? config.maxUploadBytes : 100 * 1024 * 1024;
  var touched = {};
  var sequence = 0;
  var delay = typeof config.parseDelayMs === "number" ? config.parseDelayMs : 0;

  function control(name) {
    return form.querySelector('[name="' + name + '"]');
  }

  function parsedName(name) {
    return PARSED_FIELDS.indexOf(name) >= 0 || name.indexOf(RESIDENTIAL) === 0;
  }

  function mark(event) {
    var target = event.target;
    if (target && target.name && parsedName(target.name)) touched[target.name] = true;
  }
  form.addEventListener("change", mark, true);
  form.addEventListener("paste", mark, true);

  function indicator(state) {
    ["working", "success", "failure", "oversize"].forEach(function (kind) {
      var element = document.querySelector(".resume-upload-" + kind);
      if (element) element.style.display = kind === state ? "block" : "none";
    });
  }

  function fill(name, value) {
    var element = control(name);
    if (!element || typeof value !== "string" || !value) return;
    if (touched[name] && element.value) return;
    element.value = value;
  }

  function apply(profile) {
    PARSED_FIELDS.forEach(function (name) {
      if (name === "location") return;
      if (name.indexOf("urls[") === 0) fill(name, (profile.urls || {})[name.slice(5, -1)]);
      else fill(name, profile[name]);
    });
    var place = profile.location && typeof profile.location === "object" ? profile.location : null;
    fill("location", place ? place.name : "");
    var selected = control("selectedLocation");
    if (selected) selected.value = place ? JSON.stringify(place) : "";
    var home = profile.residentialLocation || {};
    Object.keys(home).forEach(function (part) {
      fill(RESIDENTIAL + part + "]", home[part]);
    });
    var storage = control("resumeStorageId");
    if (storage && profile.resumeStorageId) storage.value = profile.resumeStorageId;
  }

  // Lever's own sanitizeFilename (read from /js/parseResume.js on 2026-10-08): it keeps parentheses, apostrophes, commas and letters of any script.
  function safeName(name) {
    return String(name).replace(/[<>:"/\\|?*\s]/g, "_").replace(/^\.+|\.+$/g, "").replace(/^_+|_+$/g, "").replace(/_+/g, "_").replace(/^$/, "untitled");
  }

  input.addEventListener("change", function () {
    var file = input.files && input.files[0];
    var label = document.querySelector(".visible-resume-upload .filename");
    if (label) label.textContent = file ? file.name : "";
    if (!file) {
      indicator("");
      return;
    }
    if (file.size > limit) {
      indicator("oversize");
      return;
    }
    var mine = ++sequence;
    var account = control("accountId");
    var data = new FormData();
    data.append("resume", file, safeName(file.name));
    data.append("accountId", account ? account.value : "");
    indicator("working");
    var sent = Date.now();
    var answered = fetch("/parseResume", { method: "POST", body: data, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("parse " + response.status);
        return response.json();
      });
    // The reader takes `delay` ms in all, success or failure; a reply that came later than that is applied now.
    var wait = function () {
      return new Promise(function (resolve) { setTimeout(resolve, Math.max(0, delay - (Date.now() - sent))); });
    };
    answered
      .then(function (profile) { return wait().then(function () { return profile; }); },
            function (error) { return wait().then(function () { throw error; }); })
      .then(function (profile) {
        if (mine !== sequence) return;
        apply(profile || {});
        indicator("success");
      })
      .catch(function () {
        if (mine === sequence) indicator("failure");
      });
  });
})();
