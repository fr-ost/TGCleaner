// Run: npm test
import assert from "node:assert/strict";
import { test } from "node:test";

import { isLocal, normalizeServiceUrl } from "../../extension/api.js";

test("service address: local http or any https, nothing else", () => {
  assert.equal(normalizeServiceUrl(" http://127.0.0.1:8765/ "), "http://127.0.0.1:8765");
  assert.equal(normalizeServiceUrl("http://localhost:9000"), "http://localhost:9000");
  assert.equal(normalizeServiceUrl("https://tgcleaner-production.up.railway.app/some/path"), "https://tgcleaner-production.up.railway.app");
  assert.throws(() => normalizeServiceUrl("http://tgcleaner.up.railway.app"), /https/); // token would travel in clear text
  assert.throws(() => normalizeServiceUrl("ftp://example.com"));
  assert.throws(() => normalizeServiceUrl("not a url"));
});

test("isLocal", () => {
  assert.equal(isLocal("http://127.0.0.1:8765"), true);
  assert.equal(isLocal("http://localhost:8765"), true);
  assert.equal(isLocal("https://x.up.railway.app"), false);
});
