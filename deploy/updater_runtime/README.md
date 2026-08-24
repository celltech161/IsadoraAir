# Protected updater runtime source

This directory is a review/distribution artifact. **Never point a root service
at this checkout.** Production executes only a root-owned copy at:

```
/usr/local/libexec/isadoraair-updater/
```

The initial privileged bootstrap uses fixed system tools only after an
unprivileged user has materialized and reviewed the exact r0004 Git tree. Do not
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

If the trusted upstream uses SSH, provision a dedicated root-owned read-only
deploy key and known-host entry separately. Do not reuse an application-owned
SSH key or place credential material in `station.json`.
