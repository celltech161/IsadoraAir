# Protected updater runtime source

This directory is a review/distribution artifact. **Never point a root service
at this checkout.** Production executes only a root-owned copy at:

```
/usr/local/libexec/isadoraair-updater/
```

The initial privileged bootstrap uses fixed system tools, after review. It does
not run repository-owned Python as root:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec/isadoraair-updater
sudo install -d -o root -g root -m 0755 /usr/local/libexec/isadoraair-updater/isadoraair_updater
sudo install -o root -g root -m 0755 deploy/updater_runtime/updaterd.py /usr/local/libexec/isadoraair-updater/updaterd.py
sudo install -o root -g root -m 0644 deploy/updater_runtime/isadoraair_updater/*.py /usr/local/libexec/isadoraair-updater/isadoraair_updater/
sudo install -d -o root -g root -m 0755 /etc/isadoraair
sudo install -o root -g root -m 0600 deploy/updater-station.example.json /etc/isadoraair/station.json
sudo install -d -o root -g root -m 0755 /var/backups/isadoraair
sudo install -d -o root -g root -m 0700 /var/backups/isadoraair/update-checkpoints
```

The operator must edit the root-owned station identity deliberately, install a
reviewed/rendered copy of `deploy/isadoraair-updater.service`, and complete the
unrestricted-sudo remediation described in `docs/UPDATE_CENTER.md` before any
Phase C activation. Phase B does not authorize enabling this service.

If the trusted upstream uses SSH, provision a dedicated root-owned read-only
deploy key and known-host entry separately. Do not reuse an application-owned
SSH key or place credential material in `station.json`.
