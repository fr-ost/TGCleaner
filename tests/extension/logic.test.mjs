// Run: node --test tests/extension
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activityOf, daysSince, formatAgo, inactiveIds, matchesFilters, parseThreshold, pruneSelection,
  retryableIds, selectableIds, sortItems, summarize,
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
  assert.deepEqual(summarize(items, 30, NOW), { total: 5, inactive: 3, active: 1, unknown: 1, unavailable: 1, inactiveSelectable: 2 });
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
