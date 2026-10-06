#!/bin/bash
# Run a command inside a delegated cgroup v2 subtree -- the kind of subtree the
# isadoraair-validation service gets from systemd (deploy/isadoraair-
# validation.service: Delegate= + DelegateSubgroup=supervisor) -- with the
# command in its "web" subgroup.
#
# Used by production/tests/run_with_validation_service.sh so that the
# executor's own unit tests (production.tests.test_confinement) can create
# validation leaves exactly as the service does. Run the suite through that
# wrapper, not this script directly.
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
