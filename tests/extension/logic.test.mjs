// Run: node --test tests/extension
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activityOf, daysSince, engagementIso, formatAgo, formatDuration, inactiveIds, ineligibility, matchesFilters,
  needsScan, parseThreshold, pruneSelection, retryableIds, selectableIds, sortItems, summarize,
} from "../../extension/logic.js";

const NOW = Date.parse("2026-10-01T00:00:00Z");
const daysAgo = (d) => new Date(NOW - d * 86_400_000).toISOString();

const items = [
  { id: "-1", title: "Python Chat", username: "pychat", kind: "supergroup", last_message_at: daysAgo(2), leavable: true },
  { id: "-2", title: "Old News", username: null, kind: "channel", last_message_at: daysAgo(400), leavable: true },
  { id: "-3", title: "No History", username: null, kind: "group", last_message_at: null, leavable: true },
  { id: "-4", title: "My Own Group", username: null, kind: "supergroup", last_message_at: daysAgo(100), leavable: false },
  { id: "-5", title: "Café Crème", username: "cafe", kind: "channel", last_message_at: daysAgo(30), leavable: true },
];

test("unknown activity is its own bucket", () => {
  assert.equal(activityOf(items[2], 7, NOW), "unknown");
  assert.equal(activityOf({ last_message_at: "garbage" }, 7, NOW), "unknown");
  assert.equal(daysSince(null, NOW), null);
});

test("threshold boundary counts as inactive", () => {
  assert.equal(activityOf(items[4], 30, NOW), "inactive");
  assert.equal(activityOf(items[4], 31, NOW), "active");
});

test("Select inactive never includes unknown or unleavable items", () => {
  assert.deepEqual(inactiveIds(items, 7, NOW), ["-2", "-5"]);
  assert.deepEqual(inactiveIds(items, 90, NOW), ["-2"]);
  assert.deepEqual(selectableIds(items), ["-1", "-2", "-3", "-5"]);
});

test("filters: show, kind and accent-insensitive multi-word search", () => {
  const f = (over) => items.filter((it) => matchesFilters(it, { query: "", show: "all", kind: "all", threshold: 30, ...over }, NOW)).map((it) => it.id);
  assert.deepEqual(f({ show: "inactive" }), ["-2", "-4", "-5"]);
  assert.deepEqual(f({ show: "active" }), ["-1"]);
  assert.deepEqual(f({ show: "unknown" }), ["-3"]);
  assert.deepEqual(f({ show: "unavailable" }), ["-4"]);
  assert.deepEqual(f({ kind: "channel" }), ["-2", "-5"]);
  assert.deepEqual(f({ query: "cafe creme" }), ["-5"]);
  assert.deepEqual(f({ query: "PYCHAT" }), ["-1"]);
  assert.deepEqual(f({ query: "news old" }), ["-2"]);
});

test("sorting puts unknown activity last", () => {
  assert.deepEqual(sortItems(items, "oldest").map((i) => i.id), ["-2", "-4", "-5", "-1", "-3"]);
  assert.deepEqual(sortItems(items, "recent").map((i) => i.id), ["-1", "-5", "-4", "-2", "-3"]);
  assert.deepEqual(sortItems(items, "name").map((i) => i.title)[0], "Café Crème");
  assert.notStrictEqual(sortItems(items, "name"), items);
});

test("summary counts", () => {
  const s = summarize(items, 30, NOW);
  assert.deepEqual([s.total, s.inactive, s.active, s.unknown, s.unavailable, s.inactiveSelectable], [5, 3, 1, 1, 1, 2]);
});

test("parseThreshold", () => {
  assert.equal(parseThreshold("45"), 45);
  assert.equal(parseThreshold("0"), null);
  assert.equal(parseThreshold("abc"), null);
  assert.equal(parseThreshold("99999"), 3650);
});

test("pruneSelection drops ids that vanished or became unleavable", () => {
  assert.deepEqual([...pruneSelection(new Set(["-1", "-4", "-99"]), items)], ["-1"]);
});

test("retryableIds", () => {
  const job = { items: [
    { id: "a", status: "left", retryable: false },
    { id: "b", status: "failed", retryable: true },
    { id: "c", status: "failed", retryable: false },
    { id: "d", status: "rate_limited", retryable: true },
    { id: "e", status: "cancelled", retryable: true },
  ] };
  assert.deepEqual(retryableIds(job), ["b", "d", "e"]);
  assert.deepEqual(retryableIds(null), []);
});

test("formatAgo", () => {
  assert.equal(formatAgo(null), "Unknown");
  assert.equal(formatAgo(0.2), "Today");
  assert.equal(formatAgo(10.7), "10 days ago");
  assert.equal(formatAgo(400), "13 months ago");
  assert.equal(formatAgo(1000), "2 years ago");
});

// --- bots, engagement ("you haven't read or posted"), archive / mute ---

const busyIgnored = {
  id: "-10", title: "Busy But Ignored", kind: "supergroup", leavable: true, can_post: true, unread_count: 900,
  last_message_at: daysAgo(0.5), last_read_at: daysAgo(200), my_last_message_at: daysAgo(400), scanned_at: daysAgo(1),
  muted: true, archived: false,
};
const busyRead = {
  id: "-11", title: "Busy And Read", kind: "supergroup", leavable: true, can_post: true, unread_count: 0,
  last_message_at: daysAgo(0.5), last_read_at: daysAgo(0.5), my_last_message_at: null, scanned_at: null,
  muted: false, archived: true,
};
const oldBot = {
  id: "77", title: "Weather Bot", kind: "bot", leavable: true, can_post: true, unread_count: 0,
  last_message_at: daysAgo(500), last_read_at: daysAgo(500), my_last_message_at: daysAgo(510), muted: false, archived: false,
};
const unscanned = {
  id: "-12", title: "Unscanned", kind: "channel", leavable: true, can_post: false, unread_count: 30,
  last_message_at: daysAgo(1), last_read_at: null, my_last_message_at: null, scanned_at: null, muted: false, archived: false,
};
const pool = [busyIgnored, busyRead, oldBot, unscanned];

test("engagement uses the newer of your last read and your last message", () => {
  assert.equal(engagementIso(busyIgnored), busyIgnored.last_read_at);
  assert.equal(engagementIso({ last_read_at: daysAgo(50), my_last_message_at: daysAgo(3) }), daysAgo(3));
  assert.equal(engagementIso(unscanned), null);
});

test("an active chat you ignore is inactive on the 'you' basis only", () => {
  assert.equal(activityOf(busyIgnored, 90, NOW, "chat"), "active");
  assert.equal(activityOf(busyIgnored, 90, NOW, "you"), "inactive");
  assert.equal(activityOf(busyRead, 90, NOW, "you"), "active");
  assert.equal(activityOf(unscanned, 90, NOW, "you"), "unknown");
  assert.deepEqual(inactiveIds(pool, 90, NOW, "you"), ["-10", "77"]);
  assert.deepEqual(inactiveIds(pool, 90, NOW, "chat"), ["77"]);
});

test("needsScan", () => {
  assert.equal(needsScan(unscanned), true);
  assert.equal(needsScan(busyIgnored), false); // already scanned
  assert.equal(needsScan(busyRead), true); // can post, own last message unknown
  assert.equal(needsScan(oldBot), false); // everything known for free
  assert.equal(needsScan({ ...unscanned, accessible: false }), false);
});

test("eligibility per action", () => {
  assert.equal(ineligibility(busyRead, "archive"), "Already archived.");
  assert.equal(ineligibility(busyIgnored, "mute"), "Already muted.");
  assert.equal(ineligibility(busyIgnored, "archive"), null);
  assert.equal(ineligibility({ ...items[3], accessible: true, archived: false }, "archive"), null); // owners can archive
  assert.equal(ineligibility({ ...items[3], accessible: false }, "mute"), "You no longer have access to this.");
  assert.deepEqual(selectableIds(pool, "mute"), ["-11", "77", "-12"]);
  assert.deepEqual([...pruneSelection(new Set(["-10", "-11"]), pool, "archive")], ["-10"]);
});

test("bot, muted and unread filters", () => {
  const f = (over) => pool.filter((it) => matchesFilters(it, { query: "", show: "all", kind: "all", threshold: 90, ...over }, NOW)).map((it) => it.id);
  assert.deepEqual(f({ kind: "bot" }), ["77"]);
  assert.deepEqual(f({ kind: "groups" }), ["-10", "-11"]);
  assert.deepEqual(f({ muted: "muted" }), ["-10"]);
  assert.deepEqual(f({ unreadOnly: true }), ["-10", "-12"]);
  assert.deepEqual(f({ show: "unavailable", action: "archive" }), ["-11"]);
  assert.deepEqual(sortItems(pool, "unread").map((i) => i.id), ["-10", "-12", "-11", "77"]);
  assert.deepEqual(sortItems(pool, "oldest", "you").map((i) => i.id), ["77", "-10", "-11", "-12"]);
});

test("formatDuration", () => {
  assert.equal(formatDuration(40), "40 s");
  assert.equal(formatDuration(600), "10 min");
});
