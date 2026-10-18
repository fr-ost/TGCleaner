# TG Group Cleaner (personal)

A personal-use tool to find inactive Telegram groups and channels and leave the ones you pick, in bulk, with one confirmation.

- Connects to **your own** Telegram account (phone number + login code + optional two-step password).
- Lists your groups and channels with type, @username/link, last message date, time since the last message, and your role.
- Filters for 7 / 30 / 90 / 180 / custom days of inactivity, plus search, sorting, Select inactive, Select all shown and Deselect all.
- **Leave selected**: shows the exact names first, asks once, then leaves them one at a time. You see Telegram's real answer for every item, and can retry the ones that can be retried.
- Runs entirely on your computer. There is no cloud backend, no account, and no analytics.

![dashboard](docs/dashboard.png)

---

## How it works

```
Chrome extension (dashboard tab)  ──HTTP, JSON──▶  Local service (Python)  ──MTProto──▶  Telegram
  UI only, no secrets              127.0.0.1:8765    Telethon + aiohttp
  stores only UI preferences                         keeps the session in ~/.tgcleaner/
```

* **Why a local service?** Leaving groups requires Telegram's client API (MTProto) as a *user*. The Bot API can't do it. A small Python service using [Telethon](https://docs.telethon.dev) is the simplest reliable way to do that. Keeping the session there also means no Telegram credentials ever exist in the extension or in Chrome storage.
* **Extension**: Manifest V3, plain HTML/CSS/JS, no build step, no frameworks. Clicking the toolbar icon opens the dashboard in a tab.
* **Service**: binds to `127.0.0.1` only. It accepts requests only from this extension's origin (see [Security](#security)).

Telegram operations used (checked against Telethon 1.45, API layer 229):

| Purpose | Telegram method |
|---|---|
| Send login code | `auth.sendCode` |
| Sign in / two-step verification | `auth.signIn`, then `account.getPassword` + `auth.checkPassword` (SRP; the password itself is never sent) |
| List groups & channels | `messages.getDialogs` (all folders, including Archive) |
| Activity | date of each dialog's most recent message (as returned with the dialog list) |
| Leave channel / supergroup | `channels.leaveChannel` |
| Leave basic group | `messages.deleteChatUser` with yourself as the user (your messages are **not** deleted) |
| Disconnect | `auth.logOut`, then the local session file is deleted |

The service never calls Telethon's `delete_dialog()`, because that helper deletes private conversations when given a user. The leave code can only build the two requests above, and only for items that were listed, are leavable, and were confirmed.

---

## Requirements

* Python **3.10+** (`python3 --version`; on Windows, install from python.org and tick "Add to PATH").
* Google Chrome (or another Chromium browser that supports Manifest V3).
* A Telegram **API ID** and **API hash** for your own account (free; see step 1).

## Setup

### 1. Get your API ID and hash
1. Go to <https://my.telegram.org> and log in with your phone number.
2. Open **API development tools** and create an app (any title / short name; platform "Desktop").
3. Keep the page open: you need **App api_id** and **App api_hash**.

### 2. Start the local service
macOS / Linux:
```sh
cd TGCleaner
./start.sh
```
Windows: double-click `start.bat`, or run it from a terminal in the `TGCleaner` folder.

The first run:
- creates a virtual environment in `service/.venv` and installs the dependencies (`telethon`, `aiohttp`)
- asks for your API ID and API hash (the hash is typed hidden)
- asks for the extension ID. Press Enter to accept the default; it's fixed by the manifest key.

These are saved to `~/.tgcleaner/config.json` (Windows: `C:\Users\<you>\.tgcleaner\config.json`) with owner-only permissions.

Leave the window open. You should see `TG Group Cleaner service on http://127.0.0.1:8765`.

### 3. Load the extension into Chrome
1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the `TGCleaner/extension` folder.
4. Check the ID shown on the card is `fdjdkkclgclbgkhlhlfbnjkjocfjjijn`. The manifest's `key` pins it, and the service only trusts that ID.
5. Optional: pin the extension (puzzle-piece icon → pin).

### 4. Connect Telegram
1. Click the extension icon. The dashboard opens in a tab.
2. Enter your phone number in international format (e.g. `+15551234567`) → **Send login code**.
3. Enter the code Telegram sends (usually in the Telegram app on your other device).
4. If two-step verification is on, enter that password.
5. The header shows **Connected as \<your name\>**. Your groups and channels load automatically.

Your phone number, code and password pass from the dashboard to the local service and on to Telegram. They are never written to disk or logged. Only Telethon's session file (`~/.tgcleaner/telegram.session`) is kept, so you stay logged in.

> Telegram treats this like any other login: you'll see a "new login" message in Telegram and a session called **TG Group Cleaner (personal)** under *Settings → Devices*.

### Try it safely first (demo mode)
```sh
./start.sh --demo        # Windows: start.bat --demo
```
Demo mode uses 320 fake groups and never contacts Telegram. It accepts any phone number, code `12345` and password `demo`. Some leaves fail on purpose, and one simulated rate-limit pause shows how errors look. Stop it with Ctrl+C before starting the real service; both use the same port.

---

## Using the dashboard

* **Inactive for**: 7 / 30 / 90 / 180 days, or **Custom…** (1–3650 days). An item is *inactive* when its last known message is at least that old.
* **Unknown activity**: Telegram returned no message history for it, for example because history was cleared or is hidden. These items are shown separately, counted separately, and **never** selected by *Select inactive*. Missing data is not treated as proof of inactivity.
* **Show**: All / Inactive / Active / Unknown activity / Can't be left. **Type**: channels, supergroups, basic groups, broadcast groups. **Sort**: least or most recently active (unknown always last), name, type.
* **Select inactive**: selects the inactive, leavable items in the current view, so search and type filters apply. **Select all shown**: every leavable item in the current view. **Deselect all**: clears everything. Click a row to toggle it; Shift+click selects a range. The counter shows how many selected items are hidden by the current filters.
* **Leave selected** asks the service to validate the selection. A dialog lists the exact names (and anything that will be skipped, with the reason). Nothing happens until you click **Leave N**.
* **Progress**: items are left one at a time with a 2-second pause between them. Each row and the results list show *Left ✓*, *Not a member*, *Failed* (with Telegram's reason), *Rate limited* or *Not attempted*. You can close the tab; the job keeps running in the service, and reopening the dashboard shows it.
* **Retry N eligible**: re-runs only the retryable failures (network problems, unexpected Telegram errors, rate-limited or cancelled items), again after a confirmation.
* **Reload list**: fetches the list from Telegram again. Channels and supergroups you left disappear. Basic groups you left stay in Telegram's chat list (as in the official apps) and show as "You already left this group"; delete them in the Telegram app if you want them gone.

### What is never listed or touched
* Private one-to-one chats, bots and **Saved Messages**. They are counted in a note under the stats so you can see they were excluded.
* Old basic groups that were upgraded to supergroups (Telegram keeps a dead copy; the live supergroup is listed instead).

### What is listed but can't be left here (shown with the reason)
* Groups and channels **you own**. Transfer ownership or delete them in the Telegram app.
* Ones you already left, were removed or banned from, or that are no longer accessible.
* A channel's direct-messages inbox, and Telegram *Communities* or other new types this tool doesn't know.

### Rate limits
Telegram does not publish exact limits for leaving chats. The tool:
* sends one request at a time with a 2 s pause (`leave_delay_seconds` in `config.json`, minimum 0.5)
* waits out a `FLOOD_WAIT` of up to 5 minutes (`max_auto_flood_wait`), with a countdown, then retries that item **once**
* stops the batch on a longer wait, or on a second wait for the same item. The remaining items are marked *Rate limited / not attempted*, and new batches are refused until the wait is over. It never hammers Telegram with retries.

### Disconnect
Click **Disconnect** in the header. This calls Telegram's logout (ending the session) and deletes the local session file. If Telegram can't be reached, the local file is still deleted and you're told to end the session under *Settings → Devices* in Telegram. From a terminal: `./start.sh logout`. To delete everything, including your API ID and hash: `./start.sh reset`.

---

## Security

* The service listens on **127.0.0.1** only, never on your network.
* Every `/api` request must:
  * come from `Origin: chrome-extension://fdjdkkclgclbgkhlhlfbnjkjocfjjijn`. Browsers don't let web pages fake the Origin header.
  * carry the `X-TGCleaner: 1` header. This forces a CORS preflight, which the service refuses for any other origin, so websites can't even trigger requests blindly.
  * address `127.0.0.1:<port>` or `localhost:<port>` in the Host header (blocks DNS-rebinding attacks).
  * be JSON, under 64 KB, with strictly validated fields: numeric item IDs, at most 5000 per request, deduplicated.
* Leaving is two-step: **prepare** returns the validated names and a single-use token valid for 10 minutes; **start** consumes the token. Reloading the list invalidates tokens. Only one leave job can run at a time. Items already left can't be submitted again.
* Your API hash and Telegram session stay in `~/.tgcleaner/` (folder `0700`, files `0600` on macOS/Linux). The extension only stores the port and harmless filter preferences in `chrome.storage.local`.
* Login codes, passwords and request bodies are never logged. Group titles from Telegram are inserted as text, never as HTML.
* A program already running as your user could read the session file. That is true for every Telegram desktop client and is outside what a localhost check can protect against.

---

## Configuration (`~/.tgcleaner/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `api_id`, `api_hash` | (setup) | From my.telegram.org |
| `port` | `8765` | Service port. If you change it, also set it on the dashboard's "Start the local service" screen. |
| `extension_ids` | `["fdjdkkclgclbgkhlhlfbnjkjocfjjijn"]` | Allowed extension IDs |
| `leave_delay_seconds` | `2.0` | Pause between leave requests (min 0.5) |
| `max_auto_flood_wait` | `300` | Longest Telegram wait handled automatically, in seconds |

Set `TGC_DATA_DIR` to use a different data folder.

---

## Tests

All tests are mocked. **None of them contacts Telegram or leaves anything.**

```sh
cd service
.venv/bin/python -m unittest discover -s tests -t .     # Windows: .venv\Scripts\python -m unittest discover -s tests -t .
cd ..
npm test                                                # or: node --test "tests/extension/*.test.mjs"  (Node 20+)
```

* `tests/test_telethon_backend.py`: classification built from real Telethon TL objects (private chats, bots and Saved Messages are excluded; owned, left, banned and unknown types are not leavable). Also checks that the exact leave requests are built (`LeaveChannelRequest`, `DeleteChatUserRequest(chat_id, InputUserSelf)` without revoking history), maps every Telegram error, and covers the login flow with 2FA (asserting the code and password never appear in logs) and logout.
* `tests/test_leave_job.py`: sequential pacing, partial failures, short and long flood waits, no hammering, cancel, crash safety, no double leave.
* `tests/test_server.py`: Origin, Host and header checks, CORS preflight, body limits, input validation, the login API, a 60-item bulk leave with mixed failures and a flood wait, retrying only eligible items, token reuse, concurrent-job rejection, cancel, and rate-limit blocking.
* `tests/extension/logic.test.mjs`: filtering, sorting, thresholds, unknown-activity handling, and the Select inactive / retry rules.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard says **Local service offline** | Start `./start.sh` / `start.bat` and keep the window open. If you changed the port, enter it on that screen. Check `http://127.0.0.1:8765/health` in a browser tab: it should show `{"ok": true, ...}`. |
| **Service rejected this extension** | The extension ID differs from the configured one, usually because the `key` was removed from `manifest.json`. Run `./start.sh setup` and enter the ID shown on the dashboard (or on `chrome://extensions`), then restart the service. |
| `Address already in use` on start | Another copy (or demo mode) is running. Close it, or set a different `port` in `config.json` and on the dashboard. |
| `python3: command not found` / wrong version | Install Python 3.10+; on macOS `brew install python`, on Windows from python.org. You can point the script at a specific interpreter: `PYTHON=/path/to/python3 ./start.sh`. |
| "Telegram rejected the API ID / hash" | Run `./start.sh setup` again and copy both values exactly from my.telegram.org. |
| No login code arrives | It usually goes to the Telegram app on another device, not SMS. Wait a minute; requesting codes repeatedly triggers a long wait. |
| "Telegram requires a login email" | Newer accounts may need a login email. Set it up in the official app, then try again. |
| "Too many attempts / wait N seconds" | Telegram's flood protection. Wait the stated time; don't retry in a loop. |
| A leave shows **Rate limited** | Telegram asked for a long pause. Retry those items after the time shown. Consider raising `leave_delay_seconds`. |
| A leave shows **Failed: Connection problem** | The request may or may not have reached Telegram. Use **Retry**: if it had worked, Telegram answers "not a member", which is shown as such. |
| An item shows **Unknown activity** | Telegram returned no messages for it (history cleared or hidden). Open it in Telegram to judge; the tool won't assume it's inactive. |
| Lost track of a session | In Telegram, *Settings → Devices*, terminate "TG Group Cleaner (personal)". Then delete `~/.tgcleaner/telegram.session` or run `./start.sh logout`. |

## Project layout

```
extension/            Chrome MV3 extension (no build step)
  manifest.json       pinned key → stable ID; only the "storage" permission
  background.js       opens/focuses the dashboard tab
  dashboard.html/css/js
  api.js              talks to 127.0.0.1
  logic.js            pure filter/sort/selection logic (unit tested)
service/
  tgcleaner/
    __main__.py       CLI: setup | serve [--demo] | logout | reset
    config.py         private data dir + config validation
    telegram_backend.py  Telethon: login, dialogs, classification, leave
    leave_job.py      sequential bulk leave with flood-wait handling
    server.py         aiohttp API + security checks
    fake_backend.py   demo mode / tests (no Telegram)
  tests/
start.sh / start.bat  create venv, install, set up, run
tests/extension/      JS unit tests
```

This project is for personal use: one person, their own account, their own computer.
