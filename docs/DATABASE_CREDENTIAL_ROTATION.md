# Coordinated database credential rotation

Release r0059 adds one explicit operator authority for the PostgreSQL role,
the application `.env`, and the protected updater's `.pgpass` entry:

```console
sudo /opt/isadoraair/venv/bin/python /opt/isadoraair/manage.py rotate_database_credentials --preflight
sudo /opt/isadoraair/venv/bin/python /opt/isadoraair/manage.py rotate_database_credentials --rotate
```

`--rotate` generates a 48-character cryptographically random password and does
not print it. `--rotate --prompt` accepts hidden input twice from a required
interactive terminal. Both surfaces allow only ASCII letters, digits,
underscore, hyphen, and tilde so the value remains byte-identical through
Django dotenv parsing and the formal backup/restore shell readers. There is no
password command-line option, web-admin field, updater request, or generic
privileged hook.

## Authority and exclusion

The command must run as root because it validates the root-owned station
configuration and must preserve `jreed:jreed` ownership while atomically
replacing application-owned credential files. Its authority is fixed to the
paths and database identity in `/etc/isadoraair/station.json`.

The pre-existing `/opt/isadoraair/.env.lock` inode is the only maintenance
lock. Lock ordering is: take this lock first and take no second maintenance
lock. Credential rotation and ordinary `.env` writes use an exclusive lease;
formal backup and supported Update Center admission use a shared lease.
Rotation also reads every protected updater job record under the root-owned
jobs root and fails closed if a job is accepted/running or malformed. This
keeps checkpoint creation and release installation outside the rotation
window without granting the updater access to `.env` or any new privilege.

The five persistent Django/database consumers must be stopped before mutation:

* `isadoraair-gunicorn.service`
* `isadoraair-engine.service`
* `isadoraair-encoders.service`
* `isadoraair-monitoring.service`
* `isadoraair-rbds.service`

The command checks that all five are loaded and inactive. One-shot commands
read `.env` afresh. The protected updater reads `.pgpass` for each `pg_dump`
and needs no restart.

## Transaction

Preflight requires exact agreement among station identity, `.env`, `.pgpass`,
running Django settings, a direct PostgreSQL login, a `psql -w` login, and a
real custom-format `pg_dump`. Duplicate/wildcard-overlapping pgpass entries,
wrong ownership/mode, active updates, or inconsistent credentials abort before
mutation.

Before its first mutation, the transaction atomically persists a root-only
mode-0600 recovery record in the protected jobs directory and fsyncs that
directory. It contains the exact old file bytes/metadata and both in-memory
credentials needed to recover after SIGKILL, process death, or power loss.
A separate nonsecret `.env.rotation-pending` marker is written first so formal
backup, ordinary environment administration, and supported Update Center
admission remain fail-closed after a process dies and releases the flock.
Normal completion and fully validated rollback remove and strictly fsync the
record and then the marker; an incomplete rollback retains the public gate and
private recovery authority for the next invocation/operator repair. Every
credential-file replacement also requires a successful directory fsync before
the transaction advances.

The transaction also keeps the original authenticated PostgreSQL connection
open:

```text
healthy old state
  -> persist durable root-only recovery record (old/new credentials, old file
     bytes/metadata)
  -> publish the nonsecret .env.rotation-pending admission marker (blocks
     backup, ordinary .env writes, and Update Center admission)
  -> open the sensitive mutation session: inspect and verify-suppress every
     PostgreSQL statement/error-logging parameter that could otherwise render
     the ALTER ROLE statement into server logs, plus pgAudit if loaded --
     fail closed, before ALTER ROLE, if any of it cannot be proven safe
  -> ALTER ROLE to new password via that verified session (client-generated
     SCRAM verifier only -- old authenticated session remains open)
  -> atomically replace .env
  -> atomically replace matching .pgpass entry
  -> verify exact file bytes/owner/mode on disk
  -> child Django login from final on-disk .env
  -> psql -w from final .pgpass
  -> real pg_dump from final .pgpass
  -> validated-new state: remove the durable recovery record (rollback
     authority is now intentionally gone -- the new credential is
     authoritative)
  -> remove the nonsecret pending marker
  -> commit
```

Any failure after `ALTER ROLE` uses the still-authenticated connection to
restore the old role password (through the same verified logging-suppression
session), atomically restores the exact old bytes and metadata of both files,
then repeats PostgreSQL, Django, psql, and pg_dump validation. If that
connection is lost, the command attempts rollback through the new credential.
Catchable termination signals enter the same rollback path and repeated
termination is deferred until rollback finishes. On the next invocation, a
durable pending record is resolved to the old fully validated state before any
new rotation can begin. An incomplete rollback is reported as a critical
redacted condition and requires operator intervention.

Once the new credential state has been fully validated and the durable
recovery record has been successfully removed, a later failure while cleaning
up only the nonsecret pending marker does **not** trigger rollback: rollback
authority has been intentionally and durably discarded at that point, and the
validated new credential is already authoritative everywhere (PostgreSQL,
`.env`, `.pgpass`). That case is reported as "durable cleanup is incomplete"
rather than as a rollback failure -- a subsequent `--preflight` clears the
stale marker once it observes all three authorities already healthy and in
agreement.

Temporary atomic-write files and the temporary validation dump are mode 0600
from creation, live on the application filesystem, and are removed on every
exit path.

### PostgreSQL statement/error-logging and pgAudit suppression boundary

libpq derives a salted SCRAM-SHA-256 verifier client-side before the
role-change statement is issued, so the *cleartext password* is never part of
that statement or sent over the wire. That alone is not sufficient: the
verifier itself is still a literal inside the `ALTER ROLE` statement text, and
PostgreSQL's own statement/error logging -- and pgAudit, where installed --
are both driven by that statement text, not by whether the value inside it
happens to be a hash rather than a password. A `log_statement = none` server
default is not sufficient either: a *failing* `ALTER ROLE` can still be
captured through `log_min_error_statement`, and a randomly sampled transaction
can still be captured through `log_transaction_sample_rate` regardless of
`log_statement`.

Before every `ALTER ROLE` -- forward rotation and rollback alike, on the same
already-authenticated connection -- the rotator sets and verifies, at session
scope, on that connection only:

* `log_statement = none`
* `log_min_error_statement = panic`
* `log_min_duration_statement = -1`
* `log_min_duration_sample = -1`
* `log_transaction_sample_rate = 0`
* `pgaudit.log = none`, but only if this server actually has pgAudit loaded
  (a plain `pg_settings` existence check) -- there is nothing to suppress,
  and nothing to fail closed over, on a server without it.

Each parameter is read back with `current_setting()` immediately after being
set; a parameter that cannot be set (most commonly: the connecting role has
not been granted `GRANT SET ON PARAMETER log_statement, log_min_error_statement,
log_min_duration_statement, log_min_duration_sample, log_transaction_sample_rate
TO <role>;` -- a one-time DBA setup step, since these are otherwise
superuser-only) or does not read back as expected aborts rotation *before*
`ALTER ROLE` is ever issued, with no PostgreSQL or credential-file authority
touched, and a static, nonsecret, actionable reason naming the parameter. The
rotator also applies a narrow, best-effort (never fail-closed -- it is a
robustness improvement, not a secrecy control) `statement_timeout`/
`lock_timeout` bound to the same session, since connection establishment being
timed out does not bound the mutation query itself. Every parameter this
boundary touches is unconditionally reset immediately after the `ALTER ROLE`
attempt, success or failure -- the long-lived connection used for the rest of
the transaction never carries suppressed logging beyond that one statement.

Child commands receive only a pgpass path in their controlled environment;
neither cleartext password nor verifier is present in argv, progress output,
exceptions, logs, or events, and neither is emitted by application logging or
SystemEvents.

## Production runbook

After r0059 is installed and reviewed:

1. Run `--preflight` while services are healthy.
2. Stop the five persistent consumers in the order shown above.
3. Run `--rotate` from an interactive root terminal.
4. Start the five services and verify their state and application health.
5. Run `--preflight` again from a new process.
6. Audit journal and application events for the static rotation messages and
   confirm no credential-shaped value was emitted.

If rotation reports a fully validated rollback, start the services again; the
old credential remains authoritative. If it reports incomplete rollback, keep
the services stopped and repair only the named authority before proceeding.
