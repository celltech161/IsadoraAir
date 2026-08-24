# Protected updater runtime source

This directory is a review/distribution artifact. **Never point a root service
at this checkout.** Production executes only a root-owned copy at:

```
/usr/local/libexec/isadoraair-updater/
```

The initial privileged bootstrap uses fixed system tools only after an
unprivileged user has materialized and reviewed the exact release Git tree. Do not
run `sudo install` with source paths inside the live application checkout, use a
glob for privileged input, or execute a repository-owned installer. The exact
file-by-file commands, ownership/modes, disarmed-first sequence, PING probe,
sudo-policy verification, and final arming gate are maintained in
`docs/UPDATE_CENTER.md`.

The operator must edit the root-owned station identity deliberately, install a
reviewed/rendered copy of `deploy/isadoraair-updater.service`, and complete the
unrestricted-sudo remediation described in `docs/UPDATE_CENTER.md` before
arming update execution. Protocol v3 maintenance requests may be exercised
while the updater remains disarmed; `START_UPDATE` cannot.

Runtime v4 keeps protocol v3 and formalizes two boundaries discovered during
the first r0005 production update. The shipped service grants root only the
ambient `CAP_SETUID` and `CAP_SETGID` needed for `/usr/sbin/runuser`; it retains
`NoNewPrivileges=true`, `UMask=0077`, and the existing `Protect*`/`Restrict*`
sandbox. Daemon construction then runs the fixed, bounded command
`/usr/bin/id -u` as `application_user` and refuses to create its socket unless
the returned UID is exact. The application-user child does not retain those
capabilities.

Materialized job directories are explicitly corrected to mode `0711` after
creation, despite the service UMask. This permits traversal of the known
read-only `source/` path without permitting directory listing. The staged
source tree remains `0555`/`0444` (or `0555` for executable files), and
`target.tar` remains root-only `0600`.

Any release whose predecessor diff touches `deploy/updater_runtime/**` must set
`manual_bootstrap_required: true`. Both the Django planner and this protected
runtime derive and enforce that fact independently. The protected helper never
self-updates. The exact r0005-to-r0006 bridge and the manual systemd capability
acceptance procedure are in `docs/UPDATE_CENTER.md`.

If the trusted upstream uses SSH, provision a dedicated root-owned read-only
deploy key and known-host entry separately. Do not reuse an application-owned
SSH key or place credential material in `station.json`.
