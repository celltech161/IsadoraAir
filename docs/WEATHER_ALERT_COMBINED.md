# Combined Weather/AMBER spoken alert (r0137)

One spoken announcement, `WxAlert/wx_alert.flac` (one Library Track, title **Weather Alert**), contains every
currently valid NWS watch/warning and AMBER-family alert. It is an ordinary track inserted at the next track boundary;
nothing is mixed over, ducked or interrupted, and the Weather Alert Beep is unchanged.

## Pieces

| Piece | Role |
|---|---|
| `weather_ingest/update_local_wx_data.py` | NWS poll (every 5 min). Writes `active_watches_warnings.json` on a successful fetch only: Watch/Warning entries in effect (not `status` Test/Exercise, not `messageType` Cancel, not VTEC CAN/EXP/UPG, not a non-operational product), with `key` (VTEC office.phen.sig.etn), `expires`, `ends`, and the unchanged `text`/`text_core`, sorted most severe first. Then runs the coordinator, then (only on a headline change, as before) `current_temp.py`. |
| `weather_ingest/amber_poll.py` | IPAWS poll (every 5 min). Writes `active_amber_alerts.json` only when the feed AND every candidate CAP message were retrieved (a retrieval failure keeps the previous snapshot); keeps only `status` Actual Alert/Update messages and drops everything a Cancel or Update references; writes `[]` when AMBER is disabled. Then runs the coordinator. |
| `weather_ingest/wx_alert.py` | **The coordinator** and the only producer of `wx_alert.flac`. `amber_alert.py` is a wrapper that runs it. |
| `WEATHER_DATA_DIR/wx_alert_state.json` | The coordinator's record (`isadoraair/wx_alert_state.py`): `generation`, content `fingerprint`, `active`, `valid_until`, `published_at`, the published file's `audio` identity, `sources`, `excluded`, `last_error`. |
| `library/services/wx_alert_playout.py` + engine | Idempotent synchronization of `insert_urgent` WxAlert nudges, the final deck-open check, and carrying a pending occurrence across hour adoption. |

## Coordinator pass (under `WEATHER_DATA_DIR/.wx_alert_coordinator.lock`, bounded wait)

1. Read both snapshots. A missing snapshot means "no known alerts from that source"; an unreadable one stops the pass
   with nothing changed (`last_error` recorded). An NWS snapshot written before r0137 (entries without `key`) also
   stops the pass, benignly, until the next NWS poll rewrites it -- so the first-run baseline is not decided from it.
2. Drop entries past their authoritative end (NWS: earliest of `expires`/`ends`; AMBER: `expires`) -- this keeps
   working during a feed outage -- and entries with no authoritative end at all (conservatively excluded and listed
   in `excluded`; there is no arbitrary validity extension).
3. Fingerprint the spoken content: `[source, alert identity, exact spoken text]`, NWS first, then AMBER sorted by
   event and identifier. Advisories, headlines and feed order never change it.
4. * **First run** (no record): record the current content as an un-announced baseline (the existing "first run is
     silent" rule). Every later genuine change is announced.
   * **Unchanged**: no synthesis and no publication. While an announcement is active, refresh `valid_until` if the
     sources extended it and send the idempotent engine nudge (this is the retry path).
   * **Nothing in effect**: new generation, `active: false`. No pending occurrence may start.
   * **Changed**: synthesize every clip (all-or-nothing), concatenate one calibrated FLAC, re-check that the sources
     did not change meanwhile (else discard the candidate unpublished; the next pass publishes the latest), publish
     through `publish_weather_asset` (atomic rename, last-known-good rollback), record the new generation
     (`published_at`, `audio`, `valid_until` = earliest end), nudge the engine.
   A failed synthesis, publication or nudge never advances the record; the next poll cycle retries.

## Engine

* **Synchronization** (`insert_urgent` with category `WxAlert`): the payload is ignored; the engine reads the record.
  It queues one occurrence of the exact `wx_alert.flac` Track only if the generation is active, unexpired and its
  recorded audio is the file on disk, no unclaimed occurrence is already pending (that one will play the newest audio),
  and no occurrence has claimed playback since `published_at` (persisted claims: an engine restart does not repeat
  an announcement that already started). Other `insert_urgent` categories are unchanged.
* **Deck open** (`_deck_open_authority`, WxAlert occurrences only): takes the publication's own lock
  (`.wx_alert.flac.publish.lock`) SHARED and non-blocking and holds it until `filesrc` has opened the file; refuses
  while a publication is in progress, when the record is missing/inactive/expired, when the file on disk is not the
  recorded generation's audio, for any Track other than the canonical FLAC, for a second occurrence of an already
  claimed generation, and for a recreation (resume/seek) of an occurrence that began an earlier generation. It re-reads
  the Track so the deck uses the duration/next-start of the audio it actually opens. A refused occurrence is skipped
  (info event "Weather/AMBER announcement occurrence not opened"); the next nudge queues a current one if still due.
* **Hour adoption**: `_advance_to_next_hour_log` moves a pending, unclaimed occurrence into `_forced_next_items` (same
  LogItem, ahead of any other forced item, as an urgent insertion already is). The next hour's approved log is not
  modified. Exhausted-queue rollover has nothing ahead of the cursor to carry.

Already opened or airing audio is never touched: the deck keeps the file it opened (atomic rename; see
`library/tests/test_wx_alert_gstreamer_single_file.py`).

## Known limitations

* An occurrence carried into `_forced_next_items` from a previous hour's log is not restored after an engine crash or
  restart (no new persistence); the next coordinator nudge re-queues a still-valid generation only if no occurrence
  claimed it.
* A claim followed by a deck that fails to open counts that generation as delivered.
* Validity follows the snapshot with up to one poll interval of lag (5 minutes), bounded by `valid_until`.
* Alerts present at the first run after installation are recorded as the baseline and are not announced until the
  effective content changes.

## Read-only preflight before release acceptance (each station)

1. Old MP3 / canonical FLAC Tracks (read-only):

   ```bash
   cd /opt/isadoraair && venv/bin/python manage.py shell -c "
   from library.models import Track, LogItem
   for t in Track.objects.filter(category__code='WxAlert').order_by('id'):
       print(t.id, t.filepath, 'ready2air=%s' % t.ready2air,
             'pending_rows=%d' % LogItem.objects.filter(track=t, played_at__isnull=True,
                                                         playback_claimed_at__isnull=True).count())
   "
   ```

   Expected: the old `WxAlert/wx_alert.mp3` Track (if any) has `ready2air=False` (retired per
   `docs/WEATHER_SPEECH_FLAC.md`). r0137 never selects it, but it should not stay eligible.
2. Only `amber_poll.py` is scheduled for AMBER (never `amber_alert.py` directly):
   `systemctl cat amber-alert-poll.service | grep ExecStart` and `grep -rl amber_alert /etc/systemd/system`.
   The AMBER poll timer/service are station-installed (not in `deploy/`); check KOGR and WRJE separately.
3. After installation, the first coordinator pass (within 5 minutes) writes `wx_alert_state.json`; Weather
   Diagnostics shows it as the `wx_alert_state` fact.
