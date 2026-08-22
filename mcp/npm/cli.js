#!/usr/bin/env node
"use strict";
/**
 * tilion-mcp — npx launcher for the Fortress stealth-browser MCP.
 *
 * The server itself is Python (the `tilion` package). This shim finds the best
 * available runner and execs it, forwarding stdio transparently so MCP clients
 * (Claude Desktop, Cursor, Cline, Windsurf, …) can spawn it with `npx tilion-mcp`.
 *
 * Preference order:
 *   1. uvx      — `uvx --from tilion tilion-mcp`   (auto-installs, no persistent state)
 *   2. pipx     — `pipx run --spec tilion tilion-mcp`
 *   3. a pre-installed `tilion-mcp` console script (pip install "tilion[mcp]")
 *   4. `python -m tilion.mcp`                        (requires tilion already installed)
 */
const { spawn, spawnSync } = require("child_process");

function has(cmd) {
  const finder = process.platform === "win32" ? "where" : "which";
  const r = spawnSync(finder, [cmd], { stdio: ["ignore", "ignore", "ignore"] });
  return r.status === 0;
}

let cmd, args;
if (has("uvx")) {
  cmd = "uvx"; args = ["--from", "tilion", "tilion-mcp"];
} else if (has("pipx")) {
  cmd = "pipx"; args = ["run", "--spec", "tilion", "tilion-mcp"];
} else if (has("tilion-mcp")) {
  cmd = "tilion-mcp"; args = [];
} else if (has("python3") || has("python")) {
  cmd = has("python3") ? "python3" : "python"; args = ["-m", "tilion.mcp"];
} else {
  process.stderr.write(
    "tilion-mcp: no Python runner found.\n" +
    "  Install uv (https://astral.sh/uv) — recommended — or run:\n" +
    "    pip install \"tilion[mcp]\"\n");
  process.exit(1);
}

// forward any extra args, inherit stdio (this is an MCP stdio server)
const child = spawn(cmd, args.concat(process.argv.slice(2)), { stdio: "inherit" });
child.on("error", (e) => {
  process.stderr.write("tilion-mcp: failed to start (" + cmd + "): " + e.message + "\n");
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code == null ? 1 : code);
});
