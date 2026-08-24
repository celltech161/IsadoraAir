"""IsadoraAir Managed Update Center -- [P0] Bucket 1 / 1.1.

Phase A established the non-privileged planning foundation. Release-manifest schema + validation (`manifest.py`), release-
chain assembly (`release_chain.py`), read-only git operations
(`git_adapter.py`), manifest-vs-repository cross-checking
(`cross_check.py`), update planning (`planner.py`), the durable
`UpdateJob` schema (`models.py`), and `/updates/` status page remain.
Phase C adds a superuser-only submission/status UI and a narrow client for the
separately installed protocol-v3 root broker. No privileged code executes from
this package; package/apt/rollback/updater-self-update scope remains blocked.
See docs/UPDATE_CENTER.md for the authoritative shipped contract.

## Bootstrap note

This app's own `0001_initial` migration (a plain `CreateModel` for
`UpdateJob`, nothing else) has to be applied to a station the same way
every migration was applied before this feature existed: by an
operator running `manage.py migrate` by hand, once, the first time
Update Center support is installed. There is no way around this --
the Update Center cannot use itself to install itself. See
docs/UPDATE_CENTER.md's "Bootstrap release sequence" section."""
