# Teams Bot — Project Context

Last updated: 2026-09-22

## Purpose

Teams Bot is a local macOS helper for Microsoft Teams. Its goal is to notice a newly arrived Teams poll, locate that poll's **Submit** button, and interact with it only under the configured safeguards. It is deliberately designed around local, on-device screen inspection and macOS accessibility rather than collecting poll questions or answer text.

The intended end-state is a polished, Mac-like utility that can remain out of the way while monitoring Teams, but still offers strong diagnostics during refinement.

## Release track

- **Standard build:** `0.6.0-beta.15.1` (internal beta).
- **Graph beta:** `0.7.0-graph-beta.13` (separate internal beta; interactive
  Microsoft browser sign-in verifies only delegated `User.Read`, with no
  Teams-content access).
- **Target release:** `1.0.0`.
- **Versioning:** meaningful behavior, safety, or stability changes advance the
  beta number (for example, `0.6.0-beta.13`); small visual or wording polish
  uses a patch suffix (for example, `0.6.0-beta.13.1`). The internal macOS
  build number still advances for every installed package.
- Each launch writes its version, stage, and release target to
  `poll-history.jsonl`, making future diagnostic archives traceable to the
  build that produced them.
- Do not call a build release-ready until fresh-poll detection, permission
  behavior, visual/OCR reliability, and false-action prevention have been
  validated over sustained normal use.

### Latest stability polish

The standard app remains fully opaque when it loses focus. This avoids a
misleading translucent appearance over Teams or the desktop while monitoring.

Monitoring input is deliberately conservative: notifications are wake hints
only after the visible banner identifies **Workflows** and **Sent a card**.
Without the live in-app **New messages** control, TeamsBot does not scroll or
move the pointer.

Before a poll enters its review window, TeamsBot compares the timestamp
directly above the visible Submit card to the Mac's local clock. It accepts
only timestamps no more than five minutes old and writes the time comparison
to Activity for auditing.

## Current project layout

| Path | Role |
| --- | --- |
| `teams_bot.py` | Main Tk/Python application and monitoring logic. |
| `teamsbot_menu_bar.m` | Separate native Cocoa menu-bar helper. |
| `CHANGELOG.md` | Chronological record of completed and future changes. |
| `TeamsBot.jpeg` / `TeamsBot.icns` | User-provided robot logo and app icon. |
| `submit_button_current.png`, `submit_button_light.png` | Visual templates used to find Teams Submit. |
| `teams_notification_icon.png` | Template used to notice a visible Teams notification. |
| `new_messages_reference.png` / `last_read_reference.png` | Narrow visual references for Teams navigation safeguards. |
| `teamsbot_vision_ocr.swift` | Short-lived native Vision helper used for on-device OCR. |
| `graph_integration.py` | Minimal Microsoft Graph browser sign-in and `User.Read` verification layer; no secrets, persistent tokens, or Teams-content access. |
| `/Applications/TeamsBot.app` | Standard installed app. |
| `/Applications/TeamsBotGraphBeta.app` | Separate Graph-readiness beta, with the same core features. |

This directory is **not currently a Git repository**.

## Change-record practice

[`CHANGELOG.md`](CHANGELOG.md) is the durable record of project changes. Before completing a future implementation, add a concise entry under **Unreleased**; roll those entries into a dated section when a build is handed off. Read it alongside this document when continuing in a new chat.

## What the app does today

- Uses one transient Screen Recording capture per normal awareness pass for
  Submit, Teams notification, and New messages template checks. The image is
  kept only in memory and discarded after that pass.
- Uses on-device macOS Vision OCR only when a stable candidate needs a safety
  decision. One OCR packet checks the completion toast, Last read divider, and
  card-local timestamp together; a second packet is reserved for the final
  live pre-click recheck after Teams comes forward.
- Uses Accessibility for pointer movement, clicks, and a best-effort direct jump to the Teams conversation scrollbar's maximum value.
- Watches visible Teams notification banners as a trigger. It does **not** have a supported macOS API for intercepting all Teams notifications in the background.
- Includes a dormant **Microsoft Graph integration status** panel. It makes no
  network calls in this beta. A future tenant-approved connector could use
  Graph change notifications as an activity signal, but it must still wake the
  existing local verification flow rather than directly authorizing a click.
- When a notification is found, opens Teams, prioritizes the newest end of the chat, locates Submit, and performs a final live visual validation before a normal-mode click.
- Has a five-minute age limit for normal poll handling. Old/completed polls should be skipped in normal monitoring.
- Applies a 30-second review delay before a normal automated click. Debug/Test mode does not click.
- Persists timestamp-only identities and no-repeat poll markers; it never
  persists screenshots, questions, or responses:
  - `~/Library/Application Support/TeamsBot/seen-timestamps.json`
  - `~/Library/Application Support/TeamsBot/noted-poll-timestamps.json`
  - `~/Library/Application Support/TeamsBot/poll-history.jsonl`
- Has a manual/continuous timestamp scan intended for user-driven scrolling through Teams history. Continuous OCR runs about every 0.55 seconds and requires two consecutive matches before logging a timestamp.
- Plays distinct macOS sounds for a validated new poll and a Teams-confirmed submission.
- Does not change desktops merely because the Mac is idle. Teams is brought
  forward only after a valid activity signal or while a user explicitly runs a
  diagnostic test.
- Uses the user-provided teal robot image in the window, Dock/app icon, and menu bar.
- Fades the bot window when unfocused so it blocks less of Teams.

## Interface and diagnostics

- **Shared visual standard:** visual changes are defaults for every port and
  edition where the native platform supports them. Keep the **TeamsBot** name,
  robot branding, simple activity-centered layout, clear control hierarchy,
  active-state color cue, readable button text, and light/dark adaptation
  aligned between the standard macOS build, Graph beta, and Android port.
  A platform may adapt layout to its form factor, but should not diverge in
  core visual meaning without an explicit product reason.
- The main window uses the subtitle **“Awaiting the next signal”** while idle.
- The visible **Diagnostics** menu is grouped and includes a **Diagnostics
  guide…** entry. Normal Activity is plain language; **Show technical details
  in Activity** exposes scan counts, timings, and exact gate reasons only when
  deliberately enabled.
- The native menu-bar robot icon offers Show/Hide, Start/Stop, and Quit. It is implemented as a separate Cocoa process to avoid Tk/Python crashes.
- The native helper has a file lock, so only one menu-bar icon should exist after a clean launch.
- `Test current poll` is an explicit diagnostic override:
  - opens Teams;
  - runs a pre-bottom scan;
  - moves to the newest chat position via the direct scrollbar jump when available (or uses a downward-scroll fallback);
  - scans for Submit again;
  - performs a final live scan before pointer movement/click;
  - ignores persisted poll/timestamp history in normal mode.
  - In **Test mode**, it moves the pointer to the dynamic Submit target but never clicks.

`Last read` is only a scrolling boundary: it helps stop an old-history sweep
and rejects a Submit above the divider. It is never proof of a new poll.

Every normal action must agree on all required safety checks: stable live
Submit match, date-qualified timestamp attached to that same card and within
five minutes, no completion toast, correct Last read position when present,
an active monitor token, and a final live recheck. A timestamp already marked
for an earlier review is skipped unless it is the same current review.

## Important macOS constraints

1. **Screen Recording** and **Accessibility** must both be enabled for `TeamsBot.app` in System Settings → Privacy & Security. A new bundle install can appear as a new TCC identity, so re-check permissions after a rebuild if features stop working.
2. macOS does not offer an officially supported, general-purpose API for another app to read every Teams notification. A visible banner can be screen-detected; Microsoft Graph would require tenant/admin/app registration work and was intentionally deferred.
   Graph change notifications require a Microsoft Entra app registration,
   appropriate consent, a verified notification receiver, and subscription
   renewal. Never add client secrets or passwords to Teams Bot’s local config.
3. Teams' accessibility tree can change across Teams releases. The app tries to set the rightmost vertical scrollbar (the conversation scrollbar) to maximum via Accessibility, but gracefully falls back to wheel scrolling if the control is not exposed.
4. OCR is local and transient, but it can still be slower than a visual pass.
   The app serializes it and avoids starting OCR during ordinary awareness
   scans; continuous timestamp scanning is diagnostic-only and pauses while
   normal monitoring runs.

## Problems already resolved

- **Repeated `TB` menu-bar icons:** caused by orphaned status helpers. Fixed with a native helper process and a single-instance file lock. Kill stale `teamsbot_menu_bar` processes when testing older builds.
- **Status-bar/Tk crashes:** `pystray` and direct AppKit callbacks in the Tk/Python process caused `EXC_BAD_ACCESS`/main-thread failures. The status item now runs in a separate native Cocoa helper.
- **Repeated Screen Recording prompts / stale app names:** old app bundles and identities created TCC confusion. Old builds were removed; use the installed `/Applications/TeamsBot.app` build.
- **Tooltips appearing as blank dark rectangles:** Tk popup tooltips were unreliable on macOS. Replaced with in-window hints/removed where unnecessary.
- **Static Submit click coordinate:** replaced by template matching and a dynamic target computed from the live button position.
- **Old visible polls re-triggering:** timestamp history/baseline logic,
  card-local timestamp association, Last read positioning, normal-mode
  age/completion checks, and persistent no-repeat poll markers were added.
- **Overly technical Activity log:** normal status text is now plain language;
  full diagnostics are optional through the Diagnostics menu.
- **Continuous scan duplicates:** two-scan confirmation plus persistent timestamp de-duplication; Activity differentiates newly logged versus already-recorded timestamps.
- **Menu-bar duplication:** one helper only; do not reintroduce a second status-item implementation inside `teams_bot.py`.

## Build and install workflow

Run from `/Users/balexgt/Documents/TeamsBot`:

```sh
python3 -m py_compile teams_bot.py
clang -framework Cocoa -o teamsbot_menu_bar teamsbot_menu_bar.m
swiftc teamsbot_vision_ocr.swift -framework Vision -framework AppKit -o teamsbot_vision_ocr

PYINSTALLER_CONFIG_DIR="$PWD/debug-work/pyinstaller-config" pyinstaller --noconfirm --windowed \
  --name TeamsBot --osx-bundle-identifier com.balexgt.teamsbot \
  --icon TeamsBot.icns \
  --add-data 'submit_button_current.png:.' \
  --add-data 'submit_button_light.png:.' \
  --add-data 'teams_notification_icon.png:.' \
  --add-data 'new_messages_reference.png:.' \
  --add-data 'last_read_reference.png:.' \
  --add-data 'TeamsBot.jpeg:.' \
  --add-binary 'teamsbot_menu_bar:.' \
  --add-binary 'teamsbot_vision_ocr:.' \
  --distpath debug-build --workpath debug-work teams_bot.py

codesign --verify --deep --strict debug-build/TeamsBot.app
```

Install by moving the prior app to Trash, then moving `debug-build/TeamsBot.app` to `/Applications/TeamsBot.app`. Before relaunching during development, stop only the scoped processes:

```sh
pkill -x teamsbot_menu_bar || true
pkill -x TeamsBot || true
open -n /Applications/TeamsBot.app
```

Always verify one `TeamsBot` and one `teamsbot_menu_bar` process after relaunch.

## Recommended next work

1. **Test the direct scrollbar jump in the installed app.** The Activity log should say either it set the scrollbar directly to the bottom or that it used the fallback. Confirm the thumb visibly sits at the very bottom.
2. **Validate a real fresh poll end-to-end.** Check: activity signal →
   newest-chat jump → stable card-local fresh timestamp → correct dynamic
   target → review delay → final recheck → click → “Your response was sent to
   the app.”
3. **Tune the final click policy.** The diagnostic `Test current poll` is intentionally more permissive than normal monitoring. Keep that separation unless the user explicitly wants to change normal safeguards.
4. **Evaluate detection reliability across virtual desktops.** Screen-based
   detection only sees the active desktop. A robust background trigger still
   requires either a visible notification banner or a future approved Graph
   integration.
5. **Keep standard features aligned across editions.** Add core behavior to both the standard and Graph-readiness beta by default. Reserve beta-only changes for future approved Graph integration or explicitly requested experiments.
6. **Parked platform idea — Android companion.** Treat Android as a future, separate native project rather than a port of the macOS automation layer. Reuse only the product concepts, local timestamp history, and authorized Graph sign-in; evaluate Android accessibility/screen-capture constraints and distribution policy before any implementation.

## Continuation prompt for a new chat

> Read `/Users/balexgt/Documents/TeamsBot/project_context.md` first. Continue improving the local macOS Teams Bot from its current installed build. Preserve the separate native menu-bar helper and the normal-mode safety rules. Start by checking the latest user-reported behavior before changing code.
