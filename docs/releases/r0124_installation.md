# r0124 installation and verification

Publication does not authorize installation. KOGR and WRJE each require a separate operator
decision and their own Update Center preview.

## Expected trusted plan from r0123

For a clean station installed at the r0123 release commit
`1a7c5b113a31f619ab20a7361be3eecf2f285559`, the preview must show:

- releases `[r0124]`;
- no migrations or manual migration operations;
- no r0124 migration-authorization companion;
- no collectstatic, Python requirement, APT package, systemd reconciliation, nginx or
  protected-runtime action; and
- restart exactly `isadoraair-gunicorn`.

Stop if the actual station baseline or preview differs. A station on an earlier release needs
review of its actual multi-release plan rather than extrapolation from this document.

## Before installation — each station

1. Record the installed release, exact HEAD and branch, and confirm the checkout is clean.
2. Confirm the trusted remote target is the published r0124 release commit and that its manifest
   resolves to that one immutable introducing commit.
3. Confirm protected-updater health and current checkpoint/recovery evidence under station policy.
4. Review the Update Center plan against the exact declaration above. It must have no
   `UNKNOWN_MANAGED_UNIT`, migration/manual-operation, companion, manifest/diff, protected-runtime
   or ancestry blocker.
5. Confirm Gunicorn is healthy. No station waveform regeneration, marker edit or Track 35884
   operation is part of this release installation.

## Installation

Use only the normal trusted Update Center workflow. It checks out the r0124 release commit and
restarts Gunicorn. Do not run migrations, create a local companion, edit the release manifest,
restart the engine or analyzer timer, or bypass a planner refusal.

## After installation

1. Confirm the Update Center job succeeded, the installed release is r0124, HEAD is the published
   r0124 release commit on `main`, and the checkout is clean.
2. Confirm `manage.py check` reports no new issue.
3. Confirm Gunicorn restarted and is healthy. Confirm engine, Monitoring, active-message worker and
   analyzer timer were not restarted by this plan and remain healthy.
4. Confirm the protected runtime is unchanged and the job reports no migration, collectstatic,
   package, unit or nginx action.
5. Open a Track Detail page and confirm **Reanalyze Track** is the only waveform action. Do not use
   a production Track merely to test the button.
6. Confirm Monitoring Recent Events still shows genuine operational warnings and does not surface
   routine cue-marker activity. This is a display filter only; underlying records remain intact.

Record the station, prior release, r0124 release identity, preview, job result, restart identity and
post-install checks separately for KOGR and WRJE.
