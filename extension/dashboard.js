import { ApiError, api, currentPort, loadPort, savePort } from "./api.js";
import {
  ACTIONS, KIND_LABELS, PRESET_THRESHOLDS, ROLE_LABELS, activityOf, daysSince, eligible, engagementIso,
  estimateSeconds, formatAgo, formatDate, formatDuration, inactiveIds, ineligibility, matchesFilters, needsScan,
  parseThreshold, pruneSelection, retryableIds, selectableIds, sortItems, summarize,
} from "./logic.js";

const $ = (id) => document.getElementById(id);

const DEFAULT_PREFS = {
  threshold: 90, thresholdChoice: "90", show: "all", kind: "all", sort: "oldest",
  basis: "chat", action: "leave", muted: "any", unreadOnly: false,
};
const SCAN_SECONDS_PER_ITEM = 1.6; // pause + up to two small requests

const state = {
  status: null,
  items: [],
  byId: new Map(),
  excluded: null,
  loaded: false,
  loading: false,
  selection: new Set(),
  prefs: { ...DEFAULT_PREFS },
  query: "",
  visible: [],
  lastClicked: null,
  job: null,
  jobResults: new Map(),
  dismissedJob: null,
  pending: null, // prepared action awaiting confirmation
};

const rows = new Map(); // item id -> row element
let offlineTimer = null;
let jobTimer = null;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

let toastTimer = null;
function toast(message, ms = 4000) {
  const t = $("toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

function errorText(e) {
  return e instanceof ApiError ? e.message : `Unexpected error: ${e?.message ?? e}`;
}

async function withBusy(button, fn) {
  const was = button.disabled;
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.disabled = was;
  }
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

const jobActive = () => Boolean(state.job && ["running", "waiting"].includes(state.job.state));
const action = () => state.prefs.action;
const actionWord = (act) => ACTIONS[act].label.toLowerCase();

// ---------------------------------------------------------------------------
// preferences (harmless UI settings only)
// ---------------------------------------------------------------------------

async function loadPrefs() {
  try {
    const { prefs } = await chrome.storage.local.get("prefs");
    if (prefs && typeof prefs === "object") state.prefs = { ...DEFAULT_PREFS, ...prefs };
  } catch {
    // defaults
  }
  state.prefs.threshold = parseThreshold(state.prefs.threshold) ?? DEFAULT_PREFS.threshold;
  if (!["leave", "archive", "mute"].includes(state.prefs.action)) state.prefs.action = "leave";
}

function savePrefs() {
  chrome.storage.local.set({ prefs: state.prefs }).catch(() => {});
}

function applyPrefsToControls() {
  const p = state.prefs;
  const preset = PRESET_THRESHOLDS.includes(p.threshold) && p.thresholdChoice !== "custom";
  $("thresholdSelect").value = preset ? String(p.threshold) : "custom";
  $("customField").hidden = preset;
  $("customDays").value = p.threshold;
  $("showSelect").value = p.show;
  $("kindSelect").value = p.kind;
  $("sortSelect").value = p.sort;
  $("basisSelect").value = p.basis;
  $("actionSelect").value = p.action;
  $("mutedSelect").value = p.muted;
  $("unreadOnly").checked = Boolean(p.unreadOnly);
}

// ---------------------------------------------------------------------------
// views & connection status
// ---------------------------------------------------------------------------

function showView(name) {
  for (const v of ["offline", "login", "app"]) $(`${v}View`).hidden = v !== name;
  $("actionBar").hidden = name !== "app";
}

function setConn(kind, text) {
  $("connDot").className = `dot ${kind}`;
  $("connText").textContent = text;
}

async function refreshStatus() {
  clearTimeout(offlineTimer);
  let status;
  try {
    status = await api("/api/status");
  } catch (e) {
    return showOffline(e);
  }
  state.status = status;
  $("demoBadge").hidden = !status.demo;
  $("disconnectBtn").hidden = !status.authorized;

  if (!status.telegram_reachable) {
    // The service is up but not (yet) connected to Telegram: wait and poll instead of offering a login.
    setConn("warn", status.error?.startsWith("Connecting") ? "Connecting to Telegram…" : "Telegram unreachable");
    showView("login");
    showLoginStep(null);
    $("loginError").textContent = `${status.error ?? "Cannot reach Telegram."} Retrying…`;
    offlineTimer = setTimeout(refreshStatus, 3000);
    return;
  }

  if (!status.authorized) {
    setConn("warn", "Telegram not connected");
    showView("login");
    showLoginStep(status.login_step ?? "phone", { phone: status.login_phone });
    $("loginError").textContent = "";
    return;
  }

  const a = status.account;
  setConn("ok", a ? `Connected as ${a.name}${a.username ? ` (@${a.username})` : ""}` : "Connected");
  $("connText").title = a?.phone ? `Phone ${a.phone}` : "";
  showView("app");
  await refreshJob();
  if (!state.loaded) await loadDialogs();
  else render();
}

function showOffline(e) {
  showView("offline");
  $("demoBadge").hidden = true;
  $("disconnectBtn").hidden = true;
  $("portInput").value = currentPort();
  if (e instanceof ApiError && e.code === "origin_not_allowed") {
    setConn("bad", "Service rejected this extension");
    $("offlineDetail").textContent =
      `The service is running but does not accept this extension's ID (${chrome.runtime.id}). ` +
      "Run setup again and enter this ID, then restart the service.";
  } else if (e instanceof ApiError && e.code === "service_offline") {
    setConn("bad", "Local service offline");
    $("offlineDetail").textContent = `Nothing is answering on 127.0.0.1:${currentPort()}. Retrying every few seconds…`;
  } else {
    setConn("bad", "Local service error");
    $("offlineDetail").textContent = errorText(e);
  }
  offlineTimer = setTimeout(refreshStatus, 3000);
}

// ---------------------------------------------------------------------------
// login
// ---------------------------------------------------------------------------

function showLoginStep(step, info = {}) {
  $("phoneForm").hidden = step !== "phone";
  $("codeForm").hidden = step !== "code";
  $("passwordForm").hidden = step !== "password";
  if (step === "code") {
    $("codeHint").textContent = info.delivery
      ? `Telegram sent a code via ${info.delivery}.`
      : `Enter the code Telegram sent${info.phone ? ` to ${info.phone}` : ""}.`;
    $("codeInput").focus();
  } else if (step === "password") {
    $("passwordHint").textContent = info.hint ? `Hint: ${info.hint}` : "Your account has two-step verification enabled.";
    $("passwordInput").focus();
  } else if (step === "phone") {
    $("phoneInput").focus();
  }
}

async function loginSubmit(event, path, field, input) {
  event.preventDefault();
  const button = event.submitter ?? event.target.querySelector("button[type=submit]");
  $("loginError").textContent = "";
  const value = input.value;
  try {
    const result = await withBusy(button, () => api(path, { method: "POST", body: { [field]: value } }));
    if (field !== "phone") input.value = "";
    if (result.step === "done") {
      $("phoneInput").value = "";
      toast("Telegram connected.");
      await refreshStatus();
    } else {
      showLoginStep(result.step, result);
    }
  } catch (e) {
    if (field === "password") input.value = "";
    $("loginError").textContent = errorText(e);
    if (e.code === "code_expired" || e.code === "no_login") showLoginStep("phone");
  }
}

async function restartLogin() {
  try {
    await api("/api/login/cancel", { method: "POST", body: {} });
  } catch {
    // ignore
  }
  $("codeInput").value = "";
  $("passwordInput").value = "";
  $("loginError").textContent = "";
  showLoginStep("phone");
}

async function disconnect() {
  try {
    const result = await api("/api/logout", { method: "POST", body: {} });
    toast(result.telegram_confirmed
      ? "Disconnected. The session was ended and deleted."
      : "Local session deleted. Telegram couldn't confirm; check Settings → Devices in Telegram.", 7000);
  } catch (e) {
    toast(errorText(e), 6000);
    return;
  }
  state.items = [];
  state.byId.clear();
  rows.clear();
  state.loaded = false;
  state.selection.clear();
  state.job = null;
  state.jobResults.clear();
  $("list").replaceChildren();
  $("progressPanel").hidden = true;
  await refreshStatus();
}

// ---------------------------------------------------------------------------
// item list
// ---------------------------------------------------------------------------

/** cached=true re-reads the service's copy (after a job) instead of downloading the list from Telegram again. */
async function loadDialogs({ cached = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (!cached) {
    $("loadingState").hidden = false;
    $("emptyState").hidden = true;
    $("loadError").hidden = true;
  }
  $("reloadBtn").disabled = true;
  try {
    const data = await api(cached ? "/api/dialogs?cached=1" : "/api/dialogs", { timeoutMs: 600_000 });
    state.items = data.items;
    state.byId = new Map(data.items.map((it) => [it.id, it]));
    state.excluded = data.excluded;
    state.loaded = true;
    rows.clear();
    state.selection = pruneSelection(state.selection, state.items, action());
    state.lastClicked = null;
    $("loadError").hidden = true;
  } catch (e) {
    if (e.status === 401) {
      state.loading = false;
      return refreshStatus();
    }
    // Keep the reason on screen (not just a toast) so a failed load is never silent.
    if (!state.loaded) {
      $("loadErrorText").textContent = e.code === "service_offline"
        ? "The local service stopped answering. Check its Terminal window, then try again."
        : errorText(e);
      $("loadError").hidden = false;
    } else {
      toast(errorText(e), 7000);
    }
  } finally {
    state.loading = false;
    $("loadingState").hidden = true;
    $("reloadBtn").disabled = jobActive();
  }
  render();
}

function filters() {
  const p = state.prefs;
  return {
    query: state.query, show: p.show, kind: p.kind, threshold: p.threshold, basis: p.basis,
    action: p.action, muted: p.muted, unreadOnly: p.unreadOnly,
  };
}

function computeVisible() {
  const now = Date.now();
  const f = filters();
  state.visible = sortItems(state.items.filter((it) => matchesFilters(it, f, now)), state.prefs.sort, state.prefs.basis);
}

function render() {
  computeVisible();
  renderStats();
  renderList();
  renderActionBar();
}

function renderStats() {
  const p = state.prefs;
  const s = summarize(state.items, p.threshold, Date.now(), p.basis, p.action);
  $("statTotal").textContent = state.loaded ? s.total : "–";
  $("statInactive").textContent = state.loaded ? s.inactive : "–";
  $("statInactiveLabel").textContent = p.basis === "you"
    ? `Not read or posted ${p.threshold}+ days`
    : `No messages ${p.threshold}+ days`;
  $("statUnknown").textContent = state.loaded ? s.unknown : "–";
  $("statSelected").textContent = state.selection.size;
  $("selectInactiveBtn").title = p.basis === "you"
    ? `Select every shown item you haven't read or posted in for ${p.threshold}+ days. Unknown activity is never included.`
    : `Select every shown item whose last message is ${p.threshold}+ days old. Unknown activity is never included.`;
  $("unavailableOption").textContent = `Can't ${actionWord(p.action)}`;

  const ex = state.excluded;
  $("excludedNote").textContent = ex
    ? `${s.bots ? `Includes ${plural(s.bots, "bot")}. ` : ""}Never shown or touched: ${plural(ex.private_chat ?? 0, "private chat")} with people` +
      `${ex.saved_messages ? " and Saved Messages" : ""}. ` +
      `${ex.unreadable ? `${plural(ex.unreadable, "chat")} couldn't be read and ${ex.unreadable === 1 ? "was" : "were"} skipped (details in the service window). ` : ""}` +
      `${s.unavailable} listed item${s.unavailable === 1 ? "" : "s"} can't be ` +
      `${p.action === "leave" ? "left or removed" : `${p.action}d`} (reason shown on each).`
    : "";
  renderScanBanner(s.needsScan);
}

function renderScanBanner(needing) {
  const show = state.loaded && state.prefs.basis === "you" && needing > 0 && !jobActive();
  $("scanBanner").hidden = !show;
  $("scanBtn").disabled = jobActive() || !state.loaded;
  if (!show) return;
  const eta = formatDuration(estimateSeconds(needing, SCAN_SECONDS_PER_ITEM));
  $("scanBannerTitle").textContent = `Scan your activity in ${plural(needing, "chat")}`;
  $("scanBannerText").textContent =
    "For these, the chat list alone doesn't tell when you last read or posted, so busy chats you never open can't be " +
    `found yet. The scan is read-only (it changes nothing), takes about ${eta}, and is remembered on your computer.`;
}

function renderList() {
  const list = $("list");
  const now = Date.now();
  const fragment = document.createDocumentFragment();
  for (const item of state.visible) {
    let row = rows.get(item.id);
    if (!row) {
      row = makeRow(item);
      rows.set(item.id, row);
    }
    updateRow(row, item, now);
    fragment.append(row);
  }
  list.replaceChildren(fragment);
  $("emptyState").hidden = !state.loaded || state.visible.length > 0;
  $("shownCount").textContent = state.loaded ? `Showing ${state.visible.length} of ${state.items.length}` : "";
}

function makeRow(item) {
  const checkbox = el("input", { type: "checkbox", "aria-label": `Select ${item.title}` });
  const sub = el("div", { class: "sub" });
  if (item.link && item.link.startsWith("https://t.me/")) {
    sub.append(el("a", { href: item.link, target: "_blank", rel: "noopener noreferrer", text: `@${item.username}` }));
  }
  const role = ROLE_LABELS[item.role];
  if (item.role === "owner" || item.role === "admin") sub.append(el("span", { class: "tag", text: role }));
  sub.append(el("span", { class: "tag tag-archived", text: "Archived" }), el("span", { class: "tag tag-muted", text: "Muted" }));
  if (item.participants_count) sub.append(el("span", { text: `${item.participants_count.toLocaleString()} members` }));
  if (item.unread_count) sub.append(el("span", { class: "unread", text: `${item.unread_count.toLocaleString()} unread` }));

  return el("div", { class: "item", dataset: { id: item.id }, role: "row" },
    el("div", { class: "cb" }, checkbox),
    el("div", { class: "title-cell" }, el("div", { class: "title", text: item.title, title: item.title }), sub),
    el("div", { class: "kind", text: KIND_LABELS[item.kind] ?? item.kind }),
    el("div", { class: "when chat-when" }, el("span", { class: "ago" }), el("span", { class: "date muted" })),
    el("div", { class: "when you-when" }, el("span", { class: "ago" }), el("span", { class: "date muted" })),
    el("div", { class: "status-cell" }),
  );
}

const RESULT_LABELS = {
  pending: ["Queued", "info"],
  in_progress: ["Working…", "info spin"],
  left: ["Left ✓", "ok"],
  removed: ["Deleted ✓", "ok"],
  not_member: ["Not a member", "ok"],
  archived: ["Archived ✓", "ok"],
  muted: ["Muted ✓", "ok"],
  scanned: ["Scanned", "ok"],
  failed: ["Failed", "bad"],
  rate_limited: ["Rate limited", "warn"],
  cancelled: ["Not attempted", "muted"],
};

/** Second line of the "You" column: what we know about your reading and posting. */
function youDetail(item, now) {
  const read = item.last_read_at ? `read ${formatAgo(daysSince(item.last_read_at, now)).toLowerCase()}` : null;
  let posted = null;
  if (item.can_post) {
    if (item.my_last_message_at) posted = `posted ${formatAgo(daysSince(item.my_last_message_at, now)).toLowerCase()}`;
    else if (item.scanned_at) posted = "never posted";
  }
  const parts = [read, posted].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  if (item.scanned_at) return item.unread_count ? "never read" : "no record";
  return needsScan(item) ? "not scanned yet" : "—";
}

function updateRow(row, item, now) {
  const p = state.prefs;
  const selected = state.selection.has(item.id);
  const ok = eligible(item, p.action);
  row.classList.toggle("selected", selected);
  row.classList.toggle("disabled", !ok);
  row.classList.toggle("is-archived", Boolean(item.archived));
  row.classList.toggle("is-muted", Boolean(item.muted));
  const cb = row.querySelector("input");
  cb.checked = selected;
  cb.disabled = !ok || jobActive();

  // "Last message" column: activity of the chat itself.
  const chatActivity = activityOf(item, p.threshold, now, "chat");
  const chat = row.querySelector(".chat-when .ago");
  chat.textContent = formatAgo(daysSince(item.last_message_at, now));
  chat.className = `ago ${p.basis === "chat" ? chatActivity : "plain"}`;
  chat.title = chatActivity === "unknown"
    ? "Telegram returned no message history, so activity is unknown (not assumed inactive)."
    : `Newest message: ${new Date(item.last_message_at).toLocaleString()}`;
  row.querySelector(".chat-when .date").textContent = item.last_message_at ? formatDate(item.last_message_at) : "No history available";

  // "You" column: your own engagement.
  const youActivity = activityOf(item, p.threshold, now, "you");
  const you = row.querySelector(".you-when .ago");
  you.textContent = formatAgo(daysSince(engagementIso(item), now));
  you.className = `ago ${p.basis === "you" ? youActivity : "plain"}`;
  you.title = "When you last read here (date of the newest message you've read) or posted here.";
  row.querySelector(".you-when .date").textContent = youDetail(item, now);

  const cell = row.querySelector(".status-cell");
  const result = state.jobResults.get(item.id);
  const reason = ineligibility(item, p.action);
  if (result && result.status !== "scanned") {
    const [label, cls] = RESULT_LABELS[result.status] ?? [result.status, "muted"];
    cell.replaceChildren(el("span", { class: `pill ${cls}`, text: label, title: result.message ?? "" }));
    if (result.message && !["left", "pending", "in_progress", "archived", "muted"].includes(result.status)) {
      cell.append(el("div", { class: "reason", text: result.message, title: result.message }));
    }
  } else if (reason) {
    cell.replaceChildren(
      el("span", { class: "pill muted", text: `Can't ${actionWord(p.action)}` }),
      el("div", { class: "reason", text: reason, title: reason }),
    );
  } else {
    cell.replaceChildren();
  }
}

function refreshRows() {
  const now = Date.now();
  for (const item of state.visible) {
    const row = rows.get(item.id);
    if (row) updateRow(row, item, now);
  }
  renderStats();
  renderActionBar();
}

function renderActionBar() {
  const n = state.selection.size;
  const visibleSelected = state.visible.filter((it) => state.selection.has(it.id)).length;
  const hidden = n - visibleSelected;
  $("selectionSummary").textContent = n === 0
    ? "Nothing selected"
    : `${plural(n, "item")} selected${hidden > 0 ? ` (${hidden} hidden by filters)` : ""}`;
  const btn = $("applyBtn");
  btn.disabled = n === 0 || jobActive();
  btn.textContent = `${ACTIONS[action()].verb} selected${n ? ` (${n})` : ""}`;
  btn.className = `btn ${action() === "leave" ? "btn-danger" : "btn-primary"}`;
  const lock = jobActive();
  for (const id of ["selectInactiveBtn", "selectAllBtn", "deselectAllBtn", "reloadBtn", "actionSelect"]) $(id).disabled = lock;
}

function setSelected(ids, value) {
  for (const id of ids) {
    if (value) state.selection.add(id);
    else state.selection.delete(id);
  }
  refreshRows();
}

function onListClick(event) {
  if (event.target.closest("a")) return;
  const row = event.target.closest(".item");
  if (!row || jobActive()) return;
  const item = state.byId.get(row.dataset.id);
  if (!item || !eligible(item, action())) return;
  const index = state.visible.indexOf(item);
  const value = !state.selection.has(item.id);
  if (event.shiftKey && state.lastClicked !== null) {
    // Shift+click selects/deselects the whole range in the current view.
    const from = Math.min(state.lastClicked, index);
    const to = Math.max(state.lastClicked, index);
    setSelected(selectableIds(state.visible.slice(from, to + 1), action()), value);
  } else {
    setSelected([item.id], value);
  }
  state.lastClicked = index;
  if (event.target.matches("input[type=checkbox]")) event.target.checked = state.selection.has(item.id);
}

// ---------------------------------------------------------------------------
// actions: prepare -> confirm -> start
// ---------------------------------------------------------------------------

const CONFIRM_TEXT = {
  leave: {
    title: (n) => `Leave or remove ${plural(n, "chat")}?`,
    intro: "You will leave these groups and channels and delete these bot chats, one at a time. Nothing has happened yet.",
    note: "Leaving can't be undone here: to rejoin a private group or channel you need a new invite link. " +
      "Bot chats are deleted on your side only.",
    ok: (n) => `Leave / remove ${n}`,
  },
  archive: {
    title: (n) => `Archive ${plural(n, "chat")}?`,
    intro: "These move to Telegram's Archive folder. You stay a member. Nothing has happened yet.",
    note: "Unarchive any time in Telegram. Archived chats that aren't muted return to the main list when a new message arrives.",
    ok: (n) => `Archive ${n}`,
  },
  mute: {
    title: (n) => `Mute ${plural(n, "chat")}?`,
    intro: "Notifications for these are turned off forever. You stay a member. Nothing has happened yet.",
    note: "Unmute any time in Telegram.",
    ok: (n) => `Mute ${n}`,
  },
};

async function prepareAndConfirm(act, ids, button) {
  let prepared;
  try {
    prepared = await withBusy(button, () => api("/api/actions/prepare", { method: "POST", body: { action: act, ids } }));
  } catch (e) {
    toast(errorText(e), 7000);
    return;
  }
  state.pending = prepared;
  const text = CONFIRM_TEXT[act];
  const n = prepared.items.length;
  const bots = prepared.items.filter((it) => it.kind === "bot").length;
  $("confirmTitle").textContent = n ? text.title(n) : "Nothing to do";
  $("confirmIntro").textContent = n ? text.intro : "None of the selected items qualify for this action.";
  $("confirmList").replaceChildren(...prepared.items.map((it) =>
    el("li", {}, el("span", { text: it.title }), el("span", { class: "k", text: KIND_LABELS[it.kind] ?? it.kind }))));
  $("confirmList").hidden = n === 0;
  $("confirmRejectedWrap").hidden = prepared.rejected.length === 0;
  $("confirmRejected").replaceChildren(...prepared.rejected.map((r) =>
    el("li", {}, el("strong", { text: r.title ?? r.id }), el("span", { class: "k", text: ` — ${r.reason ?? "not eligible"}` }))));
  $("blockBotsWrap").hidden = !(act === "leave" && bots > 0);
  $("blockBots").checked = false;
  $("blockBotsLabel").textContent = `Also block ${bots === 1 ? "this bot" : `these ${bots} bots`} (they can't message you again)`;
  $("confirmNote").textContent = n ? text.note : "";
  $("confirmOk").hidden = n === 0;
  $("confirmOk").disabled = false;
  $("confirmOk").textContent = text.ok(n);
  $("confirmOk").className = `btn ${act === "leave" ? "btn-danger" : "btn-primary"}`;
  $("confirmError").textContent = "";
  $("confirmDialog").showModal();
}

async function confirmAction(event) {
  event.preventDefault();
  const pending = state.pending;
  if (!pending?.token) return;
  const button = $("confirmOk");
  button.disabled = true; // a second click can't start anything: the token is single-use anyway
  try {
    const options = { block_bots: !$("blockBotsWrap").hidden && $("blockBots").checked };
    const job = await api("/api/actions/start", { method: "POST", body: { token: pending.token, options } });
    state.pending = null;
    $("confirmDialog").close("started");
    for (const it of pending.items) state.selection.delete(it.id);
    startedJob(job);
  } catch (e) {
    $("confirmError").textContent = errorText(e);
  }
}

async function startScan(ids, button) {
  try {
    const job = await withBusy(button, () => api("/api/scan/start", { method: "POST", body: ids ? { ids } : {} }));
    startedJob(job);
  } catch (e) {
    toast(errorText(e), 7000);
  }
}

function startedJob(job) {
  state.dismissedJob = null;
  setJob(job);
  pollJob();
}

// ---------------------------------------------------------------------------
// job progress
// ---------------------------------------------------------------------------

async function refreshJob() {
  try {
    const { job } = await api("/api/job");
    if (job) setJob(job);
    if (jobActive()) pollJob();
  } catch {
    // shown elsewhere
  }
}

function setJob(job) {
  state.job = job;
  state.jobResults = new Map(job.items.map((it) => [it.id, it]));
  renderProgress();
  if (state.loaded) refreshRows();
}

function pollJob() {
  clearTimeout(jobTimer);
  jobTimer = setTimeout(async () => {
    try {
      const { job } = await api("/api/job");
      if (job) setJob(job);
    } catch (e) {
      $("progressSub").textContent = `${errorText(e)} Still trying…`;
    }
    if (jobActive() || !state.job) pollJob();
    else onJobFinished();
  }, 1000);
}

async function onJobFinished() {
  const job = state.job;
  const c = job.counts;
  const done = Object.entries(c)
    .filter(([k]) => !["failed", "rate_limited", "cancelled"].includes(k))
    .reduce((sum, [, v]) => sum + v, 0);
  toast(`${ACTIONS[job.kind]?.label ?? "Job"} finished: ${done} done, ${c.failed ?? 0} failed.`, 6000);
  // Pick up what changed (left, archived, muted, scan results) from the service's copy of the list.
  await loadDialogs({ cached: true });
  renderProgress();
}

function countChip(count, label, cls) {
  return count ? el("span", { class: `pill ${cls}`, text: `${label}: ${count}` }) : null;
}

let lastProgressKey = "";

function renderProgress() {
  const job = state.job;
  const panel = $("progressPanel");
  if (!job || state.dismissedJob === job.id) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const c = job.counts;
  const active = jobActive();
  const meta = ACTIONS[job.kind] ?? ACTIONS.leave;
  const problems = (c.failed ?? 0) + (c.rate_limited ?? 0) + (c.cancelled ?? 0);
  panel.classList.toggle("done", !active && problems === 0);
  panel.classList.toggle("problem", !active && problems > 0);

  let title;
  let sub = "";
  if (job.state === "running") {
    title = `${meta.progress}… ${job.processed} of ${job.total}`;
    sub = job.cancel_requested
      ? "Cancelling: stopping after the current item."
      : "One item at a time with a short pause between them. You can close this tab; the service keeps going.";
  } else if (job.state === "waiting") {
    const secs = Math.max(0, Math.ceil(job.wait_until - Date.now() / 1000));
    title = `Paused by Telegram rate limit (${job.processed} of ${job.total} done)`;
    sub = `Telegram asked us to wait. Resuming automatically in ${secs} s.`;
  } else if (job.state === "stopped_rate_limited") {
    title = `Stopped: Telegram rate limit (${job.processed - (c.rate_limited ?? 0)} of ${job.total} processed)`;
    const at = job.retry_after ? new Date(job.retry_after * 1000).toLocaleTimeString() : "later";
    sub = `Telegram asked for a long pause, so the rest were not attempted. You can retry them after ${at}.`;
  } else if (job.state === "cancelled") {
    title = `${meta.label} cancelled`;
    sub = "Items not yet attempted were left untouched.";
  } else {
    title = `${meta.label}: ${problems ? "finished with some problems" : "finished"}`;
    sub = job.kind === "scan"
      ? "Set “Inactive means” to “You haven't read or posted” to find chats you ignore."
      : "Every status below is Telegram's actual answer.";
  }
  $("progressTitle").textContent = title;
  $("progressSub").textContent = sub;
  $("progressBar").style.width = `${job.total ? (100 * job.processed) / job.total : 0}%`;
  $("progressCounts").replaceChildren(...[
    countChip(c.left, "Left", "ok"),
    countChip(c.removed, "Bot chats deleted", "ok"),
    countChip(c.not_member, "Already not a member", "ok"),
    countChip(c.archived, "Archived", "ok"),
    countChip(c.muted, "Muted", "ok"),
    countChip(c.scanned, "Scanned", "ok"),
    countChip(c.failed, "Failed", "bad"),
    countChip(c.rate_limited, "Rate limited", "warn"),
    countChip(c.cancelled, "Not attempted", "muted"),
    countChip((c.pending ?? 0) + (c.in_progress ?? 0), "Remaining", "info"),
  ].filter(Boolean));

  $("cancelJobBtn").hidden = !active;
  $("cancelJobBtn").disabled = job.cancel_requested;
  const retry = retryableIds(job);
  $("retryBtn").hidden = active || retry.length === 0;
  $("retryBtn").textContent = `Retry ${retry.length} eligible`;
  $("dismissJobBtn").hidden = active;

  const key = `${job.id}:${job.processed}:${job.state}:${$("problemsOnly").checked}`;
  if (key !== lastProgressKey) {
    lastProgressKey = key;
    const problemsOnly = $("problemsOnly").checked;
    const shown = job.items.filter((it) => !problemsOnly || ["failed", "rate_limited", "cancelled"].includes(it.status));
    $("progressList").replaceChildren(...shown.map((it) => {
      const [label, cls] = RESULT_LABELS[it.status] ?? [it.status, "muted"];
      return el("li", {},
        el("span", { class: `pill ${cls}`, text: label }), " ",
        el("span", { text: it.title }),
        it.message && !["left", "archived", "muted"].includes(it.status) ? el("span", { class: "msg", text: it.message }) : null);
    }));
  }
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function setPref(key, value) {
  state.prefs[key] = value;
  savePrefs();
}

function bind() {
  $("portForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await savePort($("portInput").value);
      await refreshStatus();
    } catch (err) {
      $("offlineDetail").textContent = err.message;
    }
  });

  $("phoneForm").addEventListener("submit", (e) => loginSubmit(e, "/api/login/phone", "phone", $("phoneInput")));
  $("codeForm").addEventListener("submit", (e) => loginSubmit(e, "/api/login/code", "code", $("codeInput")));
  $("passwordForm").addEventListener("submit", (e) => loginSubmit(e, "/api/login/password", "password", $("passwordInput")));
  for (const b of document.querySelectorAll("[data-action=restart-login]")) b.addEventListener("click", restartLogin);

  $("disconnectBtn").addEventListener("click", () => $("disconnectDialog").showModal());
  $("disconnectDialog").addEventListener("close", () => {
    if ($("disconnectDialog").returnValue === "confirm") disconnect();
  });

  $("searchInput").addEventListener("input", debounce((e) => {
    state.query = e.target.value;
    render();
  }, 120));
  $("thresholdSelect").addEventListener("change", (e) => {
    const v = e.target.value;
    setPref("thresholdChoice", v);
    $("customField").hidden = v !== "custom";
    if (v === "custom") {
      $("customDays").focus();
    } else {
      setPref("threshold", Number(v));
      $("customDays").value = v;
    }
    render();
  });
  $("customDays").addEventListener("input", debounce((e) => {
    const n = parseThreshold(e.target.value);
    if (n === null) return;
    setPref("threshold", n);
    render();
  }, 250));
  for (const [id, key] of [["showSelect", "show"], ["kindSelect", "kind"], ["sortSelect", "sort"],
    ["basisSelect", "basis"], ["mutedSelect", "muted"]]) {
    $(id).addEventListener("change", (e) => {
      setPref(key, e.target.value);
      render();
    });
  }
  $("unreadOnly").addEventListener("change", (e) => {
    setPref("unreadOnly", e.target.checked);
    render();
  });
  $("actionSelect").addEventListener("change", (e) => {
    setPref("action", e.target.value);
    const before = state.selection.size;
    state.selection = pruneSelection(state.selection, state.items, action());
    const dropped = before - state.selection.size;
    if (dropped) {
      toast(`${plural(dropped, "selected item")} can't be ${actionWord(action())}d, so ${dropped === 1 ? "it was" : "they were"} deselected.`);
    }
    render();
  });

  $("selectInactiveBtn").addEventListener("click", () => {
    const p = state.prefs;
    const ids = inactiveIds(state.visible, p.threshold, Date.now(), p.basis, p.action);
    setSelected(ids, true);
    toast(ids.length ? `Selected ${plural(ids.length, "inactive item")}.` : "No eligible inactive items in this view.");
  });
  $("selectAllBtn").addEventListener("click", () => setSelected(selectableIds(state.visible, action()), true));
  $("deselectAllBtn").addEventListener("click", () => {
    state.selection.clear();
    refreshRows();
  });
  $("reloadBtn").addEventListener("click", () => loadDialogs());
  $("loadRetryBtn").addEventListener("click", () => loadDialogs());
  $("scanBtn").addEventListener("click", (e) => startScan(null, e.currentTarget));
  $("scanBannerBtn").addEventListener("click", (e) => startScan(null, e.currentTarget));
  $("list").addEventListener("click", onListClick);

  $("applyBtn").addEventListener("click", (e) => prepareAndConfirm(action(), [...state.selection], e.currentTarget));
  $("confirmOk").addEventListener("click", confirmAction);
  $("confirmDialog").addEventListener("close", () => {
    if ($("confirmDialog").returnValue !== "started") state.pending = null;
  });

  $("cancelJobBtn").addEventListener("click", async (e) => {
    try {
      const job = await withBusy(e.currentTarget, () => api("/api/job/cancel", { method: "POST", body: {} }));
      setJob(job);
    } catch (err) {
      toast(errorText(err));
    }
  });
  $("retryBtn").addEventListener("click", (e) => {
    const job = state.job;
    if (job.kind === "scan") startScan(retryableIds(job), e.currentTarget);
    else prepareAndConfirm(job.kind, retryableIds(job), e.currentTarget);
  });
  $("dismissJobBtn").addEventListener("click", () => {
    state.dismissedJob = state.job?.id ?? null;
    state.jobResults.clear();
    renderProgress();
    if (state.loaded) render();
  });
  $("problemsOnly").addEventListener("change", renderProgress);
}

async function init() {
  bind();
  await Promise.all([loadPrefs(), loadPort()]);
  applyPrefsToControls();
  await refreshStatus();
}

init();
