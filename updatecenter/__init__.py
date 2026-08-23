"""IsadoraAir Managed Update Center -- [P0] Bucket 1 / 1.1.

Phase A (this app, as it exists right now): non-privileged foundation
only. Release-manifest schema + validation (`manifest.py`), release-
chain assembly (`release_chain.py`), read-only git operations
(`git_adapter.py`), manifest-vs-repository cross-checking
(`cross_check.py`), update planning (`planner.py`), the durable
`UpdateJob` schema (`models.py`), and a read-only `/updates/` status
page (`views.py`). There is no working "Update IsadoraAir" execution
path anywhere in this app, no privileged code, and nothing here
changes the checkout, runs a migration, runs pip, installs a systemd
unit, reloads systemd, restarts a service, writes nginx config, or
runs apt. See docs/UPDATE_CENTER.md for the full design and the
authoritative shipped contract.

## Bootstrap note

This app's own `0001_initial` migration (a plain `CreateModel` for
`UpdateJob`, nothing else) has to be applied to a station the same way
every migration was applied before this feature existed: by an
operator running `manage.py migrate` by hand, once, the first time
Update Center support is installed. There is no way around this --
the Update Center cannot use itself to install itself. See
docs/UPDATE_CENTER.md's "Bootstrap release sequence" section."""
