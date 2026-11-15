# Remote DJ automatic fresh-session reconnect and leave-page protection (roadmap 2.17 Package B)

Two browser behaviours on the full Console and the dedicated `/remote-dj/` page (one shared
`dashboard.html` script):

1. **Automatic fresh-session reconnect** (optional, station switch, **Off by default**): when THIS
   browser's established Remote DJ session ends without the operator asking for it, the browser
   may start a completely new session on its own.
2. **Leave-page protection** (always on): a page that owns a live or establishing Remote DJ
   connection warns before the operator navigates away.

This is not ICE restart or same-session migration: the existing B1/B1.1 same-session recovery
(the engine's muted "Reconnecting" grace) is unchanged and always comes first. A new session is
considered only after the old one has definitively ended.

## Station switch

`Remote DJ Configuration → Reconnect / recovery → Enable automatic Remote DJ reconnect`
(`RemoteDJConfig.auto_reconnect_enabled`, migration `library.0103`, default False). Audited with
the existing configuration audit (old/new values, apply mode `next_remote_dj_reconnect_decision`,
no restart). It is read fresh by the policy endpoint before every attempt, so switching it off
stops a sequence already in progress at its next decision. Remote DJ's own master switch
(`enabled`) must also be on.

## Evidence that a session ended, and why (engine)

The engine now records, on each connection attempt (published in `remote_dj_connection`, which
every status reader already receives), **why** the session ended and whether an Emergency Mute was
latched at that moment:

| `end_reason` | Set by |
|---|---|
| `signaling_closed` | the signaling WebSocket closed (browser or network) |
| `peer_closed` | the WebRTC peer connection reached CLOSED |
| `failed` | a recorded failure (`failure.class`, e.g. `ice_state` for an unrecovered transport loss) |
| `kicked` | administrator Kick |
| `authorization_revoked` | the periodic re-authorization ended the session |
| `engine_shutdown` | engine stop |
| `session_stopped` | any other stop (treated as ambiguous: never retried) |

The first recorded reason wins; a later duplicate stop never rewrites it. The engine also publishes
`remote_dj_slot`, the r0132 persistent-slot admission state: `available`, `cleaning` (the previous
session's bounded native cleanup is still running), `quarantined` (cleanup failed or blocked:
engine restart required) or `occupied`. None of this carries a token, key, SDP or address, and the
end reason never changes what a stop does.

The server cannot tell a browser's own Disconnect from a dead network connection (both close the
socket), so the **browser** decides whether a loss was involuntary: it alone knows it did not call
Disconnect, Cancel or leave the page. The server decides whether a new session is permissible.

## Policy endpoint

`GET /api/remote-dj/reconnect-policy/?attempts=<lost>[,<replacement>…]` (at most 4 ids; under the
already-seeded `/api/remote-dj/` group access) answers `{"decision", "reason"}`, `Cache-Control:
no-store`:

* `stop` — invalid request, no `remote_dj.connect` capability or outside the schedule window,
  master switch off, automatic reconnect off, Remote DJ not running, slot quarantined, an engine
  that cannot report slot state, another session active, the engine's last attempt is not one of
  ours (another browser/operator, or an engine restart), a Kick / revocation / engine shutdown /
  unclassified end, Emergency Mute latched at the end, or a refused token.
* `wait` — engine status unavailable or stale, our lost session still finalizing, or its native
  cleanup still running (`cleaning`): ask again, without spending an attempt.
* `retry` — none of the above: start ONE new attempt.

It authorizes nothing itself: every attempt still needs a fresh token (capability + schedule) and
normal admission. The browser treats anything other than a readable `retry` (error, timeout,
malformed body) as not authorized.

## Browser sequence

Starts only when an **established** attempt (its ICE reached connected) ends with a local terminal
signal the page did not cause: the server closing the signaling socket, or ICE `failed`/`closed`.
Never after Disconnect, Cancel, a Kick message, leaving the page, or a failed first Connect.

| Bound | Value | Why |
|---|---|---|
| Attempts per qualifying loss | 3 | the workorder's suggested ceiling |
| Backoff before attempt 1 / 2 / 3 | 1 s / 2 s / 4 s | spaced, short enough for a live show |
| Establishment timeout per attempt | 30 s | token → WebSocket (may sit in CONNECTING on cellular) → offer → ICE → engine ownership |
| One policy request | 4 s | then it counts as "not retry" |
| Re-ask while waiting | 1 s | old session finalizing / cleaning; spends no attempt |
| Whole-sequence deadline | 150 s | old session's recovery grace (≤ 30 s) + r0132 native cleanup (≤ 10 s) + 3 × (backoff + 30 s) |
| Fresh budget | only after a replacement stayed established ≥ 60 s | an unstable link cannot loop |
| Sequences per page per hour | 6 | outer bound |

Each attempt is a complete new session: new token and attempt identity, new WebSocket, new
`RTCPeerConnection`, new microphone stream and AudioContext. A replacement counts as established
only when its ICE is connected AND a trusted engine snapshot names this page's attempt as the owner;
until then the Connect button shows the sequence (`Reconnecting…`, `Waiting for connection…`,
`Retrying N of 3`) with the action **Cancel reconnect**, and the Remote Mic gate is unavailable.
The Remote Mic is Off in every new session (the engine opens every session muted), the old
session's gate commands are refused by the engine (exact attempt/session binding), and an old
control key is never reused (each token response carries its own).

Every timer, policy response, token response, socket event, offer step and `getUserMedia` result
is bound to its attempt (`rdjAttemptSeq`) and its sequence (`rdjRetry.gen`): releasing an attempt
or ending a sequence makes all of its late callbacks inert, and a stream that arrives after its
attempt was released is stopped immediately.

When a sequence stops, the button briefly says why (`Reconnect failed`, `Reconnect cancelled`,
`Disconnected by the station`, `Muted by the station`, `Reconnect not permitted`, `Remote DJ
unavailable`, …); a manual Connect is always available afterwards, subject to normal admission.

## Browser resources

Every local release now goes through one function (`rdjReleaseLocal`) that closes the
`RTCPeerConnection`, the WebSocket and the AudioContext exactly once, **stops the microphone
tracks** (before this package only the AudioContext was closed, which leaves a `getUserMedia`
capture — and the browser's microphone indicator — alive), clears the monitor element, and
invalidates the attempt. The separate calibration workflow is unchanged.

## Leave-page protection

While the page owns an active or establishing connection, or a pending automatic reconnect:

* **Same-tab links and form submissions** that no page handler already took get a dialog: "You
  have a live Remote DJ audio session in progress. Are you sure you want to leave this page?
  Leaving will disconnect your microphone and station monitor audio." — **Stay on this page**
  (default focus, also Esc) or **Leave and disconnect**. Staying changes nothing (same attempt,
  audio, microphone, Remote Mic state, retry state). Leaving ends any reconnect sequence,
  disconnects, removes the native guard (no second prompt) and navigates.
* **Everything else** (Back, Reload, closing the tab or window, typed URLs, script navigation)
  gets the browser's own leave-page prompt from a `beforeunload` handler installed only while the
  page owns a connection and removed as soon as it no longer does. Browsers show their own generic
  text there, not ours, and require a prior user interaction on the page; some mobile exits (app
  switch, OS tab discard) show nothing at all.
* Links with a non-`_self` target, downloads, in-page anchors, non-HTTP links, modified clicks
  (Ctrl/Cmd/Shift/Alt/middle) and ordinary in-page controls never prompt. A page that only
  observes another browser's session never prompts.
* `pagehide` releases the connection best-effort (so the station frees the slot promptly) and
  forbids a reconnect; it is never relied on as cleanup — mobile browsers may not deliver it.
  Backgrounding a tab (visibility change) does not disconnect.

## Verification

* Server/engine: `library/tests/test_remote_dj_auto_reconnect_2_17b.py` (end reasons, slot state,
  Kick/revocation/Emergency Mute, Remote Mic Off in a new session, every policy decision, Admin and
  audit) plus `test_config_audit_admin`.
* Real browser: `library/tests/browser_remote_dj_reconnect_check.py` against pages rendered in the
  sandbox by `library/tests/render_remote_dj_console_artifact.py` (Playwright cannot launch inside
  the validation sandbox). See the module docstrings for the exact commands.

## Known limitations / future (Package C)

* A page reload or navigation always ends the session (the media lives in the page); a persistent
  dedicated-window/shared-worker owner is Package C.
* The native leave-page prompt's wording and presence are browser-controlled.
* After an engine restart the engine has no record of the lost attempt, so no automatic reconnect
  follows (by design: ambiguous ⇒ no retry).
* An Emergency Mute latched when the session died stops the sequence; the DJ reconnects manually
  (the new session starts unlatched, exactly as a manual reconnect does today).
