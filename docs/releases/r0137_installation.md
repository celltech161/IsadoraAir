# r0137 installation, verification and recovery

Publication does not authorize deployment. This runbook applies only after separate authorization for the named
station. KOGR and WRJE must be authorized, preflighted and accepted independently. Publishing r0137 is not
authorization to install it, to run the Update Center, or to restart any service.

## Exact identity and trusted plan

- Previous release: r0136 at `7c1df8f8d30c396fd01cfb6ded7d2240ecfa8fa3`.
- Accepted engineering history, in exact order:
  - `be55e0769b710aa2c0e028da51309433386abea9`;
  - `0990674a2f32dbdba951be8dd6113e275dcaca2c`;
  - `f99ceaf6dec9ec97a87f567cdc604be2b9cb0240`;
  - `33e86dd72252997f6a5c14cd22554f03e8e2cdd9`;
  - `5dba81ea8f77640fba9d0528b9b1b7b78047e98a`;
  - `3cb3d4775cfe6f7856e831f3ed5a0601043cd931`;
  - `cf01851f0e10192681c48839e5d0731874bc9e14`, tree `d482ba2f71411c9f7b87f815fd17b76615fa0ef9`.
- R is the unique canonical commit introducing `deploy/releases/r0137.json`, directly after that accepted history.
- A station already at r0136 must preview exactly one release, r0137: advance source to R, apply no migrations or
  dependency changes, and restart `isadoraair-gunicorn`, then `isadoraair-engine`, then `isadoraair-monitoring`.
- There is no migration-authorization companion, protected-runtime transition, collectstatic, nginx or unit action.
- A station below r0136 must include every intervening release and its own actions. Do not shorten that plan.

Never replace source directly, install an engineering branch, rebuild an equivalent commit, or manipulate the live
checkout. Use only the protected Update Center after separate station authorization.

## Common read-only preflight

1. Establish the installed release from the Update Center installation record and verify the installed source is
   clean. Do not infer release identity from `HEAD` alone.
2. Require `schema_current`, no pending intervention, and no running Update Center job.
3. Verify current backups and recovery readiness, protocol compatibility, trusted updater health and protected-runtime
   health.
4. Record service PIDs and activation times for Gunicorn, engine, encoders, Monitoring, RBDS, Active Messages and
   spoken rendering. All must be healthy before starting. Record program audio and encoder connection state.
5. Record the current and next approved hourly logs, current item, queue position and whether a log build is running.
   Avoid installation near the top of the hour or during a current or next-hour build.
6. Confirm there is no pending urgent insertion, pending Weather or AMBER announcement, or native Active Message. If
   one is pending, defer until it airs.
7. Confirm neither polling process is running and neither is about to start (see Polling services below), so no
   alert state is being updated during cutover.
8. Verify source-snapshot validity, ownership and permissions (see Legacy WxAlert compatibility below).
9. Record current Weather diagnostics, including any existing alert-related facts.
10. Confirm `remote_dj_connected: false`. If a Remote DJ session is active, defer or obtain the presenter's explicit
    agreement that the engine restart will end the session.
11. Preview the exact target R, release chain, action fingerprint and plan. From r0136 it must contain only source
    advancement and the Gunicorn, engine, then Monitoring restarts.

An engine restart interrupts program audio and may end an active Remote DJ session. Schedule it at an operationally
acceptable time and keep an operator present.

## Legacy WxAlert compatibility

Inspect read-only, and record, before installation. Do not delete or alter Library records or station data.

- The retired `WxAlert/wx_alert.mp3` Library Track: whether it exists and its Ready-to-Air status. It should not stay
  eligible; retiring it is an operator decision (clear Ready to Air or remove it from rotation), made before the new
  workflow is in service. r0137 never selects it.
- Pending or unclaimed legacy WxAlert `LogItem` rows for any WxAlert Track.
- The canonical `WxAlert/wx_alert.flac` Track and its exact Library path, if it exists. It may legitimately not exist
  until the first announcement is produced.
- The Weather data directory and the publication lock: the engine, Gunicorn, Monitoring and the ingestion processes
  must each be able to read the source snapshots (`active_watches_warnings.json`, `active_amber_alerts.json`) and
  `wx_alert_state.json`, and the user that runs the pollers and coordinator must be able to write them and take
  `.wx_alert.flac.publish.lock`.

```bash
cd /opt/isadoraair && venv/bin/python manage.py shell -c "
from library.models import Track, LogItem
for t in Track.objects.filter(category__code='WxAlert').order_by('id'):
    print(t.id, t.filepath, 'ready2air=%s' % t.ready2air,
          'pending_rows=%d' % LogItem.objects.filter(track=t, played_at__isnull=True,
                                                      playback_claimed_at__isnull=True).count())
"
```

## Polling services

Verify station by station; do not assume KOGR and WRJE match.

- The NWS polling unit invokes the correct `update_local_wx_data.py`.
- The AMBER polling unit invokes `amber_poll.py`.
- No installed timer or service directly invokes the former independent AMBER renderer (`amber_alert.py` is only a
  wrapper that runs the coordinator): check `systemctl cat amber-alert-poll.service` and search the installed units
  for `amber_alert`. These units are installed per station and are not part of `deploy/`.
- `AmberAlertConfig` has the correct enabled state, IPAWS URL, event codes, SAME coverage and station preferences.

Neither polling service may be running during the cutover. They are scheduled one-shot processes, so their next
invocation after installation loads the new source; no unit or timer change is made.

## Installation

After separate authorization, reconfirm the exact target and fingerprint immediately before starting. Use the
protected Update Center once. It advances source to R and restarts:

1. `isadoraair-gunicorn`
2. `isadoraair-engine`
3. `isadoraair-monitoring`

No migration, package, requirements, static, nginx, systemd-unit or protected-runtime action is expected. Do not
restart any service manually and do not retry automatically after a failure.

### Defer installation when

- an alert transition or a pending urgent insertion is underway;
- a polling process is updating alert state;
- an approved-log build or the top-of-hour transition is underway;
- an Update Center job is active;
- a Remote DJ session would be unexpectedly interrupted;
- the operator cannot safely accommodate an engine restart, which can interrupt program audio. It requires a
  separately authorized operational window.

## KOGR

Preserve KOGR's complete Roadmap 2.27 Hour Refill observation-only state:

- Hour Refill remains observation-only, with `dry_run_only` checked;
- host installation arming disabled and `HOUR_REFILL_INSTALL_ARMED` false;
- existing cause and capture switches, observation evidence and scheduled evidence-collection timers preserved;
- current Log fill config and existing Weather and urgent-insertion configuration preserved.

Record the policy row, runner status, latest evidence and timers before installation. The engine restart is an
authorized interruption of Hour Refill observation, not a reset. Afterward, confirm the runner resumes in
observation-only mode and the next scheduled evidence collection succeeds. Do not arm 2.27, clear `dry_run_only`,
reset evidence or reschedule timers.

## WRJE

WRJE requires its own authorization and preflight. Do not assume it matches KOGR. Verify independently its installed
release and clean checkout, the AMBER polling service and its paths, the IPAWS feed configuration and SAME coverage,
snapshot and data-directory permissions, the canonical Library Track and legacy MP3 Track, Remote DJ state, and
Monitoring health. Success on KOGR is neither authorization nor evidence for WRJE.

## Passive post-install acceptance

Acceptance on air is passive. Do not inject synthetic CAP or NWS alerts into production authoritative paths, generate
a test alert, alter an approved log or force a failure. Validate combined-content, cancellation and rollover
scenarios in isolated, non-airing environments.

1. Verify the Update Center job succeeded.
2. Verify the installed release is r0137, source is exactly canonical R and the checkout is clean.
3. Verify the restart sequence was Gunicorn, then the engine, then Monitoring, and that encoders, RBDS, Active
   Messages and spoken rendering kept their recorded identities and remain healthy.
4. Confirm no migration, dependency, static, nginx or unit action occurred.
5. Confirm normal program audio and encoder operation after the engine restart.
6. Confirm the current and next hour queue state: the approved log and queue position are correct, no aired item
   starts again, and the top-of-hour handoff occurs once.
7. Confirm both pollers run normally on their schedules.
8. Confirm `wx_alert_state.json` is created after both authoritative source snapshots first become available, and
   that Weather Diagnostics shows its fact.
9. Confirm no unchanged preexisting alert is announced: the first complete pass records a silent baseline.
10. Confirm the retired MP3 Track is not selected.
11. Confirm Weather diagnostics are healthy and no alert is unexpectedly suppressed.
12. Confirm station-specific operating policies are unchanged. On KOGR, confirm Hour Refill observation resumes.

If a genuine alert occurs naturally, confirm one combined announcement airs at a track boundary, is not repeated, and
is withdrawn when the alerts end.

## Stop and escalation conditions

Stop, preserve the Update Center job, engine journal, engine state, alert snapshots, `wx_alert_state.json` and
Monitoring evidence, and escalate on:

- any plan or action beyond source advancement and the three declared restarts;
- target or fingerprint mismatch;
- engine start failure, missing audio or encoder disconnection;
- overlapping or duplicate starts;
- an unchanged alert announced repeatedly, or an obsolete announcement airing after its alert ended;
- the retired MP3 Track being selected;
- a pending urgent insertion that disappears;
- a snapshot, state file or publication lock that a process cannot read or write;
- an unexpected Weather diagnostics failure or Log fill, Active Message or Hour Refill policy change;
- KOGR observation or evidence collection not resuming.

No automatic rollback is promised and no automatic source rollback is authorized. A further engine restart is not
harmless: it interrupts audio again and may end another Remote DJ session. Do not reset Git, edit station data or
Library records, replace media, restart services ad hoc or manipulate the source tree as recovery.

## Remaining limitations

- The limitations listed in the release notes apply: silent first baseline, polling latency, the 25-candidate
  unclassified retrieval bound, differently classified cancellations ending at expiration, delayed replacement on a
  persistently malformed message, possible loss of a carried previous-hour pending announcement on an engine restart,
  and the forward-moving host-clock assumption.
- Installation has not been accepted on real station playout until the passive checks above are completed under a
  separate authorization.
