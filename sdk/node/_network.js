// Resolve WebRTC presentation addresses before launch, through the actual proxy.
import http from "node:http";
import https from "node:https";
import net, { isIP } from "node:net";
import { lookup as lookupHost } from "node:dns/promises";
import { once } from "node:events";
import { fingerprintOff, normalizeFingerprintArgs } from "./_fingerprint.js";

const GEOIP_URL = "http://ip-api.com/json/?fields=status,timezone,countryCode,query";
const MAX_RESPONSE = 64 * 1024;
const COUNTRIES = new Set(("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW").split(" "));
const LOCALES = {
  AE: "ar-AE", AR: "es-AR", AT: "de-AT", AU: "en-AU", BD: "bn-BD", BE: "nl-BE",
  BG: "bg-BG", BR: "pt-BR", CA: "en-CA", CH: "de-CH", CL: "es-CL", CN: "zh-CN",
  CO: "es-CO", CZ: "cs-CZ", DE: "de-DE", DK: "da-DK", EE: "et-EE", EG: "ar-EG",
  ES: "es-ES", FI: "fi-FI", FR: "fr-FR", GB: "en-GB", GR: "el-GR", HK: "zh-HK",
  HR: "hr-HR", HU: "hu-HU", ID: "id-ID", IE: "en-IE", IL: "he-IL", IN: "hi-IN",
  IS: "is-IS", IT: "it-IT", JP: "ja-JP", KR: "ko-KR", LT: "lt-LT", LV: "lv-LV",
  MX: "es-MX", MY: "ms-MY", NG: "en-NG", NL: "nl-NL", NO: "nb-NO", NZ: "en-NZ",
  PE: "es-PE", PH: "fil-PH", PK: "ur-PK", PL: "pl-PL", PT: "pt-PT", RO: "ro-RO",
  RS: "sr-RS", RU: "ru-RU", SA: "ar-SA", SE: "sv-SE", SG: "en-SG", SI: "sl-SI",
  SK: "sk-SK", TH: "th-TH", TR: "tr-TR", TW: "zh-TW", UA: "uk-UA", US: "en-US",
  VE: "es-VE", VN: "vi-VN", ZA: "en-ZA",
};

function proxyError() { return new Error("Invalid proxy URL or credentials"); }

export function splitProxy(proxy) {
  if (proxy == null || proxy === "") return undefined;
  const input = typeof proxy === "string" ? { server: proxy } : proxy;
  let url, username, password;
  try {
    if (typeof input.server !== "string" || /[\s\x00-\x1f\x7f]/.test(input.server)) throw proxyError();
    url = new URL(input.server.includes("://") ? input.server : `http://${input.server}`);
    if (!url.hostname || url.port === "0" || url.search || url.hash || !["", "/"].includes(url.pathname) ||
        !["http:", "https:", "socks:", "socks4:", "socks4a:", "socks5:", "socks5h:"].includes(url.protocol)) throw proxyError();
    username = url.username ? decodeURIComponent(url.username) : undefined;
    password = url.password ? decodeURIComponent(url.password) : undefined;
    if (input.server.includes("@")) {
      username ??= "";
      password ??= "";
    }
    if (input.username !== undefined) username = input.username;
    if (input.password !== undefined) password = input.password;
    for (const value of [username, password])
      if (value !== undefined && (typeof value !== "string" || /[\x00-\x1f\x7f]/.test(value))) throw proxyError();
  } catch { throw proxyError(); }
  return { server: `${url.protocol}//${url.host}`,
    ...(username !== undefined ? { username } : {}),
    ...(password !== undefined ? { password } : {}),
    ...(input.bypass !== undefined ? { bypass: input.bypass } : {}),
  };
}

export function extractProxyUrl(proxy) {
  const config = splitProxy(proxy);
  if (!config) return null;
  if (config.username === undefined && config.password === undefined) return config.server;
  const url = new URL(config.server);
  return `${url.protocol}//${encodeURIComponent(config.username ?? "")}:${encodeURIComponent(config.password ?? "")}@${url.host}`;
}

export function networkArgs(args = [], proxy) {
  const result = normalizeFingerprintArgs(args);
  for (const [index, arg] of result.entries()) {
    if (/^--(?:fingerprint|uxr)-webrtc-fake-srflx(?:-allow-udp)?(?:=|$)/.test(arg))
      throw new Error(`${arg.split("=", 1)[0]} is retired; use --fingerprint-webrtc-ip for candidate presentation.`);
    const key = arg.split("=", 1)[0];
    if (["--fingerprint-webrtc-ip", "--uxr-webrtc-ip"].includes(key)) {
      const value = arg.includes("=") ? arg.slice(arg.indexOf("=") + 1) : "";
      if (value.toLowerCase() === "auto") result[index] = `${key}=auto`;
      else if (!isIP(value) || value.includes("%"))
        throw new Error(`${key} requires an IPv4/IPv6 address or auto`);
      else if (isIP(value) === 6)
        result[index] = `${key}=${new URL(`http://[${value}]/`).hostname.slice(1, -1)}`;
    }
  }
  const flags = new Set(result.map((a) => a.split("=", 1)[0]));
  const proxied = !flags.has('--no-proxy-server') && (proxy || flags.has('--proxy-server'));
  if (proxied && !flags.has('--force-webrtc-ip-handling-policy'))
    result.push("--force-webrtc-ip-handling-policy=disable_non_proxied_udp");
  return result;
}

export function lookupProxy(args, proxy) {
  const flags = new Map(args.map((arg) => [arg.split("=", 1)[0], arg.includes("=") ? arg.slice(arg.indexOf("=") + 1) : ""]));
  if (flags.has('--no-proxy-server')) {
    if (proxy) throw new Error('GeoIP/auto proxy conflicts with --no-proxy-server');
    return undefined;
  }
  if (flags.has("--proxy-pac-url") || flags.has("--proxy-auto-detect"))
    throw new Error("GeoIP/auto requires an explicit proxy route, not PAC/auto-detect");
  const server = flags.get("--proxy-server");
  if (flags.has('--proxy-server') && (!server || /[;,=]/.test(server)))
    throw new Error("GeoIP/auto requires a single --proxy-server route");
  const raw = splitProxy(server);
  if (raw && (raw.username !== undefined || raw.password !== undefined))
    throw new Error("GeoIP/auto raw --proxy-server cannot carry credentials; use the proxy option");
  if (proxy != null) {
    const configured = splitProxy(proxy);
    // Playwright also emits --proxy-server; a raw argument must not silently
    // make the browser and its metadata lookup use different routes.
    if (raw && (!configured || proxyRouteKey(raw) !== proxyRouteKey(configured)))
      throw new Error("GeoIP/auto proxy conflicts with raw --proxy-server");
    return proxy;
  }
  return server;
}

function proxyRouteKey(config) {
  const url = new URL(config.server);
  const defaultPort = url.protocol.startsWith('socks') ? '1080' : url.protocol === 'https:' ? '443' : '80';
  return `${url.protocol}//${url.hostname}:${url.port || defaultPort}`;
}

export async function resolveWebrtcArgs(args, proxy, { exitIp, geoip = false, lookup = geoipHttp } = {}) {
  let result = networkArgs(args);
  if (fingerprintOff(result)) return result;
  const flags = new Map(result.map((arg) => [arg.split("=", 1)[0], arg.includes("=") ? arg.slice(arg.indexOf("=") + 1) : ""]));
  const value = flags.get("--uxr-webrtc-ip") ?? flags.get("--fingerprint-webrtc-ip");
  if (value === "auto" || (value === undefined && geoip)) {
    if (exitIp == null) {
      const metadata = await lookup(extractProxyUrl(lookupProxy(result, proxy)));
      if (!metadata) throw new Error("WebRTC auto IP lookup failed; no direct fallback");
      exitIp = metadata.exitIp;
    }
    if (typeof exitIp !== "string" || !isIP(exitIp) || exitIp.includes("%"))
      throw new Error("WebRTC auto returned an invalid IP");
    result = result.filter((arg) => !["--fingerprint-webrtc-ip", "--uxr-webrtc-ip"].includes(arg.split("=", 1)[0]));
    result.push(`--fingerprint-webrtc-ip=${exitIp}`);
  }
  return networkArgs(result);
}

function validateGeoip(data) {
  if (!data || data.status !== "success" || typeof data.query !== "string" ||
      data.query.includes("%") || !isIP(data.query) || typeof data.timezone !== "string" ||
      data.timezone.length > 100 || !/^[A-Za-z0-9_+-]+(?:\/[A-Za-z0-9_+-]+)*$/.test(data.timezone) ||
      typeof data.countryCode !== "string" || !COUNTRIES.has(data.countryCode))
    throw new Error("GeoIP returned invalid status, IP, timezone or countryCode");
  try { new Intl.DateTimeFormat("en", { timeZone: data.timezone }); }
  catch { throw new Error("GeoIP returned an unknown timezone"); }
  // Country is not a language; leave unmapped regions unset rather than emit e.g. 'jp'.
  return { timezone: data.timezone, locale: LOCALES[data.countryCode] ?? null, exitIp: data.query };
}

function readSocksBytes(socket, size) {
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (error, value) => {
      if (done) return;
      done = true;
      socket.off("readable", pump); socket.off("error", fail);
      socket.off("end", ended); socket.off("close", ended);
      if (error) reject(error); else resolve(value);
    };
    const fail = (error) => finish(error);
    const ended = () => finish(new Error("GeoIP SOCKS reply was truncated; no direct fallback"));
    const pump = () => {
      const value = socket.read(size);
      if (value !== null) finish(null, value);
      else if (socket.destroyed || socket.readableEnded) ended();
    };
    socket.on("readable", pump); socket.on("error", fail);
    socket.on("end", ended); socket.on("close", ended);
    pump();
  });
}

function socksDestination(host) {
  if (isIP(host) === 4) return Buffer.from([1, ...host.split(".").map(Number)]);
  if (isIP(host) === 6) {
    const canonical = new URL(`http://[${host}]/`).hostname.slice(1, -1);
    const [left, right = ""] = canonical.split("::");
    const prefix = left ? left.split(":") : [], suffix = right ? right.split(":") : [];
    const words = canonical.includes("::") ? [...prefix, ...Array(8 - prefix.length - suffix.length).fill("0"), ...suffix] : prefix;
    const result = Buffer.alloc(17); result[0] = 4;
    words.forEach((word, i) => result.writeUInt16BE(parseInt(word, 16), 1 + i * 2));
    return result;
  }
  const name = Buffer.from(host, "ascii"); // URL has already applied IDNA.
  if (name.length < 1 || name.length > 255) throw new Error("GeoIP SOCKS hostname is too long");
  return Buffer.concat([Buffer.from([3, name.length]), name]);
}

async function socksConnection(proxy, target, timeoutMs) {
  const route = new URL(proxy.server);
  const host = target.hostname.replace(/^\[|\]$/g, "");
  const user = Buffer.from(proxy.username ?? "", "utf8"), password = Buffer.from(proxy.password ?? "", "utf8");
  const v4 = ["socks4:", "socks4a:"].includes(route.protocol);
  const authenticated = proxy.username !== undefined || proxy.password !== undefined;
  if (v4 && (password.length || user.length > 255))
    throw new Error("GeoIP SOCKS4 supports a user ID, not password authentication");
  if (!v4 && authenticated && (!user.length || user.length > 255 || !password.length || password.length > 255))
    throw new Error("GeoIP SOCKS5 username/password must contain 1 to 255 UTF-8 bytes");
  const socket = net.createConnection({ host: route.hostname.replace(/^\[|\]$/g, ""), port: Number(route.port || 1080) });
  // Always retain an error listener between handshake awaits and the HTTP handoff.
  const ignoreError = () => {};
  socket.on("error", ignoreError);
  const timer = setTimeout(() => socket.destroy(new Error("GeoIP SOCKS connection timed out")), timeoutMs);
  try {
    await once(socket, "connect");
    const port = Buffer.alloc(2); port.writeUInt16BE(Number(target.port || 80));
    if (v4) {
      let address, suffix = Buffer.alloc(0);
      if (route.protocol === "socks4a:") {
        if (Buffer.byteLength(host) > 255) throw new Error("GeoIP SOCKS hostname is too long");
        address = Buffer.from([0, 0, 0, 1]); suffix = Buffer.from(host + "\0", "ascii");
      } else {
        // SOCKS4 DNS is local; SOCKS4a and SOCKS5 DNS stays at the proxy.
        const result = await new Promise((resolve, reject) => {
          const closed = () => reject(new Error("GeoIP SOCKS connection timed out"));
          socket.once("close", closed);
          lookupHost(host, { family: 4 }).then(resolve, reject)
            .finally(() => socket.off("close", closed));
        });
        address = Buffer.from(result.address.split(".").map(Number));
      }
      if (socket.destroyed) throw new Error("GeoIP SOCKS connection timed out");
      socket.write(Buffer.concat([Buffer.from([4, 1]), port, address, user, Buffer.from([0]), suffix]));
      const reply = await readSocksBytes(socket, 8);
      if (reply[0] !== 0 || reply[1] !== 90) throw new Error("GeoIP SOCKS4 connection refused");
    } else {
      const method = authenticated ? 2 : 0;
      socket.write(Buffer.from([5, 1, method]));
      const selected = await readSocksBytes(socket, 2);
      if (selected[0] !== 5 || selected[1] !== method) throw new Error("GeoIP SOCKS5 authentication method rejected");
      if (authenticated) {
        socket.write(Buffer.concat([Buffer.from([1, user.length]), user, Buffer.from([password.length]), password]));
        const reply = await readSocksBytes(socket, 2);
        if (reply[0] !== 1 || reply[1] !== 0) throw new Error("GeoIP SOCKS5 authentication failed");
      }
      socket.write(Buffer.concat([Buffer.from([5, 1, 0]), socksDestination(host), port]));
      const reply = await readSocksBytes(socket, 4);
      if (reply[0] !== 5 || reply[1] !== 0 || reply[2] !== 0 || ![1, 3, 4].includes(reply[3]))
        throw new Error("GeoIP SOCKS5 connection refused");
      const length = reply[3] === 3 ? (await readSocksBytes(socket, 1))[0] : (reply[3] === 1 ? 4 : 16);
      if (!length) throw new Error("GeoIP SOCKS5 returned an invalid bound address");
      await readSocksBytes(socket, length + 2);
    }
    return socket;
  } catch (error) {
    socket.destroy();
    throw new Error(`${error.message.startsWith("GeoIP") ? error.message : "GeoIP SOCKS connection failed"}; no direct fallback`);
  } finally { clearTimeout(timer); }
}

export async function geoipHttp(proxyUrl, endpoint = GEOIP_URL) {
  const proxy = splitProxy(proxyUrl);
  const target = new URL(endpoint);
  if (target.protocol !== "http:" || target.username || target.password || target.hash)
    throw new Error("GeoIP endpoint must be an HTTP URL without credentials or fragment");
  const route = proxy ? new URL(proxy.server) : target;
  const socks = ["socks:", "socks4:", "socks4a:", "socks5:", "socks5h:"].includes(route.protocol);
  const timeout = Number(process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS ?? 10);
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 60)
    throw new Error("CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS must be greater than 0 and at most 60");
  const headers = { Host: target.host, Accept: "application/json", "Accept-Encoding": "identity" };
  if (proxy && !socks && (proxy.username !== undefined || proxy.password !== undefined)) {
    if (proxy.username?.includes(":")) throw new Error("GeoIP Basic proxy username cannot contain ':'");
    headers["Proxy-Authorization"] = `Basic ${Buffer.from(`${proxy.username ?? ""}:${proxy.password ?? ""}`, "utf8").toString("base64")}`;
  }
  const deadline = Date.now() + timeout * 1000;
  const tunnel = socks ? await socksConnection(proxy, target, timeout * 1000) : null;
  const agent = socks ? new http.Agent({ keepAlive: false }) : null;
  if (agent) agent.createConnection = () => tunnel;
  // No environment proxy discovery, bypass rules, redirects, retries or direct fallback.
  return new Promise((resolve, reject) => {
    let request, response, done = false;
    const finish = (error, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      response?.destroy();
      request?.destroy();
      agent?.destroy(); tunnel?.destroy();
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => finish(new Error("GeoIP lookup timed out; no direct fallback")), Math.max(1, deadline - Date.now()));
    try {
      if (Date.now() >= deadline) return finish(new Error("GeoIP lookup timed out; no direct fallback"));
      request = (route.protocol === "https:" ? https : http).request({
        hostname: (socks ? target : route).hostname.replace(/^\[|\]$/g, ""), port: (socks ? target : route).port || undefined,
        servername: isIP(route.hostname.replace(/^\[|\]$/g, "")) ? "" : route.hostname,
        method: "GET", path: proxy && !socks ? target.href : target.pathname + target.search,
        headers, agent: agent ?? false,
      }, (res) => {
        response = res;
        res.on("error", () => finish(new Error("GeoIP response failed; no direct fallback")));
        if (res.statusCode !== 200) return finish(new Error(`GeoIP HTTP ${res.statusCode}; redirects are disabled; no direct fallback`));
        if (Number(res.headers["content-length"]) > MAX_RESPONSE)
          return finish(new Error("GeoIP response exceeds 65536 bytes"));
        const chunks = [];
        let size = 0;
        res.on("data", (chunk) => {
          size += chunk.length;
          if (size > MAX_RESPONSE) return finish(new Error("GeoIP response exceeds 65536 bytes"));
          chunks.push(chunk);
        });
        res.on("end", () => {
          if (done) return;
          try { finish(null, validateGeoip(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks))))); }
          catch { finish(new Error("GeoIP returned invalid JSON, status, IP, timezone or countryCode")); }
        });
      });
      request.on("error", () => finish(new Error("GeoIP connection failed; no direct fallback")));
      request.end();
    } catch { finish(new Error("GeoIP request failed; no direct fallback")); }
  });
}
