// Client for the local service. Holds no secrets: the service keeps the Telegram session.

export const DEFAULT_PORT = 8765;

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let port = DEFAULT_PORT;

export async function loadPort() {
  try {
    const { port: saved } = await chrome.storage.local.get("port");
    const n = Number(saved);
    if (Number.isInteger(n) && n >= 1024 && n <= 65535) port = n;
  } catch {
    // keep the default
  }
  return port;
}

export async function savePort(value) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1024 || n > 65535) throw new Error("Port must be a number between 1024 and 65535.");
  port = n;
  await chrome.storage.local.set({ port: n });
}

export function currentPort() {
  return port;
}

export async function api(path, { method = "GET", body, timeoutMs = 120_000 } = {}) {
  const headers = { "X-TGCleaner": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`http://127.0.0.1:${port}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    });
  } catch (e) {
    throw new ApiError(0, "service_offline", "The local service is not running (or not on this port).");
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
