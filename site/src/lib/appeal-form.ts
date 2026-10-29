// The ban-appeal form's rules and wording, shared by the form (src/components/AppealForm.tsx)
// and its tests. The limits mirror src/server/appeal.ts and bot/logic/appeals.py.

export type Field = { name: "user_id" | "code" | "what" | "why" | "else"; required: boolean; max?: number };

export const FIELDS: Field[] = [
  { name: "user_id", required: true },
  { name: "code", required: true },
  { name: "what", required: true, max: 600 },
  { name: "why", required: true, max: 600 },
  { name: "else", required: false, max: 300 },
];

export const TEXT: Record<string, string> = {
  required: "This one is needed.",
  user_id: "A Discord user ID is 17 to 20 digits, with nothing else.",
  code: "The appeal code is 16 letters and digits (dashes are fine).",
  too_long: "That is too long. Please shorten it.",
  links: "Links aren't accepted. Please describe it in words.",
  format: "This doesn't look right. Please check it.",
};

export const FORM_TEXT = {
  fix: "Please check the marked fields.",
  busy: "Sending…",
  offline: "The appeal could not be sent. Check your connection and try again.",
  unavailable: "Appeals aren't open on this site right now. Please try again another day.",
  delivery: "Your appeal could not be passed on just now. Nothing was lost on your side: please try again in a few minutes.",
  size: "That is too much text to send. Please shorten your answers.",
  missing: "This copy of the site can't take appeals. Open the appeal page from the link in the message you were sent.",
  other: "Something went wrong and the appeal was not sent. Please try again later.",
} as const;

export type Values = Record<Field["name"], string>;
export type Errors = Partial<Record<Field["name"], string>>;

export function normalizeCode(value: string): string {
  return value.replace(/[\s-]/g, "").toUpperCase();
}

/** { values } when everything looks right, else { errors: { name: message } }. */
export function check(raw: Record<string, unknown>): { values: Values; errors?: undefined } | { errors: Errors } {
  const values = {} as Values;
  const errors: Errors = {};
  for (const field of FIELDS) {
    const value = String(raw[field.name] ?? "").trim();
    values[field.name] = value;
    if (!value) {
      if (field.required) errors[field.name] = TEXT.required;
      continue;
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
  }
  return Object.keys(errors).length ? { errors } : { values };
}

/** The server names fields and problems, never values; unknown names are ignored. */
export function serverErrors(fields: unknown): Errors {
  const errors: Errors = {};
  const named = fields && typeof fields === "object" ? (fields as Record<string, unknown>) : {};
  for (const field of FIELDS) {
    const problem = typeof named[field.name] === "string" ? (named[field.name] as string) : null;
    if (!problem) continue;
    if (problem === "format" && TEXT[field.name]) errors[field.name] = TEXT[field.name];
    else errors[field.name] = TEXT[problem] || TEXT.format;
  }
  return errors;
}

export type Outcome = { done: true } | { done: false; errors?: Errors; message?: string };

/** What the page shows for one reply from /api/appeal. `body` is the parsed JSON for a 400. */
export function outcome(status: number, body?: unknown): Outcome {
  if (status === 200) return { done: true };
  if (status === 404 || status === 405) return { done: false, message: FORM_TEXT.missing };
  if (status === 503) return { done: false, message: FORM_TEXT.unavailable };
  if (status === 502) return { done: false, message: FORM_TEXT.delivery };
  if (status === 413) return { done: false, message: FORM_TEXT.size };
  if (status !== 400) return { done: false, message: FORM_TEXT.other };
  const fields = body && typeof body === "object" ? (body as Record<string, unknown>).fields : undefined;
  const errors = serverErrors(fields);
  if (Object.keys(errors).length) return { done: false, errors };
  const tooLong = fields && typeof fields === "object" && (fields as Record<string, unknown>).form === "too_long";
  return { done: false, message: tooLong ? FORM_TEXT.size : FORM_TEXT.other };
}
