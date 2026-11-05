import assert from "node:assert/strict";
import { afterEach, test, mock } from "node:test";
import nodemailer from "nodemailer";
import {
  createLeadLimiter, handleLeadRequest, leadMailConfig, leadMessage, sendLeadEmail, validateLead,
} from "../lib/leads";
import { LICENSOR } from "../lib/legal";

const envNames = ["LEADS_SMTP_HOST", "LEADS_SMTP_PORT", "LEADS_SMTP_USER", "LEADS_SMTP_PASSWORD", "LEADS_SMTP_FROM", "LEADS_TO_EMAIL"];
const savedEnv = Object.fromEntries(envNames.map((name) => [name, process.env[name]]));
afterEach(() => {
  mock.restoreAll();
  for (const name of envNames) {
    if (savedEnv[name] === undefined) delete process.env[name];
    else process.env[name] = savedEnv[name];
  }
});

const valid = { phone: "+7 (900) 000-00-00", consent: true };
const noLimit = () => 0;
function configured() {
  for (const name of envNames) delete process.env[name];
  Object.assign(process.env, {
    LEADS_SMTP_HOST: "smtp.example.test", LEADS_SMTP_USER: "sender@example.test", LEADS_SMTP_PASSWORD: "synthetic-test-secret",
  });
}
function request(body: unknown, headers: Record<string, string> = {}) {
  return new Request("https://anonai.ru/api/leads", {
    method: "POST",
    headers: { origin: "https://anonai.ru", "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

test("phone is required, company and email are optional, and contact text is bounded", () => {
  assert.deepEqual(validateLead(valid), { phone: valid.phone, company: "", email: "" });
  for (const phone of ["", "123", "call me", "+79000000000\r\nBcc: x@example.test", "1".repeat(16), 123]) {
    assert.equal(typeof validateLead({ ...valid, phone }), "string");
  }
  for (const update of [
    { company: "x".repeat(161) }, { company: {} }, { email: "broken" },
    { email: "a@example.test\nBcc: x@example.test" }, { email: "a@example.test,b@example.test" }, { consent: false },
  ]) assert.equal(typeof validateLead({ ...valid, ...update }), "string");
  for (const body of [null, [], "text"]) assert.equal(typeof validateLead(body), "string");
});

test("SMTP config is server-controlled and fails closed", () => {
  assert.equal(leadMailConfig({}), null);
  configured();
  assert.equal(leadMailConfig()!.to, LICENSOR.email);
  assert.equal(leadMailConfig()!.port, 465);
  for (const update of [
    { LEADS_SMTP_PORT: "0" }, { LEADS_SMTP_PORT: "65536" }, { LEADS_SMTP_PORT: "oops" },
    { LEADS_TO_EMAIL: "a@example.test,b@example.test" }, { LEADS_SMTP_FROM: "a@example.test\r\nBcc: b@example.test" },
  ]) assert.equal(leadMailConfig({ ...process.env, ...update }), null);
});

test("email has fixed recipient and subject, optional reply-to, and contact consent", () => {
  const config = { from: "sender@example.test", to: "owner@example.test" };
  const lead = { phone: valid.phone, company: "Synthetic company", email: "reply@example.test" };
  const message = leadMessage(lead, config, new Date("2026-11-05T00:00:00Z"));
  assert.equal(message.to.address, config.to);
  assert.equal(message.from.address, config.from);
  assert.equal(message.replyTo?.address, lead.email);
  assert.ok(message.text.includes(lead.phone));
  assert.ok(message.text.includes(lead.company));
  assert.ok(message.text.includes("2026-11-05T00:00:00.000Z"));
  assert.equal(leadMessage({ ...lead, email: "" }, config).replyTo, undefined);
  assert.equal(message.disableFileAccess, true);
  assert.equal(message.disableUrlAccess, true);
});

test("SMTP recipient acceptance is required; TLS cannot silently downgrade", async () => {
  configured();
  const config = leadMailConfig()!;
  let transportOptions: Record<string, unknown> | undefined;
  let accept = true;
  mock.method(nodemailer, "createTransport", (options: Record<string, unknown>) => {
    transportOptions = options;
    return { sendMail: async () => ({ accepted: accept ? [config.to] : [], rejected: accept ? [] : [config.to] }) };
  });
  const lead = { phone: valid.phone, company: "", email: "" };
  await sendLeadEmail(lead, config);
  assert.equal(transportOptions!.secure, true);
  await sendLeadEmail(lead, { ...config, port: 587 });
  assert.equal(transportOptions!.requireTLS, true);
  assert.equal(transportOptions!.secure, false);
  accept = false;
  await assert.rejects(sendLeadEmail(lead, config));
});

test("success is returned only after the email sender resolves", async () => {
  configured();
  let completed = false;
  const response = await handleLeadRequest(request(valid), async (lead, config) => {
    assert.equal(lead.phone, valid.phone);
    assert.equal(config.to, LICENSOR.email);
    completed = true;
  }, noLimit);
  assert.equal(response.status, 200);
  assert.equal(completed, true);
  assert.deepEqual(await response.json(), { ok: true });
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("invalid, cross-origin, bot and oversized requests do not send email", async () => {
  configured();
  let sent = 0;
  const send = async () => { sent++; };
  for (const [req, status] of [
    [request({ ...valid, phone: "bad" }), 400],
    [request({ ...valid, consent: false }), 400],
    [request({ ...valid, website: "bot.example" }), 400],
    [request(valid, { origin: "https://evil.example" }), 403],
    [request(valid, { origin: "" }), 403],
    [request(valid, { "content-type": "text/plain" }), 415],
    [request({ ...valid, company: "x".repeat(5000) }), 413],
  ] as const) assert.equal((await handleLeadRequest(req, send, noLimit)).status, status);
  assert.equal(sent, 0);
});

test("missing config and SMTP failures never report success or leak errors", async () => {
  configured();
  delete process.env.LEADS_SMTP_PASSWORD;
  let sent = false;
  const missing = await handleLeadRequest(request(valid), async () => { sent = true; }, noLimit);
  assert.equal(missing.status, 503);
  assert.equal(sent, false);
  configured();
  mock.method(console, "error", () => {});
  const failed = await handleLeadRequest(request(valid), async () => { throw new Error("private SMTP details"); }, noLimit);
  assert.equal(failed.status, 502);
  assert.ok(!(await failed.text()).includes("private SMTP details"));
});

test("rate limit returns 429 with retry delay before sending", async () => {
  const response = await handleLeadRequest(request(valid), async () => { assert.fail("must not send"); }, () => 60);
  assert.equal(response.status, 429);
  assert.equal(response.headers.get("retry-after"), "60");
  const limit = createLeadLimiter();
  for (let i = 0; i < 5; i++) assert.equal(limit("192.0.2.1", 1000), 0);
  assert.equal(limit("192.0.2.1", 1000), 900);
  assert.equal(limit("192.0.2.1", 1000 + 15 * 60_000), 0);
});
