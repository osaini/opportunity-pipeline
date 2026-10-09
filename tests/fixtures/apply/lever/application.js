/* A stand-in for the rest of what a Lever application page does by script, for FakeLever (tests/apply_fake_ats.py). It
   is written from docs/phase5-lever-handoff-spec.md section 3 (items 6, 9, 10, 11, 13), not copied from Lever's scripts
   and not shaped around any adapter.

   - Location (item 9): typing starts GET /searchLocations?text=... after a pause (500 ms by default, up to 100
     characters), and only from key events: an `input` event alone (what a script's fill() sends) opens the dropdown
     container but starts no search. Choosing an option writes its JSON into selectedLocation. Leaving the field while the
     container is open and nothing was chosen since the last input empties both fields; with the container closed the
     blur does nothing.
   - Submit (item 10): the visible #btn-submit (type=button) runs hcaptcha.execute(), and when the token arrives the page
     clicks the hidden #hcaptchaSubmitBtn (type=submit) so the browser's own required checks run first. Enter in a text
     field is routed the same way. The click handlers are attached only inside hCaptcha's onload, so if the hCaptcha
     script does not load, pressing Submit does nothing at all.
   - Required checkboxes (item 11): every required checkbox in every .required-field is one set, and once any one is
     ticked `required` comes off all of them, and goes back when none is.
   - EEO (item 6): answering the disability question, the decline included, makes the signature and its date required.
   - The page fills its hidden timezone field itself.
   - Clicks: window.__leverClicks counts clicks on the controls an agent must never press, except the page's own click on
     the hidden submit: submit (#btn-submit), hiddenSubmit (#hcaptchaSubmitBtn), cookie (the banner's buttons) and
     challenge (inside the hCaptcha challenge frame). A student's own press of Submit is counted too; a test says whose it was. */
(function () {
  "use strict";
  var config = window.__fakeLever || {};
  var form = document.getElementById("application-form");
  if (!form) return;

  var counts = (window.__leverClicks = { submit: 0, hiddenSubmit: 0, cookie: 0, challenge: 0 });
  var pageIsClicking = false;

  document.addEventListener("click", function (event) {
    if (pageIsClicking) return;
    var target = event.target && event.target.closest ? event.target : null;
    if (!target) return;
    if (target.closest("#btn-submit")) counts.submit += 1;
    else if (target.closest("#hcaptchaSubmitBtn")) counts.hiddenSubmit += 1;
    else if (target.closest(".cc-window")) counts.cookie += 1;
  }, true);

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (data && data.fakeHcaptcha === "click" && /(^|\.)hcaptcha\.com$/.test(new URL(event.origin).hostname)) counts.challenge += 1;
  });

  function control(name) {
    return form.querySelector('[name="' + name + '"]');
  }

  // The hidden timezone field.
  var zone = control("timezone");
  if (zone && !zone.value) {
    try { zone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (error) { /* the page leaves it empty */ }
  }

  // Item 11: one required checkbox set for the whole page.
  var requiredBoxes = Array.prototype.slice.call(document.querySelectorAll(".required-field input[type=checkbox][required]"));
  function relaxRequiredBoxes() {
    var anyTicked = requiredBoxes.some(function (box) { return box.checked; });
    requiredBoxes.forEach(function (box) { box.required = !anyTicked; });
  }
  requiredBoxes.forEach(function (box) { box.addEventListener("change", relaxRequiredBoxes); });

  // Item 6: any disability answer makes the signature and its date required.
  var disability = control("eeo[disability]");
  if (disability) {
    disability.addEventListener("change", function () {
      ["eeo[disabilitySignature]", "eeo[disabilitySignatureDate]"].forEach(function (name) {
        var element = control(name);
        if (element) element.required = disability.value !== "";
      });
    });
  }

  // Item 9: the location typeahead.
  var place = control("location");
  var selected = control("selectedLocation");
  var box = form.querySelector(".dropdown-container");
  if (place && selected && box) {
    var results = box.querySelector(".dropdown-results");
    var chosen = false;
    var timer = null;
    var asked = 0;
    var active = -1;
    var pause = typeof config.searchDebounceMs === "number" ? config.searchDebounceMs : 500;

    var open = function (state) {
      box.classList.remove("loading", "empty");
      if (state) box.classList.add(state);
      box.classList.add("open");
    };
    var close = function () {
      box.classList.remove("open", "loading", "empty");
      active = -1;
    };
    var options = function () { return Array.prototype.slice.call(results.querySelectorAll("[data-option]")); };
    var highlight = function (index) {
      var list = options();
      list.forEach(function (item, at) { item.classList.toggle("active", at === index); });
      active = index;
    };
    var choose = function (item) {
      place.value = item.getAttribute("data-name") || item.textContent;
      selected.value = item.getAttribute("data-json") || "";
      chosen = true;
      place.dispatchEvent(new Event("change", { bubbles: true }));   // a chosen place is the user's own, as parseResume.js counts it
      close();
    };
    var search = function (text) {
      var mine = ++asked;
      open("loading");
      fetch("/searchLocations?text=" + encodeURIComponent(text.slice(0, 100)), { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : []; })
        .catch(function () { return []; })
        .then(function (found) {
          if (mine !== asked || !box.classList.contains("open")) return;
          results.textContent = "";
          (Array.isArray(found) ? found : []).forEach(function (option) {
            var item = document.createElement("div");
            item.setAttribute("data-option", "");
            item.setAttribute("role", "option");
            item.className = "dropdown-option";
            item.setAttribute("data-name", option.name || "");
            item.setAttribute("data-json", JSON.stringify(option));
            item.textContent = option.name || "";
            item.addEventListener("mousedown", function (event) {
              event.preventDefault();
              choose(item);
            });
            results.appendChild(item);
          });
          open(results.children.length ? "" : "empty");
          active = -1;
        });
    };

    place.addEventListener("input", function () {
      chosen = false;
      open("");
    });
    place.addEventListener("keyup", function (event) {
      if (["Enter", "Tab", "Shift", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].indexOf(event.key) >= 0) return;
      clearTimeout(timer);
      var text = place.value.trim();
      if (!text) {
        results.textContent = "";
        open("");
        return;
      }
      timer = setTimeout(function () { search(text); }, pause);
    });
    place.addEventListener("keydown", function (event) {
      var list = options();
      if (event.key === "ArrowDown" && list.length) { event.preventDefault(); highlight(Math.min(active + 1, list.length - 1)); }
      else if (event.key === "ArrowUp" && list.length) { event.preventDefault(); highlight(Math.max(active - 1, 0)); }
      else if (event.key === "Enter" && active >= 0 && list[active]) { event.preventDefault(); event.stopPropagation(); choose(list[active]); }
      else if (event.key === "Escape") close();
    });
    place.addEventListener("blur", function () {
      clearTimeout(timer);
      asked += 1;
      if (!box.classList.contains("open")) return;
      if (!chosen) {
        place.value = "";
        selected.value = "";
      }
      close();
    });
  }

  // Item 10: Submit through hCaptcha. Nothing below is attached until the hCaptcha script has loaded.
  var button = document.getElementById("btn-submit");
  var hidden = document.getElementById("hcaptchaSubmitBtn");
  var response = document.getElementById("hcaptchaResponseInput");
  var holder = document.getElementById("h-captcha");
  window.hcaptchaOnLoad = function () {
    if (!window.hcaptcha || !button || !hidden || !holder) return;
    var widget = window.hcaptcha.render(holder, {
      sitekey: holder.getAttribute("data-sitekey") || "",
      size: "invisible",
      callback: function (token) {
        if (response) response.value = token;
        pageIsClicking = true;
        try { hidden.click(); } finally { pageIsClicking = false; }
        window.hcaptcha.reset(widget);
      },
    });
    var press = function () { window.hcaptcha.execute(widget); };
    button.addEventListener("click", function (event) {
      event.preventDefault();
      press();
    });
    form.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || event.defaultPrevented) return;
      var target = event.target;
      if (!target || target.tagName !== "INPUT" || ["text", "email", "tel", "url", "number"].indexOf(target.type) < 0) return;
      event.preventDefault();
      press();
    });
  };
})();
