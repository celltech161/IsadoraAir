# IsadoraAir Public Website Song Requests Protocol v1

IsadoraAir can import listener song requests from a station's separate public
website. The public site owns the browser experience and submission records;
IsadoraAir owns library eligibility, scheduling, playback, and authoritative
status. The contract in this guide is framework-independent and can be
implemented in PHP, WordPress, Laravel, Node, Rails, ASP.NET, Django, or any
other server stack capable of HTTPS and JSON.

Protocol v1 is the currently deployed contract. Required fields and endpoint
semantics remain stable within v1. Receivers should ignore unknown additive
JSON fields where practical, and future optional fields may be added without
breaking v1. A breaking schema or semantic change requires an explicitly
documented Protocol v2 rather than a silent change to v1. There is no protocol
version field or header on the wire.

## Architecture

```text
listener browser
  -> public site's ordinary form/search endpoint
  -> public site's catalog and request database
  -> authenticated server-to-server API exposed by the public site
  -> IsadoraAir ingest_web_requests
  -> SongRequest + existing IsadoraAir scheduling/playback services
  -> authenticated status update back to the public site
```

IsadoraAir initiates every machine-to-machine connection as outbound HTTPS.
The public website does not connect inbound to IsadoraAir and never accesses
IsadoraAir's database. The browser never calls the authenticated machine API
and never sees the shared key.

The main IsadoraAir virtualenv runs two repo-managed systemd timers:

- `isadoraair-web-requests-ingest.timer`: idempotent request/status cycle every
  20 seconds.
- `isadoraair-web-requests-catalog.timer`: full catalog/availability sync every
  15 minutes.

An absent or disabled `WebRequestConfig` is a read-only no-op. HTTP timeouts
and systemd `TimeoutStartSec` bound each run; the next timer activation is the
retry. Scheduling remains in the existing IsadoraAir `webrequests` subsystem,
not in this transport layer or the public website.

## Start here

1. In IsadoraAir, open **Config → Web Requests**, enable the feature, and
   configure request-open hours, per-hour capacity, lookahead, and expiry.
2. Under **Config → Station Time**, confirm the station's IANA timezone.
3. Generate a strong shared key on a trusted machine:

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

4. Put the public site's HTTPS base URL and that key in IsadoraAir's protected
   `.env` as `WEB_REQUESTS_INGEST_URL` and
   `WEB_REQUESTS_INGEST_API_KEY`.
5. Put the same key in the public website's **server-side** secret manager or
   environment. Never put it in browser JavaScript, HTML, page source, a query
   parameter, or a public repository.
6. Configure the public website with the same IANA station timezone used by
   IsadoraAir.
7. Implement the three authenticated server-side endpoints documented below.
8. Implement local active-catalog storage, historical request storage, and
   status-version handling.
9. Build the listener-facing catalog search/form using catalog `id` as the
   submitted `track_id`.
10. Verify that missing and incorrect API keys are rejected.
11. Run `sync_web_request_catalog` manually and verify the accepted count.
12. Submit a test request through the normal browser form, run
    `ingest_web_requests` manually, and verify the local `SongRequest`.
13. Verify the public site applies the resulting status acknowledgement and
    pending-feed rules.
14. Only after manual tests pass, enable
    `isadoraair-web-requests-catalog.timer` and
    `isadoraair-web-requests-ingest.timer`.

Detailed commissioning commands are in [Testing your integration](#testing-your-integration).

## IsadoraAir configuration

Enable and configure the feature in **Config → Web Requests**, then add these
values to IsadoraAir's `.env`:

```dotenv
WEB_REQUESTS_INGEST_URL=https://radio.example.org
WEB_REQUESTS_INGEST_API_KEY=replace-with-a-strong-random-server-secret
WEB_REQUESTS_INGEST_CONNECT_TIMEOUT=5
WEB_REQUESTS_INGEST_READ_TIMEOUT=20
WEB_REQUESTS_INGEST_MAX_RESPONSE_BYTES=1048576
```

`WEB_REQUESTS_INGEST_URL` is the public site's HTTPS origin, optionally with a
fixed deployment path prefix. It must not contain user information, a query,
or a fragment. It is privileged, SSRF-capable operator configuration: restrict
who can edit `.env`, use only the intended site, and never derive it from
listener input.

The API key deliberately stays in protected environment/secret configuration,
not source or a plaintext database field. Include it in an encrypted recovery
secret bundle. The public website stores the same value server-side and should
compare it in constant time where practical.

## Public-site conceptual data model

The exact tables and framework are up to the public site. At minimum, preserve
these request concepts:

| Field | Generated/owned by | Required? | Purpose |
|---|---|---:|---|
| `external_request_id` | Public site | Yes | Stable public-site identifier sent to IsadoraAir and used for idempotency. |
| `track_id` | IsadoraAir catalog; selected by listener | Yes | Identifies the exact IsadoraAir track requested. |
| `requester_name` | Listener/public site | Optional | Display/dedication name; empty text is valid. |
| `dedication_message` | Listener/public site | Optional | Listener message; empty text is valid. |
| `submitted_at` | Public site | Yes | Original timezone-aware submission timestamp; never replace it with poll time. |
| `status` | Public site initially; then IsadoraAir updates it | Yes | Starts as `pending`; governs listener display and pending-feed inclusion. |
| `estimated_play_time` | IsadoraAir | Optional/null | Advisory or scheduled air time; clear it when a newer full-state update sends null. |
| `scheduled_at` | IsadoraAir | Optional/null | When IsadoraAir assigned an upcoming log item. |
| `fulfilled_at` | IsadoraAir | Optional/null | When the requested track actually began airing. |
| `status_updated_at` | IsadoraAir | Required on status updates | Ordering/version timestamp; apply only strictly newer updates. |

`external_request_id` is generated and owned by the public site. It must be
stable, unique, non-empty, no longer than 64 characters, and never reused for a
different submission. It may be an integer primary key converted to text, a
UUID, or another stable string. Repeated delivery of the same request must use
the same value.

`track_id` must come from the current IsadoraAir-supplied catalog. Do not use
artist/title typed or altered in the browser as track identity. The browser
selects a stored catalog row and submits that row's IsadoraAir track ID.

Store historical request rows even after their track leaves the active
catalog. Names and dedication messages are untrusted listener text: validate,
escape when displayed, and apply an appropriate privacy/retention policy.

## Authentication and common response rules

The public site implements the three fixed paths below relative to
`WEB_REQUESTS_INGEST_URL`. Every request authenticates with:

```http
X-IsadoraAir-Key: <shared secret>
```

The key is server-to-server only. Never accept or send it in a URL query
parameter. Use valid HTTPS, reject missing/wrong keys with 401 or 403, and do
not log key values.

Responses use `Content-Type: application/json`. A successful response includes
`"success": true`. IsadoraAir rejects non-2xx responses, non-JSON content,
malformed or oversized JSON, and semantically invalid success payloads.

## Endpoint 1: catalog and availability

```http
POST /api/isadoraair/catalog-sync/
Content-Type: application/json
Content-Encoding: gzip
X-IsadoraAir-Key: ...
```

The decompressed body is a **full replacement**, not an incremental delta:

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
  "availability_grid": [true, false, false]
}
```

The three-value grid above is abbreviated for readability; a real payload
always contains exactly 168 booleans.

Catalog track fields:

| Field | Type | Meaning |
|---|---|---|
| `id` | Positive integer | IsadoraAir `Track.id`; return this exact value later as `track_id`. |
| `artist` | String | Display/search artist supplied by IsadoraAir. |
| `title` | String | Display/search title supplied by IsadoraAir. |
| `album` | String or null | Display/search album; null when no album is assigned. |
| `duration_seconds` | Number or null | Track duration in seconds; null when unknown. |

Only tracks that IsadoraAir currently considers `music` and `ready2air` are
sent. Upsert/replace the active searchable catalog transactionally. Mark rows
absent from the new payload inactive, but do **not** delete historical listener
requests merely because their track disappeared. Keeping inactive catalog rows
referenced by historical requests is a straightforward implementation.

Return the number of accepted active tracks:

```json
{"success": true, "count": 1}
```

`count` must equal the number of submitted tracks. IsadoraAir treats a count
mismatch as a failed sync.

### Availability grid and timezone

Grid index is:

```text
weekday * 24 + hour
Monday = 0
```

The booleans represent the station's configured **local time**. Protocol v1
does not transmit the timezone. If the public site uses the grid to decide
whether requests are currently open, configure it with the same IANA timezone
selected in IsadoraAir under **Config → Station Time**, for example
`America/Chicago`, `America/New_York`, or `Europe/London`.

Transmitting the timezone may be a future additive protocol enhancement. For
v1, matching configuration on both systems is required. The grid is useful for
public-site UX, but IsadoraAir remains authoritative about whether and when a
request can be scheduled.

## Endpoint 2: pending requests

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
      "external_request_id": "abc123",
      "track_id": 123,
      "requester_name": "Alex",
      "dedication_message": "For the night shift",
      "submitted_at": "2026-08-22T03:15:20Z"
    }
  ]
}
```

Field rules:

- `external_request_id`: required string or integer; non-empty, maximum 64
  characters after conversion to text, stable, and never reused.
- `track_id`: required positive integer from the synchronized catalog.
- `requester_name`: optional string, maximum 100 characters; null may be used
  for empty.
- `dedication_message`: optional string, maximum 2,000 characters; null may be
  used for empty.
- `submitted_at`: required RFC 3339/ISO 8601 timestamp with an explicit UTC
  offset; preserve the original public-site submission time.
- At most 500 requests per response, within IsadoraAir's configured response
  limit (1 MiB by default).

The pending GET is not a claim and does not acknowledge delivery. It must
return requests whose public-site stored status is exactly `pending` or
`no_slot_soon`. Repeated delivery before a status change is expected.

After the public site successfully stores `scheduled`, `fulfilled`,
`unavailable`, or `expired`, exclude that request from the pending endpoint.
If a later, newer status POST legitimately reverts `scheduled` to `pending` or
`no_slot_soon`, resume returning the request. This differs from IsadoraAir's
internal use of the word “active,” which includes `scheduled` for outbound
status reporting; `scheduled` is not part of the public pending feed.

IsadoraAir validates the complete fetched batch before insertion and imports
it in one database transaction. Its unique `SongRequest.external_request_id`
makes repeated delivery idempotent and prevents a known ID from overwriting
the existing local lifecycle. An unknown/ineligible `track_id` becomes an
immediately `unavailable` request so the listener receives a terminal answer.

## Endpoint 3: status update and acknowledgement

```http
POST /api/isadoraair/requests/status/
Content-Type: application/json
X-IsadoraAir-Key: ...
```

Request:

```json
{
  "updates": [
    {
      "external_request_id": "abc123",
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
present, so a newer transition such as `scheduled` back to `pending` can clear
stale values. Apply an update only when `status_updated_at` is strictly newer
than the stored version. This ordering rule must permit legitimate backwards
status transitions; do not reject a newer `scheduled` → `pending` update merely
because it looks like a state-machine reversal.

All non-null timestamp values are RFC 3339/ISO 8601 strings with an explicit
UTC offset.

Return the number actually applied:

```json
{"success": true, "updated": 1}
```

The count may be lower than the number sent when stale versions were correctly
ignored. IsadoraAir sends no more than 500 updates per POST and splits larger
sets into idempotent chunks. If a later chunk fails, the next timer run replays
the complete set and version ordering safely ignores already-applied chunks.

This POST is Protocol v1's acknowledgement. IsadoraAir sends statuses only
after the pending batch commits and the existing authoritative lifecycle
refresh runs. If import fails, no status POST is sent: leave the public request
waiting and return it again on the next pending GET. There is no separate
claim/ack endpoint. Active and recently resolved terminal states are repeated
for a one-hour safety window, so one lost status POST is harmless.

## Status glossary

| Status | Meaning for the webmaster/listener | Pending endpoint? |
|---|---|---:|
| `pending` | Waiting and eligible. An ETA may be available. | Yes |
| `no_slot_soon` | Still waiting and eligible, but no suitable near-term slot is currently seen. It is **not terminal and not a rejection**. | Yes |
| `scheduled` | Assigned to an upcoming IsadoraAir `LogItem`; the song has not necessarily played yet. | No |
| `fulfilled` | The requested track actually began airing. Do not equate this with merely being scheduled. | No |
| `unavailable` | The track became unavailable or ineligible. | No |
| `expired` | The request aged out before receiving a usable slot. | No |

`scheduled` is not terminal. If its assigned slot disappears before air,
IsadoraAir may send a newer `pending` or `no_slot_soon`. The public site must
accept that reversion when `status_updated_at` is newer, clear nullable fields
according to the full-state payload, and return the request from the pending
endpoint again.

## Complete lifecycle examples

### Happy path

```text
catalog sync
  -> listener selects IsadoraAir Track ID 123
  -> public site creates request abc123 as pending
  -> pending GET returns abc123
  -> IsadoraAir atomically imports it
  -> IsadoraAir sends scheduled
  -> public site stores scheduled and stops returning abc123 as pending
  -> the song actually begins airing
  -> IsadoraAir sends fulfilled
  -> public site stores fulfilled for the listener's status page
```

### Duplicate delivery

The public site may return `abc123` on several polls before a status change.
IsadoraAir's unique `SongRequest.external_request_id` turns repeated delivery
into a no-op. The same ID must never identify a different listener submission.

### Import failure

If IsadoraAir fetches `abc123` but fails before the database import and status
acknowledgement succeed, the public site leaves it `pending` or
`no_slot_soon`, returns it again, and the next timer cycle retries it.

### Scheduled slot loss

If IsadoraAir reported `scheduled` but the assigned slot disappears before the
song airs, IsadoraAir may send a newer `pending` or `no_slot_soon`. The public
site applies the newer version, clears stale scheduled/ETA values supplied as
null, and resumes returning the request from the pending endpoint.

## Testing your integration

Use a staging public site where possible. The commands below assume the
canonical `/opt/isadoraair` install path; substitute the actual checkout path
if your installation differs.

### A. Authentication

Call a machine endpoint with no key and with an intentionally wrong test key:

```bash
curl -i https://radio.example.org/api/isadoraair/requests/pending/
curl -i -H 'X-IsadoraAir-Key: deliberately-wrong-test-key' \
  https://radio.example.org/api/isadoraair/requests/pending/
```

Both requests should return 401 or 403. Do not paste the real key into a shell
command, ticket, screenshot, or log to test the success case; let the configured
IsadoraAir command perform the authenticated request.

### B. Catalog

Run:

```bash
/opt/isadoraair/venv/bin/python /opt/isadoraair/manage.py sync_web_request_catalog
```

Verify HTTP success in the public-site logs, the accepted count matches the
submitted active catalog, the search UI contains expected songs, and the
168-boolean availability grid was stored.

### C. Listener request

Submit a request through the normal browser-facing form. Confirm the public
site stored a unique external ID, catalog `track_id`, original timezone-aware
submission time, and initial `pending` status.

### D. Ingest

Run:

```bash
/opt/isadoraair/venv/bin/python /opt/isadoraair/manage.py ingest_web_requests
```

Verify the request appears as a `SongRequest` in IsadoraAir and that a repeated
manual cycle does not create a duplicate.

### E. Status acknowledgement

Verify the public site applied the latest status and `status_updated_at`,
cleared nullable fields when instructed, and follows the pending-feed rules.
Test a normal scheduling/fulfillment lifecycle when practical.

### F. Timers

Only after A–E pass, enable the timers:

```bash
sudo systemctl enable --now \
  isadoraair-web-requests-catalog.timer \
  isadoraair-web-requests-ingest.timer
```

Verify them without printing configuration or secrets:

```bash
systemctl status isadoraair-web-requests-catalog.timer
systemctl status isadoraair-web-requests-ingest.timer
systemctl list-timers 'isadoraair-web-requests-*'
journalctl -u isadoraair-web-requests-catalog.service --since today
journalctl -u isadoraair-web-requests-ingest.service --since today
```

## Failure, retry, and diagnostics

Connection failures, timeouts, non-JSON responses, response-size violations,
and invalid payloads fail the one-shot command. systemd retries on the next
tick. IsadoraAir does not retry a POST inside the same command, avoiding an
ambiguous write; public endpoints must therefore remain idempotent.

Normal empty polls are silent. IsadoraAir records failures as coalesced System
Events rather than flooding the operator feed or email during a long outage.
Safe exceptions omit headers, keys, response bodies, requester text, and
credential-bearing URLs.

## Troubleshooting

| Symptom | Likely checks |
|---|---|
| 401 or 403 | Shared key is missing/mismatched; confirm both server-side secret configurations without printing either value. |
| TLS/certificate failure | Verify `WEB_REQUESTS_INGEST_URL`, hostname, certificate chain, expiry, and HTTPS reachability from the IsadoraAir host. |
| Catalog does not populate | Check Web Requests is enabled, `.env` URL/key exist, catalog timer/service state, public API logs, JSON response, and accepted-count mismatch. |
| Requests remain public but never appear in IsadoraAir | Check ingest timer/service, pending endpoint authentication/payload, batch limits, `track_id`, and timezone-aware `submitted_at`. |
| Request disappears from pending but status looks wrong | Check public status/version handling, full-state null clearing, and whether an older update incorrectly overwrote a newer one. |
| Malformed JSON | Compare endpoint response shape and content type with this v1 contract; inspect public application logs. |
| Response too large | Keep pending responses at 500 items and within `WEB_REQUESTS_INGEST_MAX_RESPONSE_BYTES`; drain normally through repeated cycles. |
| No activity at all | Confirm `WebRequestConfig.enabled`, request-open configuration, `.env`, and both public-site endpoints and timers. |
| Timer not running | Inspect `systemctl status`, `systemctl list-timers`, and the activated `.service` journal. |

Use systemd status/journal output, IsadoraAir **System Events**, and the public
website's application logs together. Never troubleshoot by printing API keys.

## Security expectations

- IsadoraAir initiates outbound HTTPS; the public site does not initiate an
  inbound connection to IsadoraAir.
- `X-IsadoraAir-Key` is server-to-server only. Never expose it in browser
  JavaScript/page source, query parameters, source control, or logs.
- Compare keys safely/constant-time where practical and rotate them through a
  coordinated configuration change.
- The browser form needs its own CSRF protection where applicable, validation,
  spam/bot mitigation, rate limiting, output escaping, and privacy/retention
  policy.
- Use ORM/parameterized queries for public storage.
- Never trust browser-supplied artist/title text to select IsadoraAir media;
  accept only a current stored catalog `track_id`.
- Enforce sensible request-body, response-body, and item-count limits. Consider
  an upstream body limit for the gzip catalog upload.

## Recovery boundary and reference example

The integrated IsadoraAir side is recovered from IsadoraAir source,
PostgreSQL, configuration/encrypted secrets, and repo-managed systemd units.
The listener-facing public website and its own database/backups remain a
separate station responsibility.

See [`docs/examples/web_requests/`](examples/web_requests/) for a compact
instructional Django implementation. It is not a required framework or a
drop-in application; other stacks should implement the Protocol v1 HTTP and
storage semantics described here.
