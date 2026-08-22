// CloakBrowser-compat behavior tests for the Node SDK.
// Run:  node sdk/node/test-cloak.mjs   (exit 0 = pass)
import {
  buildArgs, buildContextOptions, getDefaultStealthArgs, binaryInfo,
  resolveHumanConfig,
} from "./cloak.js";

let failed = 0;
function check(name, cond) {
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${name}`);
  if (!cond) failed++;
}

// buildArgs: priority stealth < user < dedicated; no duplicate keys.
const args = buildArgs({
  stealthArgs: true,
  extraArgs: ["--fingerprint=42", "--lang=fr-FR", "--window-size=800,600"],
  timezone: "Europe/Berlin",
  locale: "de-DE",
  headless: true,
});
check("user seed override", args.includes("--fingerprint=42"));
check("dedicated locale wins", args.includes("--lang=de-DE") && !args.includes("--lang=fr-FR"));
check("timezone flag", args.includes("--fingerprint-timezone=Europe/Berlin"));
check("geometry passthrough", args.includes("--window-size=800,600"));
const keys = args.map((a) => a.split("=", 1)[0]);
check("no duplicate keys", new Set(keys).size === keys.length);

// maximize suppressed by explicit geometry
check("maximize suppressed by geometry",
  !buildArgs({ extraArgs: ["--window-size=800,600"], startMaximized: true }).includes("--start-maximized"));
check("maximize added when free",
  buildArgs({ startMaximized: true }).includes("--start-maximized"));

// stealth seed shape
const sa = getDefaultStealthArgs();
check("stealth args carry one seed", sa.filter((a) => a.startsWith("--fingerprint=")).length === 1);
check("stealth args include --no-sandbox", sa.includes("--no-sandbox"));

// context options: default viewport headless, explicit null honored, CDP
// emulation fields stripped.
const ctx = buildContextOptions({ headless: true });
check("default 1080p viewport", ctx.viewport?.width === 1920 && ctx.viewport?.height === 947);
check("explicit viewport null honored", buildContextOptions({ headless: true, viewport: null }).viewport === null);
const ctx3 = buildContextOptions({ headless: true, contextOptions: { locale: "de-DE", foo: 1 } });
check("contextOptions.locale stripped (CDP emulation)", ctx3.locale === undefined && ctx3.foo === 1);
check("userAgent forwarded", buildContextOptions({ userAgent: "x" }).userAgent === "x");

// proxy split is exercised through module-private fn; binaryInfo shape.
const info = binaryInfo();
check("binaryInfo tier open-source", info.tier === "open-source");
check("binaryInfo has version", typeof info.version === "string" && info.version.length > 0);

// human config presets
const cfg = resolveHumanConfig("careful", { mistype: 0.5 });
check("careful preset slower", cfg.typingDelay === 130);
check("override applied", cfg.mistype === 0.5);

process.exit(failed ? 1 : 0);
