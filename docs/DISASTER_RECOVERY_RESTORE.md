# Disaster recovery: restore procedure

Roadmap item 1.2, Phase 4 (2026-08-12). This is the human-readable
procedure an administrator follows during a real recovery — usable when
the original machine is dead, given: the private GitHub repositories,
one known-good `isadoraair-backup-*.tar.gz` archive, the externally
stored credentials/licenses this document inventories below, and
replacement Ubuntu 26.04 hardware. It does **not** assume access to the
dead host for anything.

See `docs/DISASTER_RECOVERY.md` for *what's backed up and why* (the
Phase 1/2 audit); this document is *how to put it back together*, built
on `deploy/restore/`'s tooling (Phase 4). `deploy/restore/README.md`
covers that tooling's own architecture/safety-mode design in detail —
this document is the operator-facing walkthrough, not a restatement of
it.

**Phase 4 built and validated this tooling in staging. Phase 5 is the
actual bare-machine proof.** Nothing here has been run against a real
clean machine yet — every stage was exercised via `--staging-root
--apply` against isolated paths, described in the Phase 4 completion
report.

## Overview

```
clean Ubuntu 26.04
  |
  v  deploy/restore/00-preflight.sh    -- OS check, validate the backup archive
  v  deploy/restore/10-packages.sh     -- apt packages
  v  deploy/restore/20-application.sh  -- git clone + SHA checkout, .env + media/
  v  deploy/restore/30-postgresql.sh   -- role/DB bootstrap, pg_restore
  v  deploy/restore/40-station-content.sh -- carts/voicetracks/reports/StereoTool profile
  v  deploy/restore/50-native-deps.sh  -- fdk-aac/fdkaac build + HE-AAC validation
  v  deploy/restore/60-python.sh       -- IsadoraAir venv
  v  deploy/restore/70-tts.sh          -- Kokoro + Piper
  v  deploy/restore/80-companions.sh   -- syndicated-ingest/weather-ingest/ogremote-ingest
  v  deploy/restore/90-system-config.sh -- nginx + systemd units installed, NOT started
  v  deploy/restore/95-validate.sh     -- read-only final check (manage.py check,
  |                                       check_deploy_baseline, migration state)
  v
[everything below this line is MANUAL -- see "Manual checkpoints"]
  v  attach/mount persistent music storage
  v  obtain + install StereoTool binary + license
  v  controlled service bring-up (see "Service bring-up order" below)
  v  confirm sound hardware
  v  confirm transmitter/output path
  v  issue or restore certificates
```

For the native stage, a future complete DR archive supplies its extracted
`native/fdkaac/` directory directly:

```bash
deploy/restore/50-native-deps.sh --apply --staging-root /path/to/stage \
  --source-dir /path/to/extracted-backup/native/fdkaac
```

For the all-stage orchestrator, export the same handoff without passing a flag
that unrelated stages would reject:

```bash
FDKAAC_SOURCE_DIR=/path/to/extracted-backup/native/fdkaac \
  deploy/restore/restore.sh --archive /path/to/backup.tar.gz --apply ...
```

That local mode verifies the exact manifest hashes and cannot use the network.
The current production backup has not yet been extended to carry those source
archives, so this invocation documents the handoff contract rather than
claiming end-to-end DR completion.

Run each stage with `--plan` first (always safe, never writes anything),
then `--apply` once the plan looks right. `deploy/restore/restore.sh`
chains all eleven stages if you'd rather run them in one shot; running
them individually and reviewing each is recommended for the actual
Phase 5 drill.

## Manual checkpoints

These are deliberately **not** automated — the tooling makes them
obvious rather than pretending the whole restore is unattended:

| Checkpoint | Why it's manual |
|---|---|
| GitHub private-repo access | `deploy/restore/20-application.sh` and `80-companions.sh` need working, non-interactive `git`/SSH access to `celltech161/IsadoraAir` + the three companion repos. Provision an SSH key with read access before starting — see "GitHub access" below. |
| Attach/identify persistent music storage | The 717+ GB library is never part of any backup (see `docs/DISASTER_RECOVERY.md`'s "Music library" section) — mount the real disk (or its current replacement/replica) at `LIBRARY_ROOT` separately. `40-station-content.sh` only creates the empty mountpoint. |
| Recover the three companion credential files | See "Secrets and credentials inventory" and "Encrypted recovery-credential preservation" below — as of 2026-08-18 these have a verified operator-maintained off-host recovery copy; still a manual install step (decrypt-or-copy-in, `chmod 0600`), not automated. |
| Obtain StereoTool binary | Proprietary, paid software — never in Git or backups. See "StereoTool" below. |
| Certificate issuance | See "Certificates" below — this restore does not assume `acme.sh` state is recoverable. |
| Confirm sound hardware | A staging VM or spare host may not have the Topping D10s / studio card / Mixcast / transmitter path — see "Software-complete vs. audio-hardware-ready" below. |
| Confirm transmitter/output path | Physical RF chain, entirely outside this repo's scope. |

## GitHub access

`git@github.com:celltech161/IsadoraAir.git`,
`.../syndicated-ingest.git`, `.../weather-ingest.git`,
`.../ogremote-ingest.git` are all **private**. A restore needs an SSH
key with read access to all four, usable non-interactively (no
passphrase prompt) — e.g.:

```bash
GIT_SSH_COMMAND="ssh -i /path/to/key -o IdentitiesOnly=yes" deploy/restore/restore.sh --archive ... --apply
```

**The `GIT_SSH_COMMAND` override above is not optional boilerplate —
verified live (2026-08-17, Phase 4.5) it is actually required.** This
box's `~/.ssh/config` sets a *default* identity for `github.com`
(`Host github.com` → `IdentityFile ~/.ssh/github_isadora_rw`), and that
default key is a **per-repository GitHub deploy key**, scoped to
`celltech161/IsadoraAir` only — confirmed by its own SSH auth banner
(`Hi celltech161/IsadoraAir!`, GitHub's deploy-key response format,
distinct from an account-level `Hi <username>!`) and by a real,
read-only `git ls-remote` against each of the three companion repos
using that identity: `syndicated-ingest` returns "Repository not
found" (the clean auth-scoped-out response); `weather-ingest` and
`ogremote-ingest` both stall. **A bare `deploy/restore/80-companions.sh
--apply` run with no `GIT_SSH_COMMAND` override — i.e. relying on
whatever `git`/SSH the operator has "already set up," per that script's
own header comment — will fail on all three companion repos on a
freshly provisioned restore host**, unless that host's `~/.ssh/config`
happens to default to a broader-scoped key.

**What does cover all four, verified live the same pass**: this
account's personal key (`~/.ssh/id_ed25519` on the current production
host, comment `celltech161@gmail.com`) authenticates at the *account*
level (`Hi celltech161!`, not a per-repo deploy-key banner) and a real
`git ls-remote HEAD` against all four repos with that identity
succeeded. Any key with equivalent account-level (or four-repo
multi-deploy-key) access works the same way — the identity used in
practice on this box happens to be the account key, not a requirement
of the tooling itself.

No credential or key is embedded anywhere in this repo or the backup
archive — provisioning the key itself (from wherever it's held outside
this station, e.g. a password manager or a second physical copy) is a
manual step this tooling assumes is already done before it runs. See
"Secrets and credentials inventory" below for this item's A/B/C
recovery classification.

## Secrets and credentials inventory

Consolidated from `docs/DISASTER_RECOVERY.md`'s "Secret reprovisioning
boundary" plus the companion projects' own credential patterns. No
values are recorded here or anywhere in this repo — names, destinations,
and (where known) an external recovery source only.

**Classification** (established 2026-08-17, extended 2026-08-18, against
the *actual* current host — file existence/owner/mode/expected key names
verified directly, values never displayed; vendor/provider workflows
verified only where a concrete mechanism was actually confirmed, never
assumed):

- **A — externally recoverable now**: a known external source
  independent of this host already exists (password manager, vendor
  account portal, operator-maintained off-host copy, etc.).
- **B — safely reprovisionable**: the original value doesn't need to
  survive — a fresh replacement can be generated through a verified
  provider/vendor workflow.
- **Not a Phase 5 blocker**: absence during recovery is an accepted,
  confirmed-tolerable degradation, not something requiring a recovery
  source at all — distinct from A/B, which both still need *something*
  external to exist. See StereoTool license below.
- **C — unresolved DR blocker**: exists only on this host, no verified
  external copy, no proven reprovisioning procedure. As of 2026-08-18,
  no row remains in this state — see "Phase 5 readiness" below.

| Secret | Purpose | Destination | Owner/mode (verified) | Class | External source / reprovision method | Needed at |
|---|---|---|---|---|---|---|
| `.env` (`SECRET_KEY`, `DB_PASSWORD`, email creds, etc.) | Django/DB/email auth | `$ISA_ROOT/.env` | `$ISA_USER`, `0600` | **A** | Inside the backup's `app.tar.gz`, restored by `20-application.sh`. `SECRET_KEY` can be freshly regenerated if ever unavailable (invalidates sessions, otherwise harmless); `DB_PASSWORD` must match whatever the restored/recreated PostgreSQL role actually has. | Automated restore (stage 20) |
| GitHub SSH access (read, all 4 private repos) | Cloning code during restore (`20-application.sh`, `80-companions.sh`) | Operator's own `~/.ssh/`, outside this repo entirely | Verified live: `id_ed25519` (celltech161 account key), `0600`, reaches all 4 repos. Default `~/.ssh/config` identity (`github_isadora_rw`, an `IsadoraAir`-only deploy key) reaches only 1 of 4. | **B** | Log into the `celltech161` GitHub account via the normal web UI (independent of any file on this host — needs only the account's own login/2FA, not this specific key) and generate a fresh SSH key or per-repo deploy key with read access to all 4 repos. See "GitHub access" above for the verified evidence. | Automated restore (stage 20 — the very first write) |
| `~/.iasboxbu.cred` (`BAK_HOST`, `BAK_USER`, `BAK_PORT`, `BAK_PATH`, `BAK_PASS`) | `backup_isadoraair.sh`'s own SFTP upload/retrieval credentials | `$ISA_HOME/.iasboxbu.cred`, verified `0600` | **A** (resolved 2026-08-18) | Operator-maintained off-host recovery copy on local PC. **This is still the sole bootstrap source** — see "Encrypted recovery-credential preservation" below for why the nightly backup's own encrypted copy of this file does NOT substitute for it (an encrypted copy inside the backup cannot retrieve the backup that contains it). | **Pre-restore** (retrieving the archive at all) and service bring-up (resuming nightly backups) |
| `~/.syndicated_ingest.cred` | Syndicated-show fetch credentials (RadioPush, per-show site logins), SFTP art upload, SMTP, Bluesky app password | syndicated-ingest's own cred file, verified `0600` | **A** (resolved 2026-08-18) | Operator-maintained off-host recovery copy on local PC. Also preserved encrypted in the nightly backup once configured — see below. | Full production readiness (syndicated-`*` timers only — IsadoraAir itself doesn't need it) |
| `~/.ogremote_ingest.cred` (`API_KEY`) | Remote-content polling auth | ogremote-ingest's own cred file, verified `0600` | **A** (resolved 2026-08-18) | Operator-maintained off-host recovery copy on local PC. Also preserved encrypted in the nightly backup once configured — see below. | Full production readiness (ogremote-`*` timers only) |
| weather-ingest config | GW3000/weather API access | **Not a file** — lives in IsadoraAir's own database (`WeatherConfig`/`AmberAlertConfig` admin singletons — verified present in `weather/models.py`; `dump_weather_config`/`dump_amber_alert_config` commands verified to exist and are the exact cross-venv calls `weather-ingest/lib/wxconfig.py` makes), reachable only once `20-application.sh`/`30-postgresql.sh`/`60-python.sh` have restored code+DB+venv | N/A | **A** | Restored as part of the database dump itself — no separate secret-recovery action needed. Re-enter manually only if the DB restore is unavailable and a fresh empty DB was created instead. | Automated restore (rides along with stage 30) |
| acme.sh / DNS-01 provider credentials (IONOS: `IONOS_PREFIX`, `IONOS_SECRET`) | Let's Encrypt cert issuance for `radio.oakgroveradio.com` | `~/.acme.sh/account.conf`, verified `0600`; DNS plugin `~/.acme.sh/dnsapi/dns_ionos.sh` present | — | **B** | These are IONOS Developer Portal API credentials (DNS scope), not a value that has to survive — a fresh `IONOS_PREFIX`/`IONOS_SECRET` pair can be generated from the IONOS account portal and re-entered for a fresh `acme.sh --issue --dns dns_ionos ...` run, provided the operator retains their own IONOS account login (a separate, much higher-stakes credential than this one — IONOS is also the domain registrar). Falls back to the self-signed cert (already restored as part of the nginx config by `90-system-config.sh`) if unavailable either way — degraded public HTTPS, never a hard blocker. | Full production readiness only (not automated restore, not service bring-up — self-signed cert covers both) |
| StereoTool license | Unlocks the processor beyond trial/demo limits | Bundled with the StereoTool install itself — no separate license file found anywhere on this host by name search | — | **Not a Phase 5 blocker** (confirmed operational behavior, 2026-08-18) | StereoTool runs and processes audio with no license entered at all — the unlicensed penalty for the feature set in use is an occasional audio watermark every few hours, an accepted tradeoff during disaster recovery. Entering the real license key once the system is operational again is a roughly one-minute manual step. **This does not mean the license itself is backed up or vendor-recoverable** — that remains genuinely unverified (this audit did not confirm StereoTool's actual reactivation mechanism); it just means its absence no longer gates Phase 5 or even controlled audio-chain validation. | Full production readiness only — genuinely not needed for software restore or for exercising the audio chain during recovery |

As of 2026-08-18, **no row above is classified C.** The three
credential-file rows moved from C to A on the operator's own decision
(an off-host copy on their local PC now exists, independent of this
host) — not because this audit discovered a new mechanism. StereoTool's
license moved out of the blocker discussion entirely, on confirmed
operational behavior (works unlicensed, degrades gracefully), not
because reprovisioning was solved. B rows (GitHub, acme.sh) were always
non-blocking in this sense — the workflow to replace them is verified
and doesn't depend on anything surviving from the dead host — but are
still worth confirming once, in a controlled setting, rather than
trusting the workflow untested during a real incident.

## Encrypted recovery-credential preservation

2026-08-18. The three credential files above now also get an
**age-encrypted** copy inside the nightly backup archive itself
(`recovery-credentials/*.age`), automatically, if configured —
`deploy/backup_isadoraair.sh` calls `deploy/encrypt_recovery_credentials.sh`
for this; see that script's own header comment for the complete
mechanism. This is **in addition to**, not a replacement for, the
operator's own off-host PC copy above.

### Why both — the security model in one picture

```
IsadoraAir host
    |
    |-- ~/.iasboxbu.cred
    |-- ~/.syndicated_ingest.cred
    |-- ~/.ogremote_ingest.cred
    |
    |-- recovery encryption PUBLIC key / recipient
    |   (BACKUP_RECOVERY_AGE_RECIPIENT / _FILE -- non-secret, but still
    |   configured per-install, never hardcoded)
    |
    v
nightly backup
    recovery-credentials/
        iasboxbu.cred.age
        syndicated_ingest.cred.age
        ogremote_ingest.cred.age
```

The matching **private** age identity/decryption key is never generated
or stored on this host — it exists only where the operator keeps it
externally (their local recovery PC, and preferably a second independent
secure location). This means:

- Automatic nightly preservation if a credential file ever changes —
  no separate manual "remember to update the off-host copy" step for
  day-to-day drift, though the operator's own off-host copy is still
  the one to keep current for the bootstrap case below.
- No plaintext secret ever enters the backup archive.
- Compromising this host, or any backup archive it produces, does not
  grant decryption capability — the archive alone is not enough.

### The bootstrap rule — this does NOT solve archive retrieval

**`~/.iasboxbu.cred` is needed to reach the remote backup destination in
the first place.** An encrypted copy of it living *inside* the backup
archive cannot be used to retrieve that same archive — that would be
circular, and this design deliberately is not:

```
Need .iasboxbu.cred
        |
        v
retrieve backup
        |
        v
backup contains an encrypted CURRENT copy of .iasboxbu.cred
(for restoring it going forward, and for confirming the off-host
 bootstrap copy hasn't drifted -- NOT for the retrieval step above)
```

The operator's off-host PC copy (see the table above) is what a recovery
actually starts from. The encrypted in-archive copy exists for two
different purposes: keeping a durable, automatically-refreshed
authoritative record as these files change over time, and restoring them
onto the rebuilt host once the archive is already in hand (see "Manual
recovery: decrypting the credential files" below). Treating it as a
bootstrap source instead would silently reintroduce exactly the
circularity `docs/DISASTER_RECOVERY.md`'s original audit called out.

### Configuration

Two config inputs, both non-secret (the recipient is a PUBLIC key) but
still per-installation, never hardcoded — set as `Environment=` lines on
`isadoraair-backup.service` (see that unit's own template, which ships
both lines pre-written and commented out):

| Variable | Purpose |
|---|---|
| `BACKUP_RECOVERY_AGE_RECIPIENT_FILE` | Path to a file containing just the age recipient (`age1...`) — preferred, keeps the long string out of the unit file itself. |
| `BACKUP_RECOVERY_AGE_RECIPIENT` | The recipient string directly, if a separate file isn't wanted. |

Neither set (the default on every fresh/generic install) = feature
**disabled** — backups proceed exactly as before this feature existed,
with `~/.iasboxbu.cred`/`~/.syndicated_ingest.cred`/`~/.ogremote_ingest.cred`
simply not included in any form, same as always. Once either is set, the
backup **fails closed** before upload on: the `age` binary missing, the
recipient failing a basic structural check, an `age` invocation failing,
or a resulting ciphertext file being empty — see
`deploy/encrypt_recovery_credentials.sh`'s header for the exact
conditions. A source credential file simply not existing on this host
(e.g. ogremote-ingest not in use) is never a failure — it's recorded as
"absent" in the manifest and the backup proceeds.

### Manual recovery: decrypting the credential files

Not automated, and deliberately so — no script this repo ships ever
handles the private key. Run these on the machine that actually holds
the private key (the operator's off-host recovery PC), never on the
IsadoraAir host itself:

```bash
# 1. Extract just the encrypted credential files from the backup archive
#    (already retrieved via the off-host bootstrap copy -- see above).
tar -xzf isadoraair-backup-YYYYMMDD-HHMMSS.tar.gz ./recovery-credentials

# 2. Decrypt each one with the externally-held private key -- run on the
#    recovery PC, output written there too, nothing touches the
#    IsadoraAir host at this point:
age --decrypt -i /path/to/recovery-private-key.txt \
  -o iasboxbu.cred recovery-credentials/iasboxbu.cred.age
age --decrypt -i /path/to/recovery-private-key.txt \
  -o syndicated_ingest.cred recovery-credentials/syndicated_ingest.cred.age
age --decrypt -i /path/to/recovery-private-key.txt \
  -o ogremote_ingest.cred recovery-credentials/ogremote_ingest.cred.age

# 3. Transfer the three decrypted files to the restored IsadoraAir host
#    by whatever secure channel the operator normally uses (scp over
#    the restored host's own SSH, a USB drive, etc.) -- this step is
#    intentionally not prescribed further; it's a one-time transfer of
#    three small files, not something this tooling needs to automate.

# 4. On the restored host, install at the documented paths with correct
#    ownership/mode:
mv iasboxbu.cred ~/.iasboxbu.cred
mv syndicated_ingest.cred ~/.syndicated_ingest.cred
mv ogremote_ingest.cred ~/.ogremote_ingest.cred
chmod 0600 ~/.iasboxbu.cred ~/.syndicated_ingest.cred ~/.ogremote_ingest.cred

# 5. Verify expected key names are present WITHOUT displaying values --
#    e.g. for .iasboxbu.cred:
grep -oE '^[A-Za-z_]+=' ~/.iasboxbu.cred
# Expect: BAK_HOST=, BAK_USER=, BAK_PORT=, BAK_PATH=, BAK_PASS= (some
# lines may use "KEY = value" with spaces -- eyeball for the five names,
# don't just trust the regex found all of them).

# 6. Only once installed and verified, enable/re-enable the associated
#    jobs: isadoraair-backup.timer, syndicated-*.timer, ogremote-*.timer.
```

This procedure is deliberately plain `age --decrypt` invocations, not a
wrapper script — the task that produced this feature considered a
decrypt helper and rejected it as unnecessary: three documented commands
are simpler and safer than tooling that would need its own guarantees
about never persisting the private key, accepting it only via explicit
operator-supplied path, and writing atomically with correct modes. If
that calculus changes later (repeated real-world friction, not just
theoretical tidiness), building one remains an option — it would need to
satisfy exactly those constraints and nothing less.

### Production activation steps (not performed by this pass)

Repo changes establish the mechanism; the following remain deliberately
undone until separately approved, and none of them happen on the
IsadoraAir server itself except the last two:

1. Generate the age recovery keypair on an off-host machine (the
   operator's recovery PC, or another trusted machine that is not this
   server) — **never on the IsadoraAir host**.
2. Store the private key externally, in at least two independent
   locations (per the operator's own stated preference).
3. Configure only the **public** recipient on the IsadoraAir host —
   `BACKUP_RECOVERY_AGE_RECIPIENT_FILE` (or `_RECIPIENT`) on
   `isadoraair-backup.service`.
4. Run `DRY_RUN=1` `deploy/backup_isadoraair.sh` and confirm the
   "Encrypting recovery credentials..." step reports `enabled` with the
   expected per-file inclusion status.
5. Inspect the resulting local archive with
   `deploy/restore/inspect_backup.sh` and confirm the new "Recovery
   credential" checks PASS.
6. Perform a real test decrypt on the off-host recovery machine (never
   the IsadoraAir host) against that same archive, confirming the
   round-trip actually works with the real keypair before trusting it.
7. Only then let the normal nightly `isadoraair-backup.timer` run use
   the new mechanism live.

## Phase 5 readiness

**PHASE 5 READY: YES** (as of 2026-08-18, external recovery-input
readiness specifically — see the distinction below).

Every row in "Secrets and credentials inventory" above is now either
**A** (a verified external source already exists), **B** (a verified
safe reprovisioning workflow exists), or confirmed **not a Phase 5
blocker at all** (StereoTool license). No row remains unresolved-C. The
three companion credential files reached A because the operator now
maintains an off-host recovery copy independent of this host — this
audit didn't discover a new mechanism, it accepted an operator decision
as the established external source, which is exactly what Class A means.

**Two readiness questions worth keeping distinct**, so a real Phase 5
drill isn't mis-scored by conflating them:

- **Recovery-input readiness: READY.** Every external dependency a
  bare-machine restore needs (GitHub access, the backup archive itself,
  the three credential files, `.env`/DB via the archive, StereoTool
  running unlicensed) has a known, verified path to obtain it.
- **Encrypted nightly credential preservation: implementation complete,
  production activation pending.** The mechanism described above
  (`deploy/encrypt_recovery_credentials.sh`, the archive's
  `recovery-credentials/*.age`, `inspect_backup.sh`'s new checks) is
  built and tested, but `BACKUP_RECOVERY_AGE_RECIPIENT(_FILE)` is not
  yet configured on production — see "Production activation steps"
  above. **This does not block Phase 5** — the operator's off-host PC
  copy is already sufficient on its own; the encrypted-backup mechanism
  is additional defense-in-depth for keeping that copy's authoritative
  content current automatically, not a prerequisite for the bare-machine
  drill.

Nothing in this pass discovered a new blocker requiring Phase 5 to wait
further — this conclusion should not be read as "everything is
theoretically perfect," only as "every concrete gap this and prior
Phase 4.5 audits found now has a real answer."

## Backup credential recovery

`~/.iasboxbu.cred` (`BAK_HOST`/`BAK_USER`/`BAK_PORT`/`BAK_PATH`/
`BAK_PASS`) is two related but genuinely distinct needs, not one:

1. **Retrieving an existing backup archive after total host loss.**
   Before Phase 5's automated stages can even start (`00-preflight.sh`
   needs `--archive PATH`), an operator has to get a
   `isadoraair-backup-*.tar.gz` off the remote SFTP target — which
   needs these same values.
2. **Resuming future nightly backups once the restore is complete.**
   `isadoraair-backup.timer` will run
   `deploy/backup_isadoraair.sh` on its normal schedule once services
   are back up (see "Service bring-up order" below) — that script reads
   `$HOME/.iasboxbu.cred` itself and will simply fail (loudly, exit 1 —
   see the script's own `[ ! -f "$CONFIG_FILE" ]` check) until this file
   exists again on the restored host.

**Both need the exact same values**, so resolving the external source
once covers both. Resolved (2026-08-18): an operator-maintained off-host
recovery copy on the operator's local PC, independent of this host —
see the classification table above. **This off-host copy is what both
needs above actually rely on** — the plaintext file is still, by
design, never in the backup archive at all (confirmed:
`MANIFEST.txt`'s own "Secrets NEVER included in ANY form" list names it
explicitly), and even the *encrypted* copy the archive now optionally
carries (see "Encrypted recovery-credential preservation" above) is
explicitly NOT a substitute for this off-host copy — see that section's
own "bootstrap rule" for exactly why, worth reading in full rather than
assuming the encrypted copy is enough.

## Persistent storage mount

**Generic requirement**: some persistent, writable path at
`LIBRARY_ROOT` (and its `/srv/isadoraair` siblings — see
`docs/DISASTER_RECOVERY.md`'s subtree table) must exist with correct
ownership before the services that need it start. `40-station-content.sh`
creates the directory structure (including an empty `music/` mountpoint)
but never the mount itself.

**Oak Grove production reference** (an example, not a generic default —
see `docs/DISASTER_RECOVERY.md`'s own "reference evidence, not a
requirement" framing): a dedicated 1.8 TB ext4 partition
(`/dev/nvme0n1p1`), `/etc/fstab` entry:
```
UUID=ca4da361-7210-4ed6-8e74-5ddb9c92b5c4  /srv/isadoraair  ext4  defaults,noatime,nofail  0  2
```
`nofail` matters generically, not just as Oak Grove trivia: boot must
not hang if the disk is ever missing. A fresh restore target's real
UUID will differ — `blkid` the actual replacement/replica disk and
write its own `/etc/fstab` line, then `sudo mount -a` and verify with
`mount | grep isadoraair` and `df -h /srv/isadoraair`.

**Staging without the full music disk**: `95-validate.sh` and
`check_deploy_baseline` both tolerate an empty `music/` — they report it
as a warning (see `docs/DISASTER_RECOVERY.md`'s "Music library" section
on why this is a deliberately separate readiness gate), not a failure.
A restore can proceed through every automated stage, and even through
early service bring-up (web UI, library catalog browsing), with the
disk still unattached — the station just can't actually air until the
audio content itself is present.

## StereoTool

The binary and license are **never** part of this repo or the backup —
proprietary. **The binary itself is still a real manual step** (obtain
from the vendor, install at the expected path) — but **the license is
NOT a Phase 5 blocker, confirmed operational behavior (2026-08-18):**
StereoTool runs and processes audio with no license entered at all. The
unlicensed penalty for the feature set in use here is an occasional
audio watermark every few hours — an accepted tradeoff during disaster
recovery, not something this restore needs to work around. Entering the
real license key once the system is operational again is a roughly
one-minute manual step, done post-restore. Phase 5's automated stages,
service bring-up, and even controlled audio-chain validation may all
proceed with StereoTool unlicensed.

This is **not** the same claim as "the license is backed up or
vendor-recoverable" — that remains genuinely unverified (this audit did
not confirm StereoTool's actual reactivation mechanism: account/
purchase-order reissue vs. hardware-fingerprint-bound single activation
vs. something else). It simply means its absence during recovery is
tolerable, so resolving the *actual* reprovisioning mechanism is
optional follow-up work, not a Phase 5 or Phase 4.5 requirement.

`40-station-content.sh` restores the `.sts` processing profile (the one
piece of this genuinely backed up) and prints a checklist:

```
[ ] Profile (.sts) restored?       -- automated by 40-station-content.sh
[ ] Binary installed?              -- manual, obtain from vendor
[ ] License entered?                -- manual, post-restore, NOT a blocker;
                                       runs unlicensed with an occasional
                                       watermark until then (~1 minute to
                                       enter once ready)
[ ] Service unit valid?            -- automated by 90-system-config.sh
                                       (deploy/stereotool.service.example,
                                       copy + fill in placeholders + rename
                                       to stereotool.service deliberately --
                                       not matched by the *.service install
                                       glob on purpose)
```
The first, second, and fourth items gate **full station readiness**;
the license does not gate anything in this tooling's own sense — track
it as a reminder, not a checkpoint that blocks proceeding. See
`deploy/stereotool.service.example`'s own header comment for the
realtime-scheduling parameters (`CPUSchedulingPolicy=fifo`, priority 80,
`LimitRTPRIO=95`) that are load-bearing for glitch-free audio once it's
running.

## Certificates

`deploy/restore/90-system-config.sh` installs the **generic** nginx
template — self-signed cert only, `default_server`. It does not assume
`acme.sh`/Let's Encrypt state is recoverable (see the secrets table
above). Three options for the public HTTPS hostname, in order of
preference:

1. **Issue a fresh certificate** — re-run `acme.sh`'s DNS-01 issuance
   against the DNS provider (IONOS, for `radio.oakgroveradio.com`) once
   its own credentials are reprovisioned. Preferred: no dependency on
   anything from the dead host.
2. **Restore externally-preserved cert material**, if a copy of the
   actual `fullchain.pem`/`privkey.pem` exists somewhere outside the
   dead host (a password manager, a separate secrets vault). Faster,
   but only as good as whatever preserved it.
3. **Run temporarily on the self-signed cert only** — LAN/local access
   works immediately (see `deploy/README.md`'s "Public HTTPS with your
   own domain" section for the exact second-`server`-block pattern to
   add once a real cert is available). Public HTTPS is degraded, not
   broken — self-signed still serves the same content.

## Service bring-up order

Derived from the actual `After=` dependencies declared in `deploy/`'s
unit files (not assumed) plus each companion timer's real runtime
dependency on IsadoraAir's own DB/venv:

```
PostgreSQL (running, migrated -- stages 30 + 95 already confirmed this)
  |
  v
isadoraair-gunicorn                 (After= network, postgresql)
  |
  +--> isadoraair-monitoring        (After= ...gunicorn)
  |
  +--> StereoTool (external, manual -- see "StereoTool" above;
  |     After=sound.target, no IsadoraAir-side dependency)
  |       |
  |       +--> isadoraair-engine    (After= ...gunicorn)
  |       |       |
  |       |       v
  |       |     isadoraair-rbds     (After= ...gunicorn, engine, stereotool)
  |       |
  |       +--> isadoraair-encoders  (After= ...gunicorn, stereotool)
  |
  +--> companion timers/services    (syndicated-*, wx-*, ogremote-*,
  |     isadoraair-generate-dedication-intros -- all shell out to
  |     $ISA_ROOT/venv/bin/python manage.py <command>, so need
  |     gunicorn-confirmed-healthy as a practical readiness signal even
  |     though the systemd units themselves don't declare it)
  |
  +--> isadoraair-backup.timer, prune-*.timer, isadoraair-analyze.timer
  |     (low-risk, no strict ordering beyond DB+venv existing)
  |
  v
nginx (public interface -- reload only once gunicorn is confirmed
  listening on 127.0.0.1:8000; this repo's install step deliberately
  never auto-reloads nginx, see 90-system-config.sh)
```

**Reconciling with `deploy/*.service`'s own declared `After=` lines is
the source of truth if this ever needs re-deriving** — the graph above
is not the Phase 4 spec's own suggested example order verbatim; it's
what the actual units say, cross-checked.

### Software-complete vs. audio-hardware-ready

`95-validate.sh`'s PASS means the *software* restore is complete and
internally consistent — it does **not** mean the station can air. A
staging VM or spare host commonly lacks:

- the Topping D10s (or equivalent DAC/interface),
- the studio sound card itself,
- a Mixcast or external mixer,
- the actual transmitter path.

Services safe to bring up and exercise meaningfully **without** any of
that hardware: `isadoraair-gunicorn` (web UI, library browsing, admin),
`isadoraair-monitoring` (most checks — audio-silence detection excepted),
the companion timers (fetch/deliver into the library, independent of
playback), `nginx`. Services that need real hardware to do anything
useful: `isadoraair-engine` (GStreamer needs a real ALSA sink to produce
audible output — it may start cleanly and simply produce no sound, or
fail depending on ALSA device availability), `isadoraair-encoders`
(needs a real capture device), `isadoraair-rbds`/StereoTool (need the
real processing chain). Treat their non-function on hardware-less
staging as an expected, hardware-specific readiness gap, not a software
restore failure — this distinction matters specifically because Phase
5's drill environment may not reproduce every USB device exactly.

## ALSA / snd-aloop

`90-system-config.sh` installs `deploy/isadoraair-aloop.conf` to
`/etc/modprobe.d/` and `deploy/asound.conf` to `/etc/asound.conf`. A
kernel module reload (or reboot) is required afterward for the pinned
3-instance loopback layout to take effect —
`echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf` (loads at
boot) plus `sudo modprobe -r snd_aloop && sudo modprobe snd-aloop` (or
just reboot). Verify with `cat /proc/asound/cards` — three "Loopback"
entries at indices 0/3/4 alongside real hardware, or via
`manage.py check_deploy_baseline`'s own snd-aloop check. The unstable
real USB sound-card numbering (`docs/ALSA_DEVICE_INVENTORY.md`'s
`plughw:2,0` note) is **not** solved by this tooling — remains roadmap
item 1.3.

## After bring-up: confirming real readiness

Once services are up and hardware is attached:

```bash
sudo systemctl status isadoraair-gunicorn isadoraair-engine \
  isadoraair-encoders isadoraair-monitoring isadoraair-rbds
```
plus a real end-to-end check from the dashboard (`https://<host>/`) —
build an hour's log, confirm the engine actually plays audio, confirm a
stream connects if encoders are enabled. `manage.py
check_deploy_baseline` is read-only and safe to re-run at any point
during this process for a fast recheck.

## Known limitations (honest, not silently assumed away)

- `MANIFEST.txt` does not currently record whether the source tree was
  clean (no uncommitted changes) at backup time — a restore checks out
  exactly the recorded SHA and cannot recover anything that was
  uncommitted when the backup ran. Extending the backup script to
  record `git status --porcelain` output (or refuse to back up a dirty
  tree) is a natural future improvement, not implemented in this pass.
- `deploy/restore/90-system-config.sh`'s `nginx -t` and snd-aloop
  verification only run meaningfully against a REAL (non-staging)
  target — an isolated staging tree has no full nginx config context or
  kernel to load a module into, so those two checks are explicitly
  skipped (not faked) under `--staging-root`.
- `manage.py check_deploy_baseline` checks this HOST's actual configured
  paths (e.g. `/usr/local/bin/fdkaac`, `~/kokoro`), not a
  `--staging-root`'s isolated copies — during Phase 4's own staging
  validation this meant it was confirming production's real, already-
  correct state rather than the freshly-staged build/venv from earlier
  stages. Still a genuine, useful check; just not staging-root-aware by
  design (it's meant to run on the real target after a real restore).
