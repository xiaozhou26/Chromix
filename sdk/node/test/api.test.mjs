// CloakBrowser-compatible API behavior tests for the chromix Node SDK.
// Run:  node --test sdk/node/test/*.test.*
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildArgs, buildContextOptions, getDefaultStealthArgs, binaryInfo,
  resolveHumanConfig,
} from "../index.js";

test("buildArgs priority: stealth < user < dedicated; no duplicate keys", () => {
  const args = buildArgs({
    stealthArgs: true,
    extraArgs: ["--fingerprint=42", "--lang=fr-FR", "--window-size=800,600"],
    timezone: "Europe/Berlin",
    locale: "de-DE",
    headless: true,
  });
  assert.ok(args.includes("--fingerprint=42"), "user seed override");
  assert.ok(args.includes("--lang=de-DE") && !args.includes("--lang=fr-FR"), "dedicated locale wins");
  assert.ok(args.includes("--fingerprint-timezone=Europe/Berlin"), "timezone flag");
  assert.ok(args.includes("--window-size=800,600"), "geometry passthrough");
  const keys = args.map((a) => a.split("=", 1)[0]);
  assert.equal(new Set(keys).size, keys.length, "no duplicate keys");
});

test("buildArgs maximize suppressed by geometry / added when free", () => {
  assert.ok(!buildArgs({ extraArgs: ["--window-size=800,600"], startMaximized: true })
    .includes("--start-maximized"), "suppressed by geometry");
  assert.ok(buildArgs({ startMaximized: true }).includes("--start-maximized"), "added when free");
});

test("default stealth args carry one seed and preserve the sandbox", () => {
  const sa = getDefaultStealthArgs();
  assert.equal(sa.filter((a) => a.startsWith("--fingerprint=")).length, 1);
  assert.ok(!sa.includes("--no-sandbox"));
});

test("context options: default viewport, explicit null, CDP emulation stripped", () => {
  const ctx = buildContextOptions({ headless: true });
  assert.equal(ctx.viewport?.width, 1920);
  assert.equal(ctx.viewport?.height, 947);
  assert.equal(buildContextOptions({ headless: true, viewport: null }).viewport, null);
  const ctx3 = buildContextOptions({ headless: true, contextOptions: { locale: "de-DE", foo: 1 } });
  assert.equal(ctx3.locale, undefined, "contextOptions.locale stripped");
  assert.equal(ctx3.foo, 1, "other contextOptions forwarded");
  assert.equal(buildContextOptions({ userAgent: "x" }).userAgent, "x");
});

test("binaryInfo shape", () => {
  const info = binaryInfo();
  assert.equal(info.tier, "open-source");
  assert.ok(typeof info.version === "string" && info.version.length > 0);
});

test("human config presets and overrides", () => {
  const cfg = resolveHumanConfig("careful", { mistype: 0.5 });
  assert.equal(cfg.typingDelay, 130, "careful preset slower");
  assert.equal(cfg.mistype, 0.5, "override applied");
});
