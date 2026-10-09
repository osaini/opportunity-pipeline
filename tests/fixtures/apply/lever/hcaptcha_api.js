/* A stand-in for https://js.hcaptcha.com/1/api.js, for FakeLever (tests/apply_fake_ats.py). Nothing here is hCaptcha's
   code and no real challenge is shown. It does what docs/phase5-lever-handoff-spec.md section 3 item 10 needs of the
   widget: the page asks for it with ?onload=<name>&render=explicit, renders an invisible widget into #h-captcha, and
   hcaptcha.execute() ends in the widget's callback with a token.

   What execute() sends is what the real widget sends, from the real widget's places: the host page asks
   https://api.hcaptcha.com/checksiteconfig (a GET; whether a challenge is needed is answered there at execute() time, so a
   test can switch it on and off while the page stays open), then the hidden checkbox frame POSTs a form body to
   https://api.hcaptcha.com/getcaptcha/{sitekey} and reports what came back. With no challenge the token arrives a moment
   later. With one, a visible frame (title "Main content of the hCaptcha challenge", the way the real widget titles its
   challenge) is drawn from https://newassets.hcaptcha.com/captcha/v1/fake/hcaptcha.html, and the token arrives only when
   something inside the frame is pressed (its "solve" button), which POSTs /checkcaptcha/{sitekey}/... first. When any of
   the three requests is refused or fails, the error-callback runs and no token arrives, so Submit does nothing, as it would
   live. The widget also writes its answer into two hidden boxes named h-captcha-response and g-recaptcha-response inside
   its container, as the real one does (the page's own h-captcha-response input is a second control of that name); both are
   posted with the form.

   As the page loads, render() also POSTs /checksiteconfig to api.hcaptcha.com, api2.hcaptcha.com and hcaptcha.com, as the real widget does
   (the load recording, spec 11 Q3); the answers are not read.

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
  var asked = 0;
  var pending = {};

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
    frame.src = FRAME + "#frame=challenge&passive=" + (passive ? "1" : "0") + "&sitekey=" + encodeURIComponent(widget.params.sitekey || "");
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
    widget.fields.forEach(function (box) { box.value = widget.response; });
    if (typeof widget.params.callback === "function") widget.params.callback(widget.response);
  }

  function fail(widget) {
    if (typeof widget.params["error-callback"] === "function") widget.params["error-callback"]("network-error");
  }

  // The hidden checkbox frame is the widget's own: it tells the page when it has loaded and carries out getcaptcha for it.
  function ask(widget) {
    return new Promise(function (resolve, reject) {
      var send = function () {
        asked += 1;
        pending[asked] = { resolve: resolve, reject: reject };
        widget.checkbox.contentWindow.postMessage({ fakeHcaptchaTo: "frame", kind: "getcaptcha", seq: asked }, FRAME_ORIGIN);
      };
      if (widget.ready) send();
      else widget.waiting.push(send);
    });
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== FRAME_ORIGIN || !event.data || !event.data.fakeHcaptcha) return;
    widgets.forEach(function (widget) {
      if (widget.checkbox && widget.checkbox.contentWindow === event.source) {
        if (event.data.fakeHcaptcha === "ready") {
          widget.ready = true;
          widget.waiting.splice(0).forEach(function (send) { send(); });
        } else if (event.data.fakeHcaptcha === "captcha" && pending[event.data.seq]) {
          var call = pending[event.data.seq];
          delete pending[event.data.seq];
          if (event.data.ok) call.resolve(event.data);
          else call.reject(new Error("getcaptcha failed"));
        }
        return;
      }
      if (!widget.overlay || widget.overlay.querySelector("iframe").contentWindow !== event.source) return;
      if (event.data.fakeHcaptcha === "solve") {
        var passive = widget.passive;
        removeChallenge(widget);
        if (!passive) deliver(widget);
      } else if (event.data.fakeHcaptcha === "error") {
        var wasPassive = widget.passive;
        removeChallenge(widget);
        if (!wasPassive) fail(widget);
      } else if (event.data.fakeHcaptcha === "close") {
        removeChallenge(widget);
        if (typeof widget.params["close-callback"] === "function") widget.params["close-callback"]();
      }
    });
  });

  window.hcaptcha = {
    render: function (target, params) {
      var holder = holderOf(target);
      var widget = { params: params || {}, response: "", overlay: null, passive: false, ready: false, waiting: [], fields: [], checkbox: null };
      var checkbox = document.createElement("iframe");
      checkbox.title = "Widget containing checkbox for hCaptcha security challenge for Submit";
      checkbox.src = FRAME + "#frame=checkbox-invisible&sitekey=" + encodeURIComponent(widget.params.sitekey || "");
      checkbox.style.display = "none";
      widget.checkbox = checkbox;
      if (holder) {
        holder.appendChild(checkbox);
        ["h-captcha-response", "g-recaptcha-response"].forEach(function (name) {
          var box = document.createElement("textarea");
          box.name = name;
          box.id = name + "-" + widgets.length;
          box.style.display = "none";
          holder.appendChild(box);
          widget.fields.push(box);
        });
      }
      widgets.push(widget);
      // What the real widget does by itself as the page loads (tests/fixtures/apply/lever/endpoints.json): POST /checksiteconfig to three hosts.
      ["api.hcaptcha.com", "api2.hcaptcha.com", "hcaptcha.com"].forEach(function (host) {
        fetch("https://" + host + "/checksiteconfig?v=fake&host=" + encodeURIComponent(location.hostname) + "&sitekey=" + encodeURIComponent(widget.params.sitekey || ""),
          { method: "POST", body: "{}" }).catch(function () { /* a refused write is the test's point */ });
      });
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
        .then(function () { return ask(widget); })
        .then(function (found) {
          if (found && found.challenge) drawChallenge(widget, false);
          else setTimeout(function () { deliver(widget); }, 20);
        })
        .catch(function () { fail(widget); });
    },
    reset: function (id) {
      var widget = widgets[id || 0];
      if (!widget) return;
      widget.response = "";
      widget.fields.forEach(function (box) { box.value = ""; });
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
