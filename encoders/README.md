# Encoders

Outbound streaming destinations (Icecast/Shoutcast) rendered through a
hardened Liquidsoap backend. One `Encoder` row = one destination; rows
sharing an `input_device` are bundled into a single shared Liquidsoap
subprocess (`isadoraair-encoders.service`, `encoders/services/
encoder_manager.py`) — see that module's own docstring, and
`PROJECT_NOTES.md`'s "Encoder monitoring hardening" section, for the
full Phase 2/3 candidate-validation / preflight / qualification /
automatic-rollback / per-input-device-reconciliation architecture this
app is built on. This file does not re-explain that architecture; it's
the operator-facing setup guide for the two things most people editing
this app actually need: **destination provider presets**, and what to
expect after saving one.

## Provider presets

`Encoder.provider` is a preset layered **on top of** the generic
Icecast/Shoutcast model — it is never a third streaming protocol.
Live365 is fundamentally an Icecast source destination; Radio.co is
fundamentally a Shoutcast-1-style external-broadcaster source
connection. Selecting a provider only narrows:

- which `protocol`/`format`/MP3 rate-mode combinations are accepted
  (`encoders/services/validation.py`'s `validate_provider_policy`)
- how IsadoraAir verifies the destination is actually connected
  (`monitoring/services/probes.py`'s `evaluate_encoder_group_health` —
  see "Destination health" below)

It never changes what gets rendered to Liquidsoap — a Live365 row
still renders through `output.icecast()`, a Radio.co row still renders
through `output.shoutcast()`, exactly like a Generic row using the
same protocol.

### Live365

1. In your Live365 dashboard, get your **LiveDJ / source credentials**
   (Broadcasting → Source setup): hostname, port, mount, username,
   password.
2. In Django admin (**Encoders → Encoders → Add**), set:
   - **Provider**: Live365
   - **Protocol**: Icecast (required — Live365 rejects anything else
     at the provider-validation layer, with a message naming Live365
     explicitly, before it ever reaches Liquidsoap)
   - **Host / Port / Mount / Username / Password**: exactly what
     Live365 gave you
   - **Format**: MP3 or AAC only (Ogg Vorbis is rejected for this
     provider, even though the generic Icecast path supports it)
   - **Bitrate**: match what your Live365 account/stream expects
   - **MP3 Rate Mode**: leave on **Auto** if your bitrate is 192 kbps
     or higher (Auto already resolves to CBR there); otherwise set it
     to **CBR** explicitly. Live365's current guidance is CBR for MP3
     source audio, and this is enforced — an ABR MP3 Live365 row is
     rejected with a message telling you to fix the rate mode.
3. Save. See "After saving" below for what happens next.
4. **Artist/Title accuracy matters here more than for a typical
   stream** — Live365 relies on it for licensing/royalty reporting.
   IsadoraAir's existing ICY metadata path (the same one every other
   destination already uses) feeds this; nothing provider-specific was
   built for it. Verify real Artist/Title display during acceptance
   testing (checklist below) rather than assuming it's correct.
5. Live365 ad-trigger files / revenue-share ad insertion are **not**
   part of this integration — this only sets up basic source transport
   and metadata. If Live365-side ad insertion needs signaling from
   IsadoraAir later, that's separate, future work.

### Radio.co

1. In your Radio.co station's dashboard, get your **external
   broadcaster / source credentials**: host, port, password. (No mount
   or username — Shoutcast 1 doesn't use either, and the admin form
   doesn't require them for this provider.)
2. **Configure Radio.co to permit the external source before you
   connect**: your Radio.co station needs a Live DJ event scheduled,
   or (for IsadoraAir running continuously) **Live Anytime** enabled,
   wherever your Radio.co plan supports it. If Radio.co doesn't expect
   a live source right now, it will reject or ignore the connection
   regardless of how correctly this side is configured.
3. In Django admin, set:
   - **Provider**: Radio.co
   - **Protocol**: Shoutcast 1 (required — Radio.co rejects Icecast
     and Shoutcast 2 at the provider-validation layer)
   - **Host / Port / Password**: exactly what Radio.co gave you
   - **Format**: MP3 (only format supported for this provider — AAC
     and Vorbis are both rejected even though Radio.co can distribute
     AAC elsewhere in its own platform; this is specifically about
     what the external-broadcaster source ingest accepts)
   - **MP3 Rate Mode**: same Auto-at-192-or-CBR rule as Live365 above
     — Radio.co source audio must be CBR.
4. Save. See "After saving" below.
5. **IsadoraAir remains the schedule/automation authority.** This
   integration does not mirror IsadoraAir's playlist/schedule into
   Radio.co, does not manage your Radio.co account, and does not build
   a Radio.co-side AutoDJ. Radio.co's own fallback/AutoDJ behavior
   (when IsadoraAir's source disconnects) is entirely Radio.co-side
   configuration, outside this integration's scope.

### After saving (either provider)

Same as any other Encoder edit — nothing provider-specific here. The
encoder manager discovers the change itself on its next ~5s
reconciliation tick, validates and preflights a fresh candidate for
just that row's `input_device` group, and only replaces that group's
live child once the candidate proves itself healthy for a sustained
period. Every other group keeps running untouched throughout. Watch
the status banner on the Encoders changelist (or `/monitoring/`) for
progress; a rejected or still-probationary configuration is reported
there, in plain language, with the actual validation/health reason.

### Destination health

Every destination's connection state is now verified two ways,
depending on what kind of destination it is:

- **A normal Shoutcast 1/2 destination with Provider = Generic**: the
  existing external Shoutcast DNAS `/statistics` probe — independent
  of anything IsadoraAir self-reports, and the same check that caught
  a real production outage before this feature existed. Unchanged.
- **Everything else** (Icecast of any provider, including Live365; and
  Radio.co, which uses the Shoutcast 1 wire protocol but is not
  assumed to expose a normal DNAS statistics endpoint just because of
  that): a generic Liquidsoap output connection-state signal — each
  destination's own `on_connect`/`on_disconnect` callback, fed into
  the same generation-guarded runtime state file every other
  audio/health signal already uses. A destination that's never
  connected, or whose connection drops, reads as unhealthy
  immediately; it is never assumed healthy just because the process is
  alive and audio is flowing — a wrong password reconnecting forever
  must never be promoted to last-known-good.

Both paths feed the exact same aggregate health check used for
ordinary monitoring, candidate qualification, *and* rollback
qualification — there is no separate, provider-specific notion of
"healthy."

## Real provider acceptance checklist

This integration has been validated by inspection, static Liquidsoap
checks, and offline test coverage — **not** against real Live365 or
Radio.co accounts. Do not consider either provider fully certified
until someone has walked through its checklist below against a real
account, during a controlled/non-critical time.

### Live365

1. A provider-configured Encoder row validates in Django admin with no
   errors.
2. The candidate launches (check `/monitoring/` / Recent Events).
3. Liquidsoap reports the destination connected (generic
   connection-state signal, not DNAS).
4. The candidate qualifies and is promoted to last-known-good.
5. Live365 recognizes the incoming source (visible in the Live365
   dashboard).
6. Format/bitrate actually received by Live365 matches what was
   configured.
7. Artist/Title displays correctly on Live365's side.
8. Consecutive song changes update Artist/Title correctly (not just
   the first one).
9. A network interruption reconnects cleanly (Liquidsoap's own
   reconnect logic, already relied on elsewhere in this project).
10. A wrong password fails candidate qualification and leaves the
    previous last-known-good configuration running, untouched.
11. Observe and document how non-song programming (commercials,
    syndicated programming, weather, imaging, manually inserted
    material, live/unknown programming) is represented on Live365's
    side — do not assume metadata suppression rules; record what's
    actually observed.
12. No unrelated encoder group is interrupted during any of the above.

### Radio.co

1. The Radio.co station's account/settings actually permit a live
   external source (Live DJ event scheduled, or Live Anytime enabled).
2. A provider-configured Encoder row validates in Django admin with no
   errors.
3. The candidate connects and Liquidsoap reports it connected.
4. MP3 CBR is confirmed on Radio.co's side (not just configured here).
5. Artist/Title displays correctly on Radio.co's side.
6. Radio.co's own service-side fallback occurs correctly when
   IsadoraAir's source disconnects (Radio.co-side behavior, outside
   this integration, but worth confirming it's configured sanely).
7. IsadoraAir reconnecting resumes the source cleanly on Radio.co's
   side.
8. A wrong password fails candidate qualification and leaves the
   previous last-known-good configuration running, untouched.
9. A Phase 3 configuration edit (e.g. a bitrate change) reconciles
   correctly against the real Radio.co endpoint.
10. No unrelated encoder group is interrupted during any of the above.
