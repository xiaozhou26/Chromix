// Public switch syntax; keep in parity with the Python launcher.
const OFF = new Set(["off", "false", "0", "disable", "disabled"]);
const BRANDS = new Map([["chrome", "Chrome"], ["google chrome", "Chrome"],
  ["edge", "Edge"], ["microsoft edge", "Edge"], ["opera", "Opera"], ["vivaldi", "Vivaldi"]]);
const BOOLEANS = new Set(["--fingerprint-noise", "--fingerprint-sapi-voices",
  "--fingerprint-allow-3p-cookies", "--fingerprint-windows-font-metrics"]);
const INTEGERS = new Map([
  ["--fingerprint-hardware-concurrency", [1n, 128n]],
  ["--fingerprint-screen-width", [1n, 32768n]],
  ["--fingerprint-screen-height", [1n, 32768n]],
  ["--fingerprint-taskbar-height", [0n, 32768n]],
  ["--fingerprint-storage-quota", [0n, ((1n << 63n) - 1n) / 1048576n]],
]);

export function fingerprintOff(args) {
  const flag = args.filter((a) => a.split("=", 1)[0] === "--fingerprint").at(-1);
  return flag !== undefined && OFF.has(flag.slice(flag.indexOf("=") + 1).toLowerCase());
}

export function normalizeFingerprintArgs(args = []) {
  let result = (args || []).map((arg) => {
    if (typeof arg !== "string" || arg.includes("\0"))
      throw new Error("browser arguments must be strings without NUL");
    const separator = arg.indexOf("=");
    const key = separator < 0 ? arg : arg.slice(0, separator);
    const value = separator < 0 ? "" : arg.slice(separator + 1);
    if (key === "--fingerprint") {
      if (OFF.has(value.toLowerCase())) return "--fingerprint=off";
      if (value && (!/^[0-9]+$/.test(value) || BigInt(value) < 1n || BigInt(value) >= 1n << 64n))
        throw new Error("--fingerprint requires a uint64 seed or off/false/0/disable/disabled");
    } else if (key === "--fingerprint-brand") {
      const brand = BRANDS.get(value.toLowerCase());
      if (!brand) throw new Error("--fingerprint-brand must be Chrome, Edge, Opera or Vivaldi");
      return `${key}=${brand}`;
    } else if (BOOLEANS.has(key)) {
      const low = value.toLowerCase();
      if (OFF.has(low)) return `${key}=false`;
      if (["", "true", "1", "on", "enable", "enabled"].includes(low)) return `${key}=true`;
      throw new Error(`${key} requires a boolean`);
    } else if (INTEGERS.has(key)) {
      const [min, max] = INTEGERS.get(key);
      if (!/^[0-9]+$/.test(value) || BigInt(value) < min || BigInt(value) > max)
        throw new Error(`${key} requires an integer in [${min}, ${max}]`);
    } else if (key === "--fingerprint-device-memory") {
      if (!/^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$/.test(value) || !Number.isFinite(Number(value)) ||
          Number(value) <= 0 || Number(value) > 32)
        throw new Error("--fingerprint-device-memory requires a number greater than 0 and at most 32");
    } else if (["--fingerprint-brand-version", "--fingerprint-platform-version"].includes(key)) {
      if (!/^[0-9]+(?:\.[0-9]+){0,3}$/.test(value) || value.split(".").some((n) => BigInt(n) > 0xffffffffn))
        throw new Error(`${key} requires a numeric version with at most four components`);
      if (key === "--fingerprint-brand-version" && (BigInt(value.split(".")[0]) < 1n || BigInt(value.split(".")[0]) > 0x7fffffffn))
        throw new Error(`${key} requires a positive int32 major version`);
    }
    return arg;
  });
  if (fingerprintOff(result))
    result = result.filter((arg) => arg.split("=", 1)[0] !== "--fingerprint-platform");
  return result;
}
