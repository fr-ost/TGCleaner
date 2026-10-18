// Client for the TG Cleaner service: local (http://127.0.0.1:8765, the default) or hosted (https://…, e.g. Railway).
// The service keeps the Telegram session. The same files run as the Chrome extension and as the web
// dashboard the service serves at "/" (then the address is simply the page's own origin).

export const DEFAULT_URL = "http://127.0.0.1:8765";

/** True inside the Chrome extension; false when the page is served by the service itself. */
export const inExtension = typeof chrome !== "undefined" && Boolean(chrome.runtime?.id && chrome.storage?.local);

/** Small key/value storage: chrome.storage in the extension, localStorage on the web. */
export async function storageGet(keys) {
  if (inExtension) return chrome.storage.local.get(keys);
  const out = {};
  for (const key of keys) {
    try {
      const raw = localStorage.getItem(`tgc:${key}`);
      if (raw !== null) out[key] = JSON.parse(raw);
    } catch {
      // unavailable or corrupt: treat as unset
    }
  }
  return out;
}

export async function storageSet(values) {
  if (inExtension) return chrome.storage.local.set(values);
  for (const [key, value] of Object.entries(values)) {
    try {
      localStorage.setItem(`tgc:${key}`, JSON.stringify(value));
    } catch {
      // private mode etc.: settings just won't persist
    }
  }
}

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let baseUrl = inExtension || typeof location === "undefined" ? DEFAULT_URL : location.origin;
let accessToken = "";

/**
 * Accepts a local address (http://127.0.0.1:PORT or http://localhost:PORT) or an https:// address.
 * Plain http to anything else is refused so the access token never travels unencrypted.
 * Returns the normalized origin, or throws with a readable message.
 */
export function normalizeServiceUrl(value) {
  let url;
  try {
    url = new URL(String(value).trim());
  } catch {
    throw new Error("Enter the service address, e.g. http://127.0.0.1:8765 or https://your-app.up.railway.app");
  }
  const local = url.hostname === "127.0.0.1" || url.hostname === "localhost";
  if (url.protocol === "http:" && !local) throw new Error("A hosted service must use https://");
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("The address must start with http:// or https://");
  return url.origin;
}

export const isLocal = (url = baseUrl) => /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(url);

export async function loadSettings() {
  try {
    const { serviceUrl, accessToken: token, port } = await storageGet(["serviceUrl", "accessToken", "port"]);
    if (!inExtension) {
      // served by the service: always talk to it
    } else if (serviceUrl) baseUrl = normalizeServiceUrl(serviceUrl);
    else if (Number.isInteger(Number(port)) && port) baseUrl = `http://127.0.0.1:${Number(port)}`; // older versions stored only a port
    accessToken = typeof token === "string" ? token : "";
  } catch {
    // keep the defaults
  }
  return { serviceUrl: baseUrl, accessToken };
}

export async function saveSettings(serviceUrl, token) {
  const url = inExtension ? normalizeServiceUrl(serviceUrl) : baseUrl;
  const cleaned = String(token ?? "").trim();
  baseUrl = url;
  accessToken = cleaned;
  await storageSet({ serviceUrl: url, accessToken: cleaned });
}

export function currentSettings() {
  return { serviceUrl: baseUrl, accessToken };
}

export async function api(path, { method = "GET", body, timeoutMs = 120_000 } = {}) {
  const headers = { "X-TGCleaner": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    });
  } catch (e) {
    throw new ApiError(0, "service_offline", `The service at ${baseUrl} is not answering.`);
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    // non-JSON response
  }
  if (!res.ok) {
    throw new ApiError(res.status, data?.error ?? `http_${res.status}`, data?.message ?? `Request failed (HTTP ${res.status}).`);
  }
  return data;
}
