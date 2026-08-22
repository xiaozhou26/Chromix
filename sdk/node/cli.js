#!/usr/bin/env node
// CLI: chromix — manage the stealth Chromium binary.
import { VERSION, ensureBinary, binaryInfo, clearCache } from "./index.js";

const [, , cmd] = process.argv;
if (cmd === "--version" || cmd === "version") { console.log(`chromix ${VERSION}`); process.exit(0); }
if (cmd === "install") { console.log(await ensureBinary()); process.exit(0); }
if (cmd === "info") { console.log(JSON.stringify(binaryInfo(), null, 2)); process.exit(0); }
if (cmd === "clear-cache") { clearCache(); console.log("cache cleared"); process.exit(0); }
console.log("usage: chromix [--version | install | info | clear-cache]");
