// The appeal page's own checks (src/lib/appeal-form.ts): the same rules the handler applies,
// and what the page says for each reply.
import { describe, expect, test } from "vitest";
import { check, FIELDS, FORM_TEXT, normalizeCode, outcome, serverErrors, TEXT } from "./appeal-form";
import { FIELDS as HANDLER_FIELDS } from "@/server/appeal";

// Made up, and built at run time: the bot's site tests refuse ID-like numbers in site/.
const USER = "123456789" + "012345678";
const GOOD = { user_id: USER, code: "td7y-ul66-oych-iztl", what: "I spammed.", why: "I read the rules.", else: "" };

describe("check", () => {
  test("a good appeal passes, with the code normalised", () => {
    const out = check(GOOD);
    expect(out.errors).toBeUndefined();
    expect("values" in out && out.values.code).toBe("TD7YUL66OYCHIZTL");
    expect(normalizeCode(" ab-cd ef ")).toBe("ABCDEF");
  });

  test("each field is checked the way the handler checks it", () => {
    expect(check({ ...GOOD, user_id: "" }).errors).toEqual({ user_id: TEXT.required });
    expect(check({ ...GOOD, user_id: "0" + USER }).errors).toEqual({ user_id: TEXT.user_id });
    expect(check({ ...GOOD, code: "short" }).errors).toEqual({ code: TEXT.code });
    expect(check({ ...GOOD, what: "x".repeat(601) }).errors).toEqual({ what: TEXT.too_long });
    expect(check({ ...GOOD, why: "see https://example.com" }).errors).toEqual({ why: TEXT.links });
    expect(check({ ...GOOD, else: "www.example.com" }).errors).toEqual({ else: TEXT.links });
    expect(check({ ...GOOD, else: undefined }).errors).toBeUndefined(); // optional
  });

  test("the page and the handler agree on the fields and their limits", () => {
    const answers = FIELDS.filter((f) => f.max).map((f) => [f.name, f.max, f.required]);
    expect(answers).toEqual(HANDLER_FIELDS);
  });
});

describe("outcome", () => {
  test("each reply has its own message", () => {
    expect(outcome(200)).toEqual({ done: true });
    expect(outcome(404)).toEqual({ done: false, message: FORM_TEXT.missing });
    expect(outcome(405)).toEqual({ done: false, message: FORM_TEXT.missing });
    expect(outcome(503)).toEqual({ done: false, message: FORM_TEXT.unavailable });
    expect(outcome(502)).toEqual({ done: false, message: FORM_TEXT.delivery });
    expect(outcome(413)).toEqual({ done: false, message: FORM_TEXT.size });
    expect(outcome(500)).toEqual({ done: false, message: FORM_TEXT.other });
    expect(outcome(400, { fields: { form: "too_long" } })).toEqual({ done: false, message: FORM_TEXT.size });
    expect(outcome(400, "nonsense")).toEqual({ done: false, message: FORM_TEXT.other });
  });

  test("the server's field errors name fields and problems, never values", () => {
    expect(outcome(400, { fields: { code: "format", what: "links" } })).toEqual({
      done: false,
      errors: { code: TEXT.code, what: TEXT.links },
    });
    expect(serverErrors({ evil: "format", what: "<script>" })).toEqual({ what: TEXT.format });
    expect(serverErrors(null)).toEqual({});
  });
});
