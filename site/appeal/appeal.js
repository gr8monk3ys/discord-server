// The ban-appeal form. Checks the fields, sends them as JSON to this site's own
// /api/appeal with fetch (the page's CSP has form-action 'none', so the form never
// submits natively) and shows what came back. No values are kept, logged or put in
// the URL. The limits mirror api/appeal.js and bot/logic/appeals.py.
(function () {
  "use strict";

  var FIELDS = [
    { name: "user_id", required: true },
    { name: "code", required: true },
    { name: "what", required: true, max: 600 },
    { name: "why", required: true, max: 600 },
    { name: "else", required: false, max: 300 }
  ];
  var TEXT = {
    required: "This one is needed.",
    user_id: "A Discord user ID is 17 to 20 digits, with nothing else.",
    code: "The appeal code is 16 letters and digits (dashes are fine).",
    too_long: "That is too long. Please shorten it.",
    links: "Links aren't accepted. Please describe it in words.",
    format: "This doesn't look right. Please check it."
  };
  var FORM_TEXT = {
    fix: "Please check the marked fields.",
    busy: "Sending…",
    offline: "The appeal could not be sent. Check your connection and try again.",
    unavailable: "Appeals aren't open on this site right now. Please try again another day.",
    delivery: "Your appeal could not be passed on just now. Nothing was lost on your side: please try again in a few minutes.",
    size: "That is too much text to send. Please shorten your answers.",
    mirror: "This copy of the site can't take appeals. Open the appeal page from the link in the message you were sent.",
    other: "Something went wrong and the appeal was not sent. Please try again later."
  };

  var form = document.getElementById("appeal-form");
  if (!form || !window.fetch) return;
  var button = document.getElementById("appeal-submit");
  var busy = document.getElementById("appeal-busy");
  var alertBox = document.getElementById("form-error");
  var done = document.getElementById("appeal-done");
  var sending = false;

  function input(name) { return form.elements.namedItem(name); }

  function setError(name, message) {
    var el = input(name);
    var note = document.getElementById(name + "-error");
    if (!el || !note) return;
    if (message) {
      note.textContent = message;
      note.hidden = false;
      el.setAttribute("aria-invalid", "true");
    } else {
      note.textContent = "";
      note.hidden = true;
      el.removeAttribute("aria-invalid");
    }
  }

  function setFormError(message) {
    alertBox.textContent = message || "";
    alertBox.hidden = !message;
  }

  function normalizeCode(value) {
    return value.replace(/[\s-]/g, "").toUpperCase();
  }

  // { values } when everything looks right, else { errors: { name: message } }.
  function check() {
    var values = {};
    var errors = {};
    FIELDS.forEach(function (field) {
      var value = String(input(field.name).value || "").trim();
      values[field.name] = value;
      if (!value) {
        if (field.required) errors[field.name] = TEXT.required;
        return;
      }
      if (field.name === "user_id" && !/^[1-9][0-9]{14,19}$/.test(value)) errors.user_id = TEXT.user_id;
      if (field.name === "code") {
        values.code = normalizeCode(value);
        if (!/^[A-Z2-7]{16}$/.test(values.code)) errors.code = TEXT.code;
      }
      if (field.max) {
        if (Array.from(value).length > field.max) errors[field.name] = TEXT.too_long;
        else if (/:\/\/|\bwww\./i.test(value)) errors[field.name] = TEXT.links;
      }
    });
    return Object.keys(errors).length ? { errors: errors } : { values: values };
  }

  function showErrors(errors) {
    var first = null;
    FIELDS.forEach(function (field) {
      setError(field.name, errors[field.name] || "");
      if (errors[field.name] && !first) first = input(field.name);
    });
    if (first) {
      setFormError(FORM_TEXT.fix);
      first.focus();
    }
  }

  function setBusy(on) {
    sending = on;
    button.setAttribute("aria-disabled", on ? "true" : "false");
    busy.textContent = on ? FORM_TEXT.busy : "";
  }

  function fail(message) {
    setBusy(false);
    setFormError(message);
    alertBox.focus();
  }

  function finish() {
    form.reset();
    form.hidden = true;
    done.hidden = false;
    done.focus();
  }

  // The server names fields and problems, never values; unknown names are ignored.
  function serverErrors(fields) {
    var errors = {};
    FIELDS.forEach(function (field) {
      var problem = fields && typeof fields[field.name] === "string" ? fields[field.name] : null;
      if (!problem) return;
      if (problem === "format" && TEXT[field.name]) errors[field.name] = TEXT[field.name];
      else errors[field.name] = TEXT[problem] || TEXT.format;
    });
    return errors;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending) return;
    setFormError("");
    var result = check();
    if (result.errors) {
      showErrors(result.errors);
      return;
    }
    showErrors({});
    setBusy(true);
    fetch(new URL("../api/appeal", window.location.href).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result.values),
      credentials: "omit",
      cache: "no-store",
      redirect: "error"
    }).then(function (response) {
      if (response.status === 200) {
        setBusy(false);
        finish();
        return null;
      }
      if (response.status === 404 || response.status === 405) return fail(FORM_TEXT.mirror);
      if (response.status === 503) return fail(FORM_TEXT.unavailable);
      if (response.status === 502) return fail(FORM_TEXT.delivery);
      if (response.status === 413) return fail(FORM_TEXT.size);
      if (response.status !== 400) return fail(FORM_TEXT.other);
      return response.json().then(function (data) {
        var errors = serverErrors(data && data.fields);
        setBusy(false);
        if (Object.keys(errors).length) showErrors(errors);
        else fail(data && data.fields && data.fields.form === "too_long" ? FORM_TEXT.size : FORM_TEXT.other);
      });
    }).catch(function () {
      fail(FORM_TEXT.offline);
    });
  });

  FIELDS.forEach(function (field) {
    input(field.name).addEventListener("input", function () { setError(field.name, ""); });
  });
})();
