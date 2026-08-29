// Linux Fontconfig wiring for the bundled Windows font assets.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir, tmpdir } from "node:os";

export function linuxFontEnv(executable) {
  if (process.platform !== "linux") return {};
  const fontsDir = join(dirname(executable), "fonts");
  const template = join(fontsDir, "fonts.conf.template");
  if (!existsSync(template)) return {};
  try {
    const cacheRoot = process.env.XDG_CACHE_HOME || join(homedir(), ".cache");
    const cacheDir = join(cacheRoot, "chromix", "fontconfig");
    mkdirSync(cacheDir, { recursive: true });
    const config = readFileSync(template, "utf8")
      .split("@FONTS_DIR@").join(fontsDir)
      .split("@CACHE_DIR@").join(cacheDir);
    const configPath = join(tmpdir(), `chromix-fontconfig-${process.getuid?.() ?? 0}.conf`);
    writeFileSync(configPath, config);
    return { FONTCONFIG_FILE: configPath };
  } catch {
    return {};
  }
}

export function fontLaunchEnv(executable, userEnv) {
  const fontEnv = linuxFontEnv(executable);
  if (Object.keys(fontEnv).length === 0 && userEnv === undefined) return undefined;
  return { ...process.env, ...fontEnv, ...(userEnv || {}) };
}
