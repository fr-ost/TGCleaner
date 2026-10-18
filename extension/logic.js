// Pure functions for filtering, sorting and selecting items. No DOM, no chrome.* (unit tested with node --test).

export const DAY_MS = 86_400_000;
export const PRESET_THRESHOLDS = [7, 30, 90, 180];
export const MAX_THRESHOLD = 3650;

export const KIND_LABELS = {
  channel: "Channel",
  supergroup: "Supergroup",
  gigagroup: "Broadcast group",
  group: "Basic group",
  bot: "Bot",
  other: "Other",
};

export const ROLE_LABELS = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
  subscriber: "Subscriber",
  none: "",
  unknown: "",
};

/** Cleanup actions. "leave" leaves groups/channels and deletes bot chats. */
export const ACTIONS = {
  leave: { label: "Leave / remove", verb: "Leave / remove", progress: "Leaving and removing" },
  archive: { label: "Archive", verb: "Archive", progress: "Archiving" },
  mute: { label: "Mute", verb: "Mute", progress: "Muting" },
  scan: { label: "Scan", verb: "Scan", progress: "Scanning your activity" },
};

/** Mirrors the service's eligibility rules. Returns null when allowed, else the reason. */
export function ineligibility(item, action) {
  if (action === "leave") return item.leavable ? null : (item.reason || "This can't be left here.");
  if (item.accessible === false) return "You no longer have access to this.";
  if (item.kind === "other") return "This type isn't supported by this tool.";
  if (action === "archive") return item.archived ? "Already archived." : null;
  if (action === "mute") return item.muted ? "Already muted." : null;
  return "Unknown action.";
}

export const eligible = (item, action) => ineligibility(item, action) === null;

function time(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/** Days since an ISO timestamp, or null when unknown. */
export function daysSince(iso, now) {
  const t = time(iso);
  return t === null ? null : Math.max(0, (now - t) / DAY_MS);
}

/** Your most recent known engagement: the newest message you read, or your own last message. */
export function engagementIso(item) {
  const read = time(item.last_read_at);
  const mine = time(item.my_last_message_at);
  if (read === null && mine === null) return null;
  return read !== null && (mine === null || read >= mine) ? item.last_read_at : item.my_last_message_at;
}

/** The timestamp a basis looks at: "chat" = newest message in the chat, "you" = your engagement. */
export function basisIso(item, basis) {
  return basis === "you" ? engagementIso(item) : item.last_message_at;
}

/** "unknown" is its own bucket: missing data is never treated as inactivity. */
export function activityOf(item, thresholdDays, now, basis = "chat") {
  const days = daysSince(basisIso(item, basis), now);
  if (days === null) return "unknown";
  return days >= thresholdDays ? "inactive" : "active";
}

/** True when the read-only scan could still tell us something about this item. */
export function needsScan(item) {
  if (item.accessible === false || item.kind === "other" || item.scanned_at) return false;
  return !item.last_read_at || (item.can_post && !item.my_last_message_at);
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
 * filters: { query, show: all|inactive|active|unknown|unavailable, kind: all|groups|<kind>, threshold,
 *            basis: chat|you, action, muted: any|muted|unmuted, unreadOnly }
 */
export function matchesFilters(item, filters, now) {
  const kind = filters.kind || "all";
  if (kind === "groups") {
    if (!["supergroup", "gigagroup", "group"].includes(item.kind)) return false;
  } else if (kind !== "all" && item.kind !== kind) {
    return false;
  }
  if (filters.muted === "muted" && !item.muted) return false;
  if (filters.muted === "unmuted" && item.muted) return false;
  if (filters.unreadOnly && !(item.unread_count > 0)) return false;
  const show = filters.show || "all";
  if (show === "unavailable") {
    if (eligible(item, filters.action || "leave")) return false;
  } else if (show !== "all") {
    if (activityOf(item, filters.threshold, now, filters.basis) !== show) return false;
  }
  const q = normalize(filters.query).trim();
  if (q) {
    const hay = normalize(`${item.title} ${item.username ?? ""}`);
    if (!q.split(/\s+/).every((part) => hay.includes(part))) return false;
  }
  return true;
}

const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

/** Returns a new sorted array. Items with unknown activity always go last for activity sorts. */
export function sortItems(items, key, basis = "chat") {
  const out = [...items];
  const byName = (a, b) => collator.compare(a.title, b.title);
  const byActivity = (dir) => (a, b) => {
    const ta = time(basisIso(a, basis));
    const tb = time(basisIso(b, basis));
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
    case "unread":
      return out.sort((a, b) => (b.unread_count ?? 0) - (a.unread_count ?? 0) || byName(a, b));
    case "oldest":
    default:
      return out.sort(byActivity(1));
  }
}

export function summarize(items, thresholdDays, now, basis = "chat", action = "leave") {
  const s = { total: items.length, inactive: 0, active: 0, unknown: 0, unavailable: 0, inactiveSelectable: 0, needsScan: 0, bots: 0 };
  for (const it of items) {
    const a = activityOf(it, thresholdDays, now, basis);
    s[a] += 1;
    if (!eligible(it, action)) s.unavailable += 1;
    else if (a === "inactive") s.inactiveSelectable += 1;
    if (needsScan(it)) s.needsScan += 1;
    if (it.kind === "bot") s.bots += 1;
  }
  return s;
}

/** Ids "Select inactive" adds: eligible for the action and known to be inactive. Unknown is never included. */
export function inactiveIds(items, thresholdDays, now, basis = "chat", action = "leave") {
  return items
    .filter((it) => eligible(it, action) && activityOf(it, thresholdDays, now, basis) === "inactive")
    .map((it) => it.id);
}

export function selectableIds(items, action = "leave") {
  return items.filter((it) => eligible(it, action)).map((it) => it.id);
}

/** Drop selected ids that are no longer listed or no longer eligible for the action. */
export function pruneSelection(selection, items, action = "leave") {
  const ok = new Set(selectableIds(items, action));
  return new Set([...selection].filter((id) => ok.has(id)));
}

const RETRYABLE_STATUSES = new Set(["failed", "rate_limited", "cancelled"]);

export function retryableIds(job) {
  if (!job) return [];
  return job.items.filter((it) => RETRYABLE_STATUSES.has(it.status) && it.retryable).map((it) => it.id);
}

/** Rough duration of a sequential job, in seconds. */
export function estimateSeconds(count, perItemSeconds) {
  return Math.ceil(count * perItemSeconds);
}

export function formatDuration(seconds) {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 90 ? `${minutes} min` : `${(minutes / 60).toFixed(1)} h`;
}
