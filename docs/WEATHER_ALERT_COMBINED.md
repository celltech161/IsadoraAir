# Combined Weather/AMBER spoken alert (r0137)

One spoken announcement, `WxAlert/wx_alert.flac` (one Library Track, title **Weather Alert**), contains every
currently valid NWS watch/warning and AMBER-family alert. It is an ordinary track inserted at the next track boundary;
nothing is mixed over, ducked or interrupted, and the Weather Alert Beep is unchanged.

## Pieces

| Piece | Role |
|---|---|
| `weather_ingest/update_local_wx_data.py` | NWS poll (every 5 min). Writes `active_watches_warnings.json` on a successful fetch only: Watch/Warning entries in effect (not `status` Test/Exercise, not `messageType` Cancel, not VTEC CAN/EXP/UPG, not a non-operational product), with `key` (VTEC office.phen.sig.etn), `expires`, `ends`, and the unchanged `text`/`text_core`, sorted most severe first. Then runs the coordinator, then (only on a headline change, as before) `current_temp.py`. |
| `weather_ingest/amber_poll.py` | IPAWS poll (every 5 min). Writes `active_amber_alerts.json` only when the feed AND every candidate CAP message were retrieved AND interpreted (a transport failure, unparseable XML, a missing/empty/undefined mandatory root element -- identifier, sender, sent, status, msgType, scope --, malformed `<references>` (any entry that is not a well-formed `sender,identifier,sent` with a valid CAP dateTime; one bad entry rejects the whole message), an Actual Alert/Update without `<info>` or whose `<info>` lacks a valid category, event, urgency, severity or certainty, or an Actual Cancel referencing nothing keeps the previous snapshot; optional CAP elements are not required). Candidates are the feed entries classified with a configured event AND state, plus -- only while an Actual alert of interest is held -- entries whose summary LACKS an event and/or state classification (an info-less Cancel can look like that); at most `MAX_UNCLASSIFIED_CAP_FETCHES` (25) of those per cycle, above which the cycle keeps the previous snapshot. Entries classified with another event or state are never fetched. Root fields are parsed without `<info>` (a Cancel may have none). Only `status` Actual messages have authority: an Actual Cancel retires what it references, an Actual Update supersedes what it references (chains, any feed order). A reference names a message by its complete CAP 1.2 identity -- the referenced sender, identifier and sent time -- never by identifier alone: sender and identifier match exactly, `sent` is compared as the instant it denotes (16:00-06:00 equals 22:00+00:00), and the referencing message's own sender is irrelevant; another sender's alert sharing an identifier is left active; Test/Exercise/System/Draft messages never air and never cancel or supersede anything. Writes `[]` when AMBER is disabled. Then runs the coordinator. |
| `weather_ingest/wx_alert.py` | **The coordinator** and the only producer of `wx_alert.flac`. `amber_alert.py` is a wrapper that runs it. |
| `WEATHER_DATA_DIR/wx_alert_state.json` | The coordinator's record (`isadoraair/wx_alert_state.py`): `generation`, content `fingerprint`, `active`, `valid_until`, `published_at`, the published file's `audio` identity, `sources`, `excluded`, `last_error`. |
| `library/services/wx_alert_playout.py` + engine | Idempotent synchronization of `insert_urgent` WxAlert nudges, the final deck-open check, and carrying a pending occurrence across hour adoption. |

## Coordinator pass (under `WEATHER_DATA_DIR/.wx_alert_coordinator.lock`, bounded wait)

1. Read both snapshots. Source states: a readable list (possibly a confirmed-empty one) is known; an AMBER snapshot that
   is absent because `AmberAlertConfig.enabled` is False is known and empty (`disabled`). Everything else is
   UNKNOWN and never means "no alerts": a missing file (never produced yet, or lost), a pre-r0137 NWS snapshot
   (entries without `key`; rewritten by the next NWS poll), or a malformed one. While any source is unknown the pass
   changes nothing: no first-run baseline is recorded (it waits for every source), and an existing announcement is
   neither withdrawn nor shrunk (`last_error` records a missing/unreadable snapshot). The engine refuses to start an
   occurrence it cannot verify against the current sources (below), so nothing obsolete is authorized meanwhile.
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
     (`published_at`, `audio`, `valid_until` = earliest end, `fingerprint`, `sources`), then nudge the engine --
     unless the sources changed while publication ran, in which case no nudge is sent (the generation is already
     refused by the engine) and the pass the changing poller started replaces it.

   The steps are separate, and each failure is retried differently:

   | Step | On success | On failure |
   |---|---|---|
   | Synthesis + concatenation | candidate FLAC | nothing published, record unchanged (`last_error`); next cycle retries |
   | Publication (`publish_weather_asset`) | file + Track committed | previous file restored, record unchanged (`last_error`); next cycle retries |
   | State persistence | new generation recorded | the file is ahead of the record: the engine refuses it (audio mismatch) and the next cycle republishes |
   | Command enqueue (nudge) | queued for the engine | the generation STAYS recorded (publication succeeded); every later unchanged cycle re-sends the nudge |
   | Engine insertion | one LogItem queued | the engine retries briefly; every later nudge re-checks |
   | On-air start | the occurrence's `played_at` | a claimed occurrence whose deck failed before its first real buffer is retried by the next nudge |

## Engine

* **Authorization** (both synchronization and deck open): the generation must be active and unexpired, its recorded
  audio must be the file on disk, AND the current source snapshots must still produce exactly this generation's
  content fingerprint (`isadoraair.wx_alert_state.playback_decision`, local file reads only). A cancellation, update
  or expiry committed by a poller therefore takes effect immediately -- also while publication runs and in the
  interval before the coordinator has replaced the announcement -- without any lock shared between pollers and the
  coordinator (snapshots are replaced atomically and read whole). Unknown sources never authorize.
* **Delivery**: an occurrence belongs to the generation published at or before its CLAIM (`playback_claimed_at`); this
  is exact because a deck is authorized and claimed only under the shared publication lock and only while the record
  names the file on disk, and a newer generation's `published_at` is stamped after its publication finished. A
  generation is delivered only when such an occurrence has its own authoritative air-start evidence (`played_at`,
  the first real buffer) -- an earlier generation's late first buffer never counts. A claimed occurrence still owned
  by a live deck (not completed/finished/retiring) is in progress; one whose deck failed or vanished before its
  first real buffer (deck creation error, a bin that cannot start -- `sync_state_with_parent()` failure is completed
  as an error immediately -- media or decoder error) is retryable.
* **Synchronization** (`insert_urgent` with category `WxAlert`): the payload is ignored; the engine reads the record.
  It queues one occurrence of the exact `wx_alert.flac` Track only if the generation is authorized, no unclaimed
  occurrence is already pending (that one will play the newest audio), the generation has not aired (persisted: an
  engine restart does not repeat it) and no live deck owns one of its occurrences. Other `insert_urgent` categories
  are unchanged.
* **Deck open** (`_deck_open_authority`, WxAlert occurrences only): takes the publication's own lock
  (`.wx_alert.flac.publish.lock`) SHARED and non-blocking and holds it until `filesrc` has opened the file; refuses
  while a publication is in progress, when the record is missing/inactive/expired, when the file on disk is not the
  recorded generation's audio, when the sources changed or cannot be verified, for any Track other than the canonical
  FLAC, for a second occurrence of a generation that already aired or is on a live deck, and for a recreation (resume/seek) of an occurrence that began an earlier generation. It re-reads
  the Track so the deck uses the duration/next-start of the audio it actually opens. A refused occurrence is skipped
  (info event "Weather/AMBER announcement occurrence not opened"); the next nudge queues a current one if still due.
* **Hour adoption**: `_advance_to_next_hour_log` moves a pending, unclaimed occurrence into `_forced_next_items` (same
  LogItem, ahead of any other forced item, as an urgent insertion already is). The next hour's approved log is not
  modified. Exhausted-queue rollover has nothing ahead of the cursor to carry.

Already opened or airing audio is never touched: the deck keeps the file it opened (atomic rename; see
`library/tests/test_wx_alert_gstreamer_single_file.py`).

## Known limitations

* An occurrence carried into `_forced_next_items` from a previous hour's log is not restored after an engine crash or
  restart (no new persistence); the next coordinator nudge re-queues a still-valid generation unless it already aired.
* Generation attribution relies on one host clock (claims and `published_at` are both wall-clock stamps on the
  station); a backwards clock step during an alert could misattribute a claim.
* While a source snapshot is missing or unreadable, a pending announcement is refused (it cannot be verified); it is
  re-queued by the next nudge once the source is readable again. A permanently unparseable qualifying CAP message
  holds the AMBER snapshot at its last complete state (known expirations still apply) until it is fixed or leaves
  the feed.
* An Actual Cancel listed in the IPAWS Atom feed with a DIFFERENT event or state classification than the alert it
  cancels is not fetched (only missing classifications widen the prefilter); the alert still ends at its `expires`.
* While an AMBER alert is held, more than 25 unclassified feed entries in one cycle hold the snapshot at its last
  complete state (logged as a warning) rather than fetch them all or commit a set whose cancellations were not all
  seen. The live feed's typical count of unclassified entries has not been measured from this host.
* Validity follows the upstream feeds with up to one poll interval of lag (5 minutes); within that, the engine applies
  each committed snapshot and every known expiration immediately.
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
