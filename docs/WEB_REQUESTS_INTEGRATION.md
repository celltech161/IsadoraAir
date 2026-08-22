# Public Website Song Requests

IsadoraAir can import listener song requests from a station's separate public
website. The public site owns its browser form and submission records;
IsadoraAir owns library eligibility, scheduling, playback, and final status.
The integration is framework-independent and does not require either system to
access the other's database.

## Architecture

```text
listener browser
  -> public site's ordinary form endpoint
  -> public site's request database
  -> authenticated server-to-server API
  -> IsadoraAir ingest_web_requests
  -> SongRequest + existing scheduling/playback services
  -> authenticated status update back to the public site
```

The browser never calls the authenticated IsadoraAir-facing API and never sees
its shared key. The public website must validate and rate-limit its own browser
form.

The canonical execution model is two systemd timers from `deploy/`:

- `isadoraair-web-requests-ingest.timer` runs an idempotent poll every 20
  seconds.
- `isadoraair-web-requests-catalog.timer` sends the catalog every 15 minutes.

Both invoke Django management commands in the main IsadoraAir virtualenv. A
disabled `WebRequestConfig` returns before configuration is read or a remote
call is attempted. HTTP timeouts and systemd `TimeoutStartSec` bound each run;
the next timer activation is the retry. There is no permanent daemon and no
second scheduler.

## Configuration

Enable the feature in **Config > Web Requests**, configure request hours there,
and put these values in IsadoraAir's `.env`:

```dotenv
WEB_REQUESTS_INGEST_URL=https://radio.example.org
WEB_REQUESTS_INGEST_API_KEY=a-long-random-server-to-server-secret
WEB_REQUESTS_INGEST_CONNECT_TIMEOUT=5
WEB_REQUESTS_INGEST_READ_TIMEOUT=20
WEB_REQUESTS_INGEST_MAX_RESPONSE_BYTES=1048576
```

`WEB_REQUESTS_INGEST_URL` is the HTTPS public-site origin, optionally including
a fixed deployment path prefix. It must not contain user information, a query,
or a fragment. The API key is deliberately environment-managed: it does not
belong in source, a browser bundle, a URL, or an unencrypted database field. A
recovery system should place it in the encrypted IsadoraAir secret bundle.

The public site implements the three fixed paths below, relative to that base
URL. All requests authenticate with:

```http
X-IsadoraAir-Key: <shared secret>
```

Use a high-entropy random key, compare it in constant time, rotate it through a
coordinated cutover, and accept it only over valid HTTPS. A response is JSON
with `Content-Type: application/json`. Successful responses always include
`"success": true`. Non-2xx, malformed, oversized, or semantically invalid
responses fail the cycle.

## 1. Catalog and availability

```http
POST /api/isadoraair/catalog-sync/
Content-Type: application/json
Content-Encoding: gzip
X-IsadoraAir-Key: ...
```

The decompressed JSON body is a full replacement:

```json
{
  "tracks": [
    {
      "id": 123,
      "artist": "Example Artist",
      "title": "Example Song",
      "album": "Example Album",
      "duration_seconds": 213.4
    }
  ],
  "availability_grid": [true, false]
}
```

`availability_grid` always has 168 booleans. Index `weekday * 24 + hour`
uses Monday as weekday zero and the station's configured local time. `album`
may be null. The site replaces its searchable catalog transactionally and
returns the number stored:

```json
{"success": true, "count": 1}
```

Only IsadoraAir's own `music`/`ready2air` records are sent. Artist and title are
display metadata; pending requests refer back to the numeric track id, so the
ingest process never trusts browser-supplied artist/title text to choose audio.

## 2. Pending requests

```http
GET /api/isadoraair/requests/pending/
X-IsadoraAir-Key: ...
```

Response:

```json
{
  "success": true,
  "requests": [
    {
      "external_request_id": "98765",
      "track_id": 123,
      "requester_name": "Alex",
      "dedication_message": "For the night shift",
      "submitted_at": "2026-08-22T03:15:20Z"
    }
  ]
}
```

Rules:

- `external_request_id` is the public site's stable unique id, represented as
  a non-empty string or integer and no longer than 64 characters. It must never
  be reused for a different submission.
- `track_id` is a positive integer from the most recent catalog.
- `requester_name` is optional, text, at most 100 characters.
- `dedication_message` is optional, text, at most 2,000 characters.
- `submitted_at` is RFC 3339/ISO 8601 with an explicit UTC offset. It is the
  public site's original submission time, not the poll time.
- A response contains at most 500 requests and its encoded body must fit the
  configured response-byte limit (1 MiB by default).

The GET is not a claim and does not acknowledge delivery. Return every request
whose public status is still active on every poll. Repeated delivery is
expected. IsadoraAir's unique `SongRequest.external_request_id` makes it
idempotent and never overwrites the local lifecycle of a known id.

The entire fetched batch is validated before insertion and inserted in one
database transaction. An unknown/ineligible `track_id` creates an immediately
`unavailable` request so the listener receives a terminal answer. Malformed
input fails the complete cycle rather than partly importing and partly
acknowledging it.

## 3. Status update and acknowledgement

```http
POST /api/isadoraair/requests/status/
Content-Type: application/json
X-IsadoraAir-Key: ...
```

```json
{
  "updates": [
    {
      "external_request_id": "98765",
      "status": "scheduled",
      "estimated_play_time": "2026-08-22T03:42:00+00:00",
      "scheduled_at": "2026-08-22T03:21:00+00:00",
      "fulfilled_at": null,
      "status_updated_at": "2026-08-22T03:21:00+00:00"
    }
  ]
}
```

Every update is a full state replacement. Nullable timestamp keys are always
present so a transition such as `scheduled` back to `pending` can clear stale
values. Status is one of `pending`, `no_slot_soon`, `scheduled`, `fulfilled`,
`unavailable`, or `expired`. The public site should apply an update only when
`status_updated_at` is newer than the stored version, making delayed/replayed
POSTs harmless, and return:

```json
{"success": true, "updated": 1}
```

This POST is the protocol's acknowledgement. IsadoraAir sends it only after the
pending batch commits and the existing `refresh_song_request_statuses` command
has applied authoritative business rules. If insertion fails, no status POST
is sent and the public site must redeliver on the next GET. There is no separate
claim/ack endpoint. Active requests and recently resolved terminal requests are
reported repeatedly for a one-hour safety window, so one lost POST is harmless.

The `updated` count can be lower than the number sent when the public site
correctly ignores stale versions. IsadoraAir sends at most 500 updates per
POST, splitting a larger status set into idempotent chunks. An empty update
list causes no POST. If a later chunk fails, the next timer run safely replays
the complete set and the version check ignores chunks already applied.

## Example lifecycle

1. The listener submits track 123 to the public site's CSRF-protected form.
2. The site creates id 98765 with status `pending` and returns a normal browser
   confirmation. No server-to-server secret reaches the browser.
3. IsadoraAir's next authenticated GET receives id 98765. A transaction creates
   one `SongRequest`; later polls of the same id are no-ops.
4. Existing IsadoraAir scheduling changes the request from `pending` to
   `scheduled`, and actual air-start changes it to `fulfilled`.
5. Status POSTs mirror those states. Once the public site stores a terminal
   state it stops returning that request from the pending endpoint.

## Failure, retry, and diagnostics

Connection failures, timeouts, non-JSON responses, response-size violations,
and invalid payloads produce a failed one-shot command. systemd retries on the
next tick. The client never retries a POST within the same command, avoiding
ambiguous duplicate writes; the endpoints themselves must be idempotent.

Normal empty polls are silent. Failures are recorded as `SystemEvent` rows and
coalesced for six hours, including a repeat count, so an extended outage does
not flood the operator feed or email. Exceptions exposed to logs contain the
operation/path and error class, never headers, keys, response bodies, request
dedications, or credential-bearing URLs.

## Security expectations

- Terminate and verify TLS; IsadoraAir rejects plain HTTP.
- Send the secret only in `X-IsadoraAir-Key`, never a query parameter, request
  body field, browser JavaScript, or application log.
- Protect the browser form with CSRF defenses, input validation, abuse/rate
  limits, and an appropriate privacy/retention policy.
- Use ORM/parameterized queries. Treat names and dedications as untrusted text
  and escape them when displayed.
- Keep endpoint paths fixed. The configured URL is trusted operator
  configuration and is therefore an SSRF-capable setting; restrict who can
  edit `.env`, use an intended hostname, and do not derive it from user input.
- Enforce request/response count and byte limits on both systems. Consider an
  upstream request-body limit for catalog uploads.
- Never use artist/title supplied by a listener to select a local file.

## Oak Grove migration and cutover

The legacy Oak Grove helper used the same three paths and header, a 20-second
request timer, a 15-minute catalog timer, repeated pending delivery, unique-id
deduplication in PostgreSQL, and status POST acknowledgement. It hardcoded the
Oak Grove hostname and `/home/jreed/isadoraair-django`, used a separate venv,
and read `~/.web_requests_ingest.cred`. It also logged every empty poll and
could emit one notification per failed run. The integrated implementation
retains the wire contract while removing those station/host assumptions and
bounding payloads and diagnostics.

The legacy `data/` directory was inspected and is empty. It contains no durable
state, checkpoint, cache, or migration input. Deduplication state is already the
unique `SongRequest.external_request_id` in PostgreSQL; catalog/status payloads
are regenerated from PostgreSQL. Legacy log files are operational history, not
runtime state. No production data migration is required.

Safe deployment sequence (do not overlap pollers):

1. Deploy the repository and main-environment requirements, render but do not
   yet start the two new timers.
2. Copy the existing key value into `WEB_REQUESTS_INGEST_API_KEY` in IsadoraAir's
   protected `.env`, set `WEB_REQUESTS_INGEST_URL`, and leave the existing
   `WebRequestConfig.enabled` value unchanged. Do not put the key on a command
   line or in shell history.
3. Run `manage.py check`, then manually run `sync_web_request_catalog` and
   `ingest_web_requests` once only after stopping the old timers.
4. **Cutover point:** stop and disable both legacy timers/services; verify they
   are inactive; then enable and start the two `isadoraair-web-requests-*`
   timers. The unique id is defense in depth, not a reason to run both.
5. Verify successful unit exits, a catalog count match, a real request/status
   round trip, and no credential text in the journal.
6. After an observation period, remove `/home/jreed/web-requests-ingest`, its
   standalone venv/logs, the old unit files, and
   `~/.web_requests_ingest.cred`. Preserve any logs separately only if station
   policy requires them.

For disaster recovery the complete feature is now represented by the
IsadoraAir source snapshot, `.env`/encrypted secrets, PostgreSQL, and the
repo-managed units. No separate helper source archive, virtualenv, host-only
unit definitions, credential bootstrap file, or sidecar state archive is
required.

See [`docs/examples/web_requests/`](examples/web_requests/) for a compact
Django public-site example. Other frameworks should implement the same HTTP
contract rather than copying Django-specific code.
