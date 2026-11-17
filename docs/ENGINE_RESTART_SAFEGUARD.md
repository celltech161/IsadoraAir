# Operator-initiated engine restart safeguard (roadmap 1.20, revised)

An application-level admission check for the three places a Django operator can
ask for an engine restart. It does **not** change the engine, its Auto/Manual
semantics, restart recovery, microphone ownership, Remote DJ/WebRTC teardown,
systemd recovery, the protected updater or the protected broker.

## Entry points covered

| Entry point | Gated when | Override |
|---|---|---|
| Update Center `start_update` | the fresh trusted plan's `services_requiring_restart` contains `isadoraair-engine` (a Gunicorn-only plan reads no engine status) | superuser (the Update Center is already superuser-only to start an install); checkbox value must equal the target release id, plus a written reason |
| Monitoring `api_restart_check` | the check's unit is exactly `isadoraair-engine[.service]` (other units are untouched) | superuser; POST body `override=<exact unit>` + `reason` |
| Django Admin Audio Pipeline save | `sample_rate` or `program_gain_db` changed (other fields save as before and never read engine status) | superuser; the "Engine restart safeguard" checkbox + reason on that save |

## Assessment (`library/services/engine_restart_safety.py`)

Reads `/run/isadoraair/engine_state.json` (read-only). `level` is one of:

* **safe** – fresh status, no live microphone, no Remote DJ session/attempt, slot idle.
* **warning** – no block, but the engine is in Manual mode, or the Remote DJ slot is
  *quarantined* (an engine restart is the documented recovery for that). Proceeds
  without an override; the reason is shown.
* **blocked** – studio mic live; Remote DJ mic live; Remote DJ connected (even with its
  mic Off); reconnecting; an attempt in `connecting/connected/reconnecting` while the
  engine has not published the session (establishing); slot `occupied` or `cleaning`.
* **unknown** – missing, unreadable, non-JSON/non-object, stale (> 10 s), timestamp in the
  future (> 5 s), missing/mistyped required fields (`transport` and the booleans
  `mic_live`, `remote_dj_connected`, `remote_dj_live`, `remote_dj_reconnecting`,
  `manual_mode`), or an unrecognised `remote_dj_slot` / `remote_dj_connection.status`.
  Unknown is never "safe" and a stale block is also just unknown.

`blocked` and `unknown` require the override. The result carries the engine instance
(`engine_session_id`, `state_revision`) and observation time; it never copies the Remote DJ
owner block, attempt ids, tokens or signaling data.

## Override contract

Superuser only; explicit and specific to the requested action (release id / exact unit /
the Audio Pipeline save); per request (nothing is stored, no flag, nothing carries to the next
request); a written reason (≥ 8 characters). Each use writes `SystemEvent` rows (category
`restart_safety`, never coalesced): the override decision (operator, reason, engine state,
target) and, afterwards, the request outcome (`submitted`, `broker_rejected`,
`submission_uncertain`, `not_submitted_maintenance_lock`). Outcomes describe the *request*;
`completion_claimed` is always `false`. Refusals are audited too. Capability checks that
existed before (`monitoring.restart_service`, staff/superuser) still run first.

## Residual race (documented, not closed)

The check happens when the request is admitted. Between admission and the restart actually
running, a DJ may connect or a mic go live:

* Monitoring / Audio Pipeline: the broker request is made immediately after the check; the
  residual interval is the broker's acknowledgement + systemd stop (seconds).
* Update Center: the admitted job runs a multi-step installation (backup, migrations, static
  files, restarts) and the engine restart comes *later* in that sequence, possibly minutes
  after admission. A DJ connecting during that window is not re-checked, because re-checking
  at the restart step would require changes to the protected updater, which this work does
  not make. Operators should treat an admitted update's restart time as unprotected.
* A browser that holds a Remote DJ token but has not yet reached the engine (token issued,
  signaling not yet admitted) is invisible in engine status.
* A stopped/hung engine with no fresh status is `unknown`, so recovering it through Django
  needs the superuser override (systemd `Restart=on-failure` and root maintenance are
  unaffected).
