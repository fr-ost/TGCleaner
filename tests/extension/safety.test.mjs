// Run: npm test
import assert from "node:assert/strict";
import { test } from "node:test";

import { guardReason, importanceReasons, inactiveIds, ineligibility, matchesFilters, selectableIds } from "../../extension/logic.js";

const NOW = Date.parse("2026-10-01T00:00:00Z");
const daysAgo = (d) => new Date(NOW - d * 86_400_000).toISOString();
const base = { leavable: true, accessible: true, can_post: true, unread_count: 0, folders: [], last_message_at: daysAgo(400) };
const chats = [
  { ...base, id: "a", title: "Old public", kind: "supergroup", username: "old", participants_count: 50, role: "member" },
  { ...base, id: "b", title: "Family", kind: "group", folders: ["Family"], participants_count: 8, role: "member" },
  { ...base, id: "c", title: "My channel admin", kind: "channel", role: "admin", participants_count: 20000 },
  { ...base, id: "d", title: "Pinned bot", kind: "bot", pinned: true, role: "none" },
  { ...base, id: "e", title: "Protected", kind: "gigagroup", protected: true, participants_count: 500000, role: "member" },
  { ...base, id: "f", title: "Quiet but I posted", kind: "supergroup", my_last_message_at: daysAgo(10), unread_mentions: 2, role: "member" },
];
const f = (over) => chats.filter((it) => matchesFilters(it, { query: "", show: "all", kind: "all", threshold: 90, ...over }, NOW)).map((it) => it.id);

test("type filters: channels, groups, supergroups (incl. broadcast groups), basic groups, bots", () => {
  assert.deepEqual(f({ kind: "channel" }), ["c"]);
  assert.deepEqual(f({ kind: "groups" }), ["a", "b", "e", "f"]);
  assert.deepEqual(f({ kind: "supergroup" }), ["a", "e", "f"]);
  assert.deepEqual(f({ kind: "group" }), ["b"]);
  assert.deepEqual(f({ kind: "bot" }), ["d"]);
});

test("more filters", () => {
  assert.deepEqual(f({ role: "admin" }), ["c"]);
  assert.deepEqual(f({ role: "member" }), ["a", "b", "d", "e", "f"]);
  assert.deepEqual(f({ visibility: "public" }), ["a"]);
  assert.deepEqual(f({ pinned: "yes" }), ["d"]);
  assert.deepEqual(f({ folder: "yes" }), ["b"]);
  assert.deepEqual(f({ folder: "no" }).includes("b"), false);
  assert.deepEqual(f({ size: "lt100" }), ["a", "b"]);
  assert.deepEqual(f({ size: "10k+" }), ["c", "e"]);
  assert.deepEqual(f({ posted: "yes" }), ["f"]);
  assert.deepEqual(f({ mentionsOnly: true }), ["f"]);
  assert.deepEqual(f({ protection: "only" }), ["e"]);
  assert.deepEqual(f({ protection: "hide" }).includes("e"), false);
});

test("protected chats are never eligible for any action", () => {
  for (const action of ["leave", "archive", "mute"]) assert.match(ineligibility(chats[4], action), /Protected/);
});

test("safety guard keeps pinned, foldered and admin chats out of bulk selection", () => {
  assert.equal(guardReason(chats[1]), "in your folder “Family”");
  assert.equal(guardReason(chats[2]), "you're an admin");
  assert.equal(guardReason(chats[3]), "pinned");
  assert.equal(guardReason(chats[0]), null);
  assert.deepEqual(inactiveIds(chats, 90, NOW, "chat", "leave", true), ["a", "f"]);
  assert.deepEqual(inactiveIds(chats, 90, NOW, "chat", "leave", false), ["a", "b", "c", "d", "f"]);
  assert.deepEqual(selectableIds(chats, "mute", true), ["a", "f"]);
});

test("looks-important reasons for the confirmation", () => {
  assert.deepEqual(importanceReasons(chats[0], NOW), []);
  assert.deepEqual(importanceReasons(chats[1], NOW), ["in folder “Family”"]);
  assert.deepEqual(importanceReasons(chats[5], NOW), ["you posted 10 days ago", "2 unread mentions"]);
});
