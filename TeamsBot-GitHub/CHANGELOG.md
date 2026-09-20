# Changelog

All notable changes to Teams Bot are recorded here. New work should be added to the **Unreleased** section before it is handed off or rebuilt.

## Release track

- **Current standard build:** `0.6.0-beta.7` — internal beta.
- **Current Graph beta:** `0.7.0-graph-beta.13` — internal beta.
- **Release target:** `1.0.0` — only after sustained real-world validation:
  reliable fresh-poll detection, no unhandled crashes/OCR stalls, stable
  permissions/installation behavior, and no false normal-mode actions.
- Every launch records the current build, stage, and target in the local poll
  history, so archived diagnostics can always be tied to a specific version.
- Standard features ship in both editions by default. The Graph-readiness beta
  is reserved for future approved Microsoft integration work or explicit
  experiments, not general feature divergence.

## Unreleased

- All macOS editions: adopted the supplied TeamsBot robot artwork as the
  primary logo direction. The compact transparent mark now appears in the
  window, Dock icon, and native menu-bar icon, and shared accents moved from
  teal to the artwork's indigo/blue palette.
  The Graph edition keeps its edition name in the window title while retaining
  the compact **TeamsBot** header wordmark beside its Connect Teams control.

- All editions: status colors now communicate state rather than branding:
  **Watching** is green and safe diagnostic activity is yellow. Indigo remains
  reserved for the TeamsBot visual identity and active controls.

- Project-wide: established a shared visual standard for all TeamsBot ports
  and editions. Naming, branding, readable controls, active-state meaning,
  centered activity treatment, and light/dark behavior should stay aligned
  unless a platform capability or an explicitly requested beta feature calls
  for a meaningful difference.

- All editions: standardized the visible app and header name as **TeamsBot**;
  the connected edition is **TeamsBot Graph Beta**.

- Standard build: the teal active control now reads **Monitoring** while the
  monitor is running. **Stop Monitoring** remains a neutral control in every
  state, so teal consistently means active monitoring rather than stopping.

- Standard build: made the **New messages** handoff faster and more precise.
  A fresh Teams banner now attempts Teams' own in-chat jump after the app is
  foregrounded, even if the first lightweight capture did not yet expose that
  control. When a new card header supplies only a nearby bare clock (for
  example, `11:01 PM`), it may be treated as today **only** during that fresh
  signal and only when it is physically associated with the visible Submit.
  The existing five-minute, Last read, completion, duplicate, and final live
  recheck gates still apply. If that jump cannot yield a verified card, the
  fallback is limited to three nearby passes instead of a full sweep.

- Standard build: improved light-appearance Submit recognition. Reference
  matching now covers the current light control's bounded size range. The
  supervised Test Newest Submit tool makes one Teams-window-only OCR fallback
  for the exact visible Submit label when image and Accessibility lookup miss;
  normal monitoring remains lightweight and retains its existing safety gates.

- Standard build: Continuous Scan and Monitoring are now mutually exclusive.
  Turning on Continuous Scan stops Monitoring first; starting Monitoring stops
  Continuous Scan. This prevents concurrent OCR work and makes the active mode
  unambiguous.

- Standard build: aligned Poll History and the main Start/Stop controls with
  the current adaptive light/dark button styling and clear borders.

- Standard build: made the new-activity handoff more reliable. A Teams banner
  that says **Sent a card** now wakes a normal Teams check even when the icon
  image does not match the current light/dark banner. After pressing Teams'
  in-chat **New messages** control, the bot re-checks the newly revealed
  position before scrolling again. Submit detection now has a narrow
  Accessibility fallback for Teams' exact visible Submit control when the
  reference image misses. The existing fresh-timestamp, Last read,
  completion, and final live-recheck safeguards remain required before any
  pointer action.

- Standard build: a verified Teams activity signal now activates and raises
  Teams' real window before review. macOS may switch to the Teams Space using
  the user's normal desktop preference; if Teams cannot be brought forward,
  the bot stops that pass without scrolling or clicking elsewhere.

- Parked a future Android companion concept. It would be a separate native,
  Graph-first app; the macOS screen-capture, Accessibility, and menu-bar
  implementation is not assumed portable.

- Made the small Diagnostics help hint self-clearing in both editions. It
  disappears immediately when the pointer leaves a Diagnostics menu and after
  3.2 seconds when the same item remains selected, so it cannot cover the
  Activity panel indefinitely.

- Moved Diagnostics out of both main windows. It is now available through the
  native menu-bar helper or `⌘⇧D` (`Ctrl-Shift-D` fallback); the menu-bar item
  reveals the app and opens Diagnostics in one action.

- Replaced the menu-bar Diagnostics action with a native **Diagnostics**
  submenu. Its controls remain visually anchored to the macOS menu bar and
  dispatch their existing local diagnostic actions through the app's safe
  command queue.

- Graph beta: replaced the visual-only sign-in preview with Microsoft’s
  interactive browser sign-in using only delegated `User.Read`. The access
  token remains in memory for the current run and no password, client secret,
  account identity, or Teams content is stored. The setup status label now
  wraps cleanly instead of clipping long status text.

- Graph beta: moved **Connect with Teams** out of Diagnostics and into the
  main window. Diagnostics remains for troubleshooting; the visible connection
  control changes to **Teams connected** for the current signed-in session.

- Graph beta: promoted **Connect with Teams** to a stronger primary action
  using the Teams brand color and mark for clearer visual hierarchy.

- Graph beta: tightened the primary button's combined icon-and-label layout
  so it remains centered and fully contained in its header control.

- Graph beta: restyled **Connect Teams** as an inverted macOS-style outlined
  action with a white background, Teams-purple border, icon, and text.

- Graph beta: removed the redundant Teams mark from **Connect Teams**; the
  existing robot brand mark remains the sole header icon.

- Graph beta: refreshed the **Connect with Teams** window with a Teams-purple
  header, supplied Teams logo tile, lavender status surface, and lightly
  tinted, purple-bordered Tenant ID and Application ID fields.

- Added **Verify & Repair Scan State** and **Start New Session…** in
  Diagnostics. Repair preserves malformed tracking files in a dated recovery
  folder; a new session archives the old state and writes a compact
  cross-reference summary without poll content.

- Added a dormant Microsoft Graph readiness boundary and a Diagnostics status
  panel. It makes no network calls and stores no credentials; a future approved
  connector may provide a new-activity signal, but can never bypass the local
  live-screen, timestamp, and final-click safeguards.

- Consolidated Diagnostics into a smaller, guided menu: safe test mode,
  extended search, screen inspection, newest-Submit testing, grouped timestamp
  tools, Shortcuts/Siri setup, and poll history. Removed obsolete manual
  coordinate tuning, the unused Accessibility timestamp scanner, and the
  experimental idle desktop-switching mode.

- Added a Diagnostics-only **Show technical details in Activity** toggle.
  Normal Activity now uses plain-language status updates; timing, OCR counts,
  and exact safety reasons appear only when explicitly requested.

- Reworked normal monitoring into one in-memory awareness capture per pass.
  Submit, Teams notification, and New messages template checks reuse that
  frame; OCR is deferred to one unified safety packet only for a stable
  candidate, with a deliberate second packet only at the final live recheck.

- Added persistent no-repeat poll markers separate from general timestamp
  history. Once a timestamp begins a poll review, later scans reject it unless
  it belongs to that same active review, preventing an old or already-noted
  card from becoming a new action after a later scan or relaunch.

- Clarified the Last read safeguard: it is a navigation boundary only. It can
  reject a Submit above the divider, but it never establishes newness; every
  action still requires a stable live Submit, card-local fresh timestamp,
  no completion toast, and a final live recheck.

- Refined the idle subtitle to **“Awaiting the next signal”**: a quieter,
  more intentional status that keeps the app’s purpose discreet without
  obscuring its active state.

- Added an input-source safety layer for the pre-click handoff. A narrow flag
  marks the app's own dispatched click, while global mouse-down/key input and
  pointer movement are treated as user intervention and cancel the action.

- Added a user-override handoff before every Submit click. After moving to the
  target, Teams Bot waits 0.75 seconds; meaningful pointer movement cancels
  the click, records the override, and leaves that poll to the user.

- Fixed a Stop Monitoring cancellation race. Each monitor run now carries an
  invalidatable generation token; stopping immediately cancels in-flight
  scrolling and rechecks that token directly before the pointer moves and
  directly before a normal-monitor click.

- Bound fresh-timestamp evidence to the visible Submit card: only a
  date-qualified timestamp positioned above that card can validate it. A newer
  timestamp elsewhere in the chat can no longer authorize an old Submit.

- Refined monitor controls: inactive controls now use a neutral gray state,
  while the active Stop Monitoring control is blue for an immediate at-a-glance
  indication that monitoring is running.

- Added a visual Last read reference checkpoint for bounded chat searches. If
  the direct scrollbar jump is unavailable, finding the marker ends the old
  history sweep and performs only two final downward scrolls into newer chat.

- Switched the unified timestamp/Last read safety check to fast on-device OCR.
  These short UI labels do not need full-document accuracy, and this prevents
  the slow recognizer from repeatedly timing out normal monitoring.

- Added a Last read OCR safety gate to the unified pre-click check. A Submit
  above Teams’ visible Last read divider is recorded as old/read and skipped;
  a card below it still must prove a fresh date-qualified timestamp.

- Tightened poll safety: a visible Submit is now eligible only after three
  stable visual matches and one date-qualified Teams timestamp verified within
  the five-minute window. Unknown or old timestamps fail closed and are logged
  as skipped. This also removes repeated OCR calls from ordinary monitor passes.

- Confirmed the app never persists OCR screenshots or poll contents. Added a
  narrow startup cleanup for only its own stale temporary captures after a
  crash; the single small New messages reference image remains the sole
  persistent visual asset.

- Serialized native OCR requests so startup baseline, monitoring, and
  diagnostics cannot contend for Vision at the same time. A busy or late OCR
  pass now defers/retries without reporting a false monitor error.

- Improved monitor and continuous-scan failure reporting. Activity now shows
  the exception type even when macOS supplies an empty message, and a compact
  local traceback is appended to `diagnostic-errors.jsonl` for follow-up.

- Added a padded reference crop of the actual Teams **New messages** pill.
  Detection now uses this fast visual match before falling back to bounded OCR,
  while keeping the reference scoped to the Teams window.

- Scoped the visual New message(s) fallback to the visible Teams window before
  OCR, reducing false matches from other apps and improving its live click
  coordinate accuracy.

- Added a visual, live-position fallback for the Teams **New message(s)**
  control. When Teams does not expose that button through Accessibility, the
  bot now OCR-locates its text and clicks its calculated center instead of
  falling straight to wheel scrolling.

- When the Teams **New message(s)** indicator is detected, Teams Bot now
  presses that accessible control to jump to the newest activity before the
  normal bounded chat-bottom fallback and poll verification run.

- Added Teams' in-app **New messages** indicator as a secondary activity
  signal. It starts a newest-chat search, while the existing Submit, timestamp,
  confirmation, and five-minute safeguards still decide whether any action is
  permitted.

- Isolated Vision OCR in a short-lived native helper with a five-second limit.
  Continuous timestamp diagnostics now either complete and report their result,
  or stop with an explicit OCR timeout instead of accumulating hung scans.

- Renamed the sole distributed app from `TeamsBotDebug.app` to `TeamsBot.app` and moved it to the production bundle identifier `com.balexgt.teamsbot`.
- Made continuous timestamp diagnostics report one complete outcome for every completed OCR pass, including OCR lines read, date-qualified timestamps found, known timestamps ignored, bare times skipped, confirmation state, and any history update.
- Fixed a startup exit where a newly launched app could replay a previously queued menu-bar Quit command.
- Made continuous scanning resilient and inspectable: it now writes each completed pass to `~/Library/Application Support/TeamsBot/continuous-scan.jsonl`, shows changed outcomes immediately plus a five-second UI heartbeat, and clearly pauses itself if normal monitoring begins.
- Kept continuous timestamp scanning session-only: it is disabled on a full app quit and always starts off after relaunch. Trace events still record when scanning is enabled, paused, or disabled.
- Updated user-facing activity messages from “Debug” to “Diagnostics” to match the renamed controls.
- Added a generation-safe eight-second watchdog to continuous OCR scanning. It records every scan start and automatically replaces a hung Vision pass while discarding any late result from it.
- Replaced repeated external `screencapture` calls with direct Quartz screen capture for OCR, notification matching, and Submit matching to prevent continuous scans from hanging before Vision receives an image.

## 2026-09-13

### Changed

- Renamed the user-facing `Debug` menu/button to **Diagnostics**.
- Limited the optional idle Teams visit to Diagnostic mode. Normal monitoring no longer changes desktops merely because the Mac is idle.
- Changed normal notification-driven monitoring to begin at the newest end of the Teams conversation.
- Improved the continuous timestamp-scan activity log with outcomes, counts, and scan duration rather than timing alone.
- Prevented an in-flight continuous OCR worker from reporting a result after continuous scanning has been switched off.
- Moved continuous timestamp de-duplication ahead of confirmation processing, so known timestamps are ignored instead of being reconsidered.
- Added explicit reporting for bare Teams times (for example, `1:28 AM`) that are intentionally skipped because no date context is visible.

### Added

- `Test current poll` now reports a pre-bottom scan, bottom-position scan, and final pre-click scan.
- `Test current poll` first tries to set the rightmost Teams conversation scrollbar directly to its maximum via macOS Accessibility. It falls back to a bounded downward scroll sweep only when Teams does not expose that scrollbar.

## 2026-09-12

### Added

- Local persistent timestamp history and a poll activity history:
  - `~/Library/Application Support/TeamsBot/seen-timestamps.json`
  - `~/Library/Application Support/TeamsBot/poll-history.jsonl`
- Timestamp baseline at monitor start, timestamp de-duplication, five-minute poll age handling, and a two-scan confirmation requirement for continuous timestamp scanning.
- Continuous visible timestamp scanning with on-device Vision OCR.
- Poll-history viewer and detailed diagnostics menu.
- Dynamic Submit-button matching and dynamic pointer target calculation.
- Bounded Teams search/recovery when Submit is off-screen.
- Notification-banner visual detection as a local trigger.
- Submission confirmation check using Teams’ “Your response was sent to the app” banner.
- Distinct macOS sounds for a found poll and confirmed submission.
- Optional idle/media-aware Teams visit (later confined to Diagnostics mode on 2026-09-13).
- User-supplied robot logo in the app UI, Dock icon, and menu bar.
- Native menu-bar companion with Show/Hide, Start/Stop, and Quit actions.

### Changed

- Refined the UI toward a macOS-like presentation, including inactive-window transparency and less intrusive diagnostics.
- Replaced fixed click coordinates with live template matching and final pre-click re-location.
- Made `Test current poll` an explicit override that can ignore persisted timestamp/history eligibility in normal mode; Diagnostic mode still only previews the pointer target.
- Increased off-screen search from three small scrolls to a larger newest-first sweep.
- Reduced the continuous scan scheduling interval to roughly 0.55 seconds while retaining confirmation safeguards.

### Fixed

- Repeated Screen Recording prompts caused by stale/duplicate app bundles and macOS TCC identities.
- Repeated `TB` menu-bar items from orphaned helper processes. A single-instance lock now prevents duplicates for new launches.
- Native menu-bar crashes caused by status-item work in the Tk/Python process. The status item now runs in a separate Cocoa helper.
- Blank/dark macOS tooltip popups; replaced/removed unreliable tooltip behavior.
- Submit clicks landing at static or vertically incorrect coordinates.
- Old polls being reprocessed or treated as fresh by normal monitoring.

## 2026-09-11

### Added

- Initial local Teams Bot prototype and macOS app bundle workflow.
- Screen Recording and Accessibility permission guidance.
- Early monitor controls, debug/test mode, activity logging, and Submit-button image references.

### Changed

- Iterated from an early console-style prototype toward the current standalone Teams Bot application.
