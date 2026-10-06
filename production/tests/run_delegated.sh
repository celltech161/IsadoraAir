#!/bin/bash
# Run a command inside a delegated cgroup v2 subtree, the way production runs
# Gunicorn (deploy/isadoraair-gunicorn.service: Delegate= + DelegateSubgroup=web).
#
# Media validation (production.services.confinement) runs every tool in a
# kernel-enforced per-run cgroup inside the CALLER's delegated subtree and fails
# closed without one, so tests that run the real validators need this:
#
#   systemd-run --user --scope -p Delegate=yes --quiet \
#       production/tests/run_delegated.sh python manage.py test production
#
# Unprivileged: it only uses the invoking user's own systemd user manager.
set -eu
own=$(sed -n 's/^0:://p' /proc/self/cgroup)
scope="/sys/fs/cgroup${own}"
if [ ! -w "${scope}/cgroup.procs" ] || [ ! -w "${scope}/cgroup.subtree_control" ]; then
    echo "run_delegated.sh: ${scope} is not a delegated cgroup; start me with" >&2
    echo "  systemd-run --user --scope -p Delegate=yes --quiet $0 <command ...>" >&2
    exit 97
fi
mkdir -p "${scope}/web"
echo $$ > "${scope}/web/cgroup.procs"
exec "$@"
