// Pure functions for filtering, sorting and selecting items. No DOM, no chrome.* (unit tested with node --test).

export const DAY_MS = 86_400_000;
export const PRESET_THRESHOLDS = [7, 30, 90, 180];
export const MAX_THRESHOLD = 3650;

export const KIND_LABELS = {
  channel: "Channel",
  supergroup: "Supergroup",
  gigagroup: "Broadcast group",
  group: "Basic group",
  other: "Other",
};

export const ROLE_LABELS = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
  subscriber: "Subscriber",
  none: "—",
  unknown: "—",
};

/** Days since the last known message, or null when Telegram gave us no history. */
export function daysSince(iso, now) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (now - t) / DAY_MS);
}

/** "unknown" is its own bucket: missing history is never treated as inactivity. */
export function activityOf(item, thresholdDays, now) {
  const days = daysSince(item.last_message_at, now);
  if (days === null) return "unknown";
  return days >= thresholdDays ? "inactive" : "active";
}

export function parseThreshold(value) {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n) || n < 1) return null;
  return Math.min(n, MAX_THRESHOLD);
}

export function formatAgo(days) {
  if (days === null || days === undefined) return "Unknown";
  if (days < 1) return "Today";
  if (days < 2) return "1 day ago";
  if (days < 60) return `${Math.floor(days)} days ago`;
  if (days < 730) return `${Math.floor(days / 30.44)} months ago`;
  return `${Math.floor(days / 365.25)} years ago`;
}

export function formatDate(iso) {
  if (!iso) return "No history";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "No history";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function normalize(text) {
  return String(text ?? "").toLocaleLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

/**
 * filters: { query, show: all|inactive|active|unknown|unavailable, kind: all|<kind>, threshold }
 */
export function matchesFilters(item, filters, now) {
  if (filters.kind && filters.kind !== "all" && item.kind !== filters.kind) return false;
  const show = filters.show || "all";
  if (show === "unavailable") {
    if (item.leavable) return false;
  } else if (show !== "all") {
    if (activityOf(item, filters.threshold, now) !== show) return false;
  }
  const q = normalize(filters.query).trim();
  if (q) {
    const hay = normalize(`${item.title} ${item.username ?? ""}`);
    if (!q.split(/\s+/).every((part) => hay.includes(part))) return false;
  }
  return true;
}

const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

function lastTime(item) {
  const t = item.last_message_at ? Date.parse(item.last_message_at) : NaN;
  return Number.isNaN(t) ? null : t;
}

/** Returns a new sorted array. Items with unknown activity always go last for activity sorts. */
export function sortItems(items, key) {
  const out = [...items];
  const byName = (a, b) => collator.compare(a.title, b.title);
  const byActivity = (dir) => (a, b) => {
    const ta = lastTime(a);
    const tb = lastTime(b);
    if (ta === null && tb === null) return byName(a, b);
    if (ta === null) return 1;
    if (tb === null) return -1;
    return dir * (ta - tb) || byName(a, b);
  };
  switch (key) {
    case "recent":
      return out.sort(byActivity(-1));
    case "name":
      return out.sort(byName);
    case "name_desc":
      return out.sort((a, b) => byName(b, a));
    case "kind":
      return out.sort((a, b) => collator.compare(KIND_LABELS[a.kind] ?? a.kind, KIND_LABELS[b.kind] ?? b.kind) || byName(a, b));
    case "oldest":
    default:
      return out.sort(byActivity(1));
  }
}

export function summarize(items, thresholdDays, now) {
  const s = { total: items.length, inactive: 0, active: 0, unknown: 0, unavailable: 0, inactiveSelectable: 0 };
  for (const it of items) {
    const a = activityOf(it, thresholdDays, now);
    s[a] += 1;
    if (!it.leavable) s.unavailable += 1;
    else if (a === "inactive") s.inactiveSelectable += 1;
  }
  return s;
}

/** Ids "Select inactive" adds: leavable and known to be inactive. Unknown activity is never included. */
export function inactiveIds(items, thresholdDays, now) {
  return items.filter((it) => it.leavable && activityOf(it, thresholdDays, now) === "inactive").map((it) => it.id);
}

export function selectableIds(items) {
  return items.filter((it) => it.leavable).map((it) => it.id);
}

/** Drop selected ids that are no longer leavable or no longer listed (e.g. after a reload). */
export function pruneSelection(selection, items) {
  const leavable = new Set(selectableIds(items));
  return new Set([...selection].filter((id) => leavable.has(id)));
}

const RETRYABLE_STATUSES = new Set(["failed", "rate_limited", "cancelled"]);

export function retryableIds(job) {
  if (!job) return [];
  return job.items.filter((it) => RETRYABLE_STATUSES.has(it.status) && it.retryable).map((it) => it.id);
}
