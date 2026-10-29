"use client";

import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { check, FIELDS, FORM_TEXT, outcome, type Errors, type Field } from "@/lib/appeal-form";

// The ban-appeal form. Checks the fields, sends them as JSON to this site's own /api/appeal
// with fetch (the page's CSP has form-action 'none', so the form never submits natively)
// and shows what came back. No values are kept, logged or put in the URL.

type Name = Field["name"];

function Hint({ name, children }: { name: Name; children: ReactNode }) {
  return (
    <p className="field-hint" id={`${name}-hint`}>
      {children}
    </p>
  );
}

export function AppealForm() {
  const form = useRef<HTMLFormElement>(null);
  const alertBox = useRef<HTMLDivElement>(null);
  const done = useRef<HTMLElement>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [formError, setFormError] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);

  const field = (name: Name) => form.current?.elements.namedItem(name) as HTMLInputElement | HTMLTextAreaElement | null;

  const showErrors = (next: Errors) => {
    setErrors(next);
    const first = FIELDS.find((f) => next[f.name]);
    if (first) {
      setFormError(FORM_TEXT.fix);
      field(first.name)?.focus();
    }
  };

  const fail = (message: string) => {
    setSending(false);
    setFormError(message);
    // after React has drawn the alert, so focus lands on a visible box
    requestAnimationFrame(() => alertBox.current?.focus());
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (sending || !form.current) return;
    setFormError("");
    const raw: Record<string, string> = {};
    for (const f of FIELDS) raw[f.name] = field(f.name)?.value ?? "";
    const result = check(raw);
    if (result.errors) {
      showErrors(result.errors);
      return;
    }
    showErrors({});
    setSending(true);
    fetch("/api/appeal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result.values),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    })
      .then(async (response) => {
        const body = response.status === 400 ? await response.json().catch(() => null) : undefined;
        const next = outcome(response.status, body);
        if (next.done) {
          setSending(false);
          form.current?.reset();
          setSent(true);
          requestAnimationFrame(() => done.current?.focus());
        } else if (next.errors) {
          setSending(false);
          showErrors(next.errors);
        } else {
          fail(next.message ?? FORM_TEXT.other);
        }
      })
      .catch(() => fail(FORM_TEXT.offline));
  };

  const clear = (name: Name) => () => {
    if (errors[name]) setErrors((prev) => ({ ...prev, [name]: undefined }));
  };

  const control = (name: Name) => ({
    id: name,
    name,
    "aria-describedby": `${name}-hint ${name}-error`,
    "aria-invalid": errors[name] ? (true as const) : undefined,
    onInput: clear(name),
  });

  const error = (name: Name) => (
    <p className="field-error" id={`${name}-error`} hidden={!errors[name]}>
      {errors[name] ?? ""}
    </p>
  );

  return (
    <>
      <form className="appeal-form" id="appeal-form" method="post" noValidate hidden={sent} ref={form} onSubmit={onSubmit}>
        <div className="appeal-alert" id="form-error" role="alert" tabIndex={-1} hidden={!formError} ref={alertBox}>
          {formError}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="user_id">Discord user ID</label>
          <Hint name="user_id">Numbers only, 17 to 20 digits. It is in the message Front Desk sent you.</Hint>
          <input className="field-input" type="text" inputMode="numeric" autoComplete="off" spellCheck={false} maxLength={20} required {...control("user_id")} />
          {error("user_id")}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="code">Appeal code</label>
          <Hint name="code">16 letters and digits, with or without the dashes.</Hint>
          <input className="field-input field-code" type="text" autoComplete="off" autoCapitalize="characters" spellCheck={false} maxLength={24} required {...control("code")} />
          {error("code")}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="what">What happened?</label>
          <Hint name="what">In your own words. Up to 600 characters.</Hint>
          <textarea className="field-input" rows={5} maxLength={600} required {...control("what")}></textarea>
          {error("what")}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="why">Why should we reconsider?</label>
          <Hint name="why">What would be different if you came back? Up to 600 characters.</Hint>
          <textarea className="field-input" rows={5} maxLength={600} required {...control("why")}></textarea>
          {error("why")}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="else">
            Anything else? <span className="field-optional">Optional</span>
          </label>
          <Hint name="else">Up to 300 characters.</Hint>
          <textarea className="field-input" rows={3} maxLength={300} {...control("else")}></textarea>
          {error("else")}
        </div>

        <p className="appeal-actions">
          <button className="btn btn-lg" id="appeal-submit" type="submit" aria-disabled={sending ? "true" : "false"}>
            Send appeal
          </button>
          <span className="label" id="appeal-busy" role="status" aria-live="polite">
            {sending ? FORM_TEXT.busy : ""}
          </span>
        </p>
      </form>

      <section className="appeal-done" id="appeal-done" aria-labelledby="done-title" tabIndex={-1} hidden={!sent} ref={done}>
        <h2 id="done-title">Your appeal was received.</h2>
        <p className="lede">
          If the user ID and code belong together and appeals are open for you, it is now with the staff team. This page
          shows the same message either way, so it can&apos;t confirm that a code is right.
        </p>
        <p className="lede">
          You don&apos;t need to send it again. Sending it twice doesn&apos;t speed anything up: extra copies are discarded.
        </p>
      </section>
    </>
  );
}
