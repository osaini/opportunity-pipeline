/* A stand-in for https://js.hcaptcha.com/1/api.js, for FakeLever (tests/apply_fake_ats.py). Nothing here is hCaptcha's
   code and no real challenge is shown. It does what docs/phase5-lever-handoff-spec.md section 3 item 10 needs of the
   widget: the page asks for it with ?onload=<name>&render=explicit, renders an invisible widget into #h-captcha, and
   hcaptcha.execute() ends in the widget's callback with a token.

   Whether a challenge is needed is asked of https://api.hcaptcha.com/checksiteconfig at execute() time, so a test can
   switch it on and off while the page stays open. With no challenge the token arrives a moment later. With one, a visible
   frame (title "Main content of the hCaptcha challenge", the way the real widget titles its challenge) is drawn from
   https://newassets.hcaptcha.com/captcha/v1/fake/hcaptcha.html, and the token arrives only when something inside the
   frame is pressed (its "solve" button). The invisible checkbox frame the real widget keeps is drawn too, hidden.

   window.__fakeLever.challengeDuringFill = [start, end] (seconds after the widget renders) draws the same frame with no
   press, for a challenge that shows while a form is being filled; its "solve" only removes it, and it asks for no token. */
(function () {
  "use strict";
  var config = window.__fakeLever || {};
  var FRAME = "https://newassets.hcaptcha.com/captcha/v1/fake/hcaptcha.html";
  var FRAME_ORIGIN = "https://newassets.hcaptcha.com";
  var widgets = [];
  var script = document.currentScript;
  var query = new URLSearchParams(((script && script.src) || "").split("?")[1] || "");
  var onload = query.get("onload");
  var minted = 0;

  function holderOf(target) {
    return typeof target === "string" ? document.getElementById(target) : target;
  }

  function drawChallenge(widget, passive) {
    if (widget.overlay) {
      if (!passive) widget.passive = false;
      return;
    }
    var overlay = document.createElement("div");
    overlay.setAttribute("data-fake-hcaptcha", "challenge");
    overlay.style.cssText = "position:fixed;left:50%;top:80px;transform:translateX(-50%);width:400px;height:300px;z-index:2000000000;background:#fff;border:1px solid #d7d7d7;box-shadow:0 0 8px rgba(0,0,0,.4)";
    var frame = document.createElement("iframe");
    frame.title = "Main content of the hCaptcha challenge";
    frame.src = FRAME + "#frame=challenge&passive=" + (passive ? "1" : "0");
    frame.style.cssText = "width:100%;height:100%;border:0";
    overlay.appendChild(frame);
    document.body.appendChild(overlay);
    widget.overlay = overlay;
    widget.passive = !!passive;
  }

  function removeChallenge(widget) {
    if (widget.overlay) widget.overlay.remove();
    widget.overlay = null;
  }

  function deliver(widget) {
    minted += 1;
    widget.response = "P1_fake-hcaptcha-token-" + minted;
    if (typeof widget.params.callback === "function") widget.params.callback(widget.response);
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== FRAME_ORIGIN || !event.data || !event.data.fakeHcaptcha) return;
    widgets.forEach(function (widget) {
      if (!widget.overlay || widget.overlay.querySelector("iframe").contentWindow !== event.source) return;
      if (event.data.fakeHcaptcha === "solve") {
        var passive = widget.passive;
        removeChallenge(widget);
        if (!passive) deliver(widget);
      } else if (event.data.fakeHcaptcha === "close") {
        removeChallenge(widget);
        if (typeof widget.params["close-callback"] === "function") widget.params["close-callback"]();
      }
    });
  });

  window.hcaptcha = {
    render: function (target, params) {
      var holder = holderOf(target);
      var widget = { params: params || {}, response: "", overlay: null, passive: false };
      var checkbox = document.createElement("iframe");
      checkbox.title = "Widget containing checkbox for hCaptcha security challenge for Submit";
      checkbox.src = FRAME + "#frame=checkbox-invisible";
      checkbox.style.display = "none";
      if (holder) holder.appendChild(checkbox);
      widgets.push(widget);
      var during = config.challengeDuringFill;
      if (during && during.length === 2) {
        setTimeout(function () { drawChallenge(widget, true); }, during[0] * 1000);
        setTimeout(function () { if (widget.passive) removeChallenge(widget); }, during[1] * 1000);
      }
      return widgets.length - 1;
    },
    execute: function (id) {
      var widget = widgets[id || 0];
      if (!widget) return;
      var url = "https://api.hcaptcha.com/checksiteconfig?v=fake&host=" + encodeURIComponent(location.hostname) + "&sitekey=" + encodeURIComponent(widget.params.sitekey || "");
      fetch(url)
        .then(function (reply) { return reply.json(); })
        .then(function (found) {
          if (found && found.challenge) drawChallenge(widget, false);
          else setTimeout(function () { deliver(widget); }, 20);
        })
        .catch(function () {
          if (typeof widget.params["error-callback"] === "function") widget.params["error-callback"]("network-error");
        });
    },
    reset: function (id) {
      var widget = widgets[id || 0];
      if (widget) widget.response = "";
    },
    getResponse: function (id) {
      var widget = widgets[id || 0];
      return widget ? widget.response : "";
    },
    remove: function (id) {
      var widget = widgets[id || 0];
      if (widget) removeChallenge(widget);
    },
    // For tests: draw or remove the challenge frame now, as if hCaptcha had decided to show one with nothing pressed.
    __fakeShow: function () { widgets.forEach(function (widget) { drawChallenge(widget, true); }); },
    __fakeHide: function () { widgets.forEach(removeChallenge); },
  };

  // The page's onload function is defined by a deferred script, and this script is async: it may run before or after that one. Try now, when
  // parsing ends and when the page has loaded, and call the function once.
  var booted = false;
  function boot() {
    var callback = onload && window[onload];
    if (booted || typeof callback !== "function") return;
    booted = true;
    callback();
  }
  document.addEventListener("DOMContentLoaded", boot);
  window.addEventListener("load", boot);
  if (document.readyState !== "loading") boot();
})();
