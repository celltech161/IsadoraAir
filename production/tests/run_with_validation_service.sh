#!/bin/bash
# Run a command (normally the test suite) in the production validation
# topology, using only the invoking user's own systemd user manager:
#
#   production/tests/run_with_validation_service.sh /path/to/venv/bin/python manage.py test production ...
#
# The service runs under the command's own interpreter ($1), or under
# $VALIDATION_SERVICE_PYTHON when the command is not a Python invocation.
#
# 1. a transient isadoraair-validation service with the SAME lifecycle
#    properties as deploy/isadoraair-validation.service (delegated cpu/memory/
#    pids subtree, DelegateSubgroup=supervisor, KillMode=control-group,
#    Restart=always) -- the tests' web side talks to it over its socket;
# 2. a transient isadoraair-render service (2.22C2A) -- a SEPARATE unit with the
#    properties of deploy/isadoraair-render.service and its own socket
#    (PRODUCTION_RENDER_SOCKET) -- so renders run as on a station;
# 3. the command itself in a delegated scope (production/tests/run_delegated.sh)
#    -- only so the executors' own unit tests can create validation/render
#    leaves exactly as the services do.
# Both services are stopped (and their runs destroyed) when the command ends.
set -u
python="${VALIDATION_SERVICE_PYTHON:-$1}"
here=$(cd "$(dirname "$0")/../.." && pwd -P)
runtime=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/isadoraair-validation-test.XXXXXX")
chmod 700 "$runtime"
unit="isadoraair-validation-test-$$-${RANDOM}"
socket_path="$runtime/validator.sock"
env_args=(-E "PRODUCTION_VALIDATION_SOCKET=$socket_path" -E "PATH=$PATH" -E "HOME=$HOME")
while IFS='=' read -r name _; do
    case "$name" in GST_*) env_args+=(-E "$name=${!name}") ;; esac
done < <(env)

stop_service() {
    busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
        org.freedesktop.systemd1.Manager StopUnit ss "$unit.service" replace >/dev/null 2>&1 || true
    rm -rf "$runtime"
}
trap stop_service EXIT

systemd-run --user --quiet --collect --unit="$unit" \
    -p Delegate="cpu memory pids" -p DelegateSubgroup=supervisor -p KillMode=control-group \
    -p Restart=always -p RestartSec=1 -p UMask=0077 -p WorkingDirectory="$here" \
    "${env_args[@]}" "$python" "$here/manage.py" production_validation_service || exit 98
for _ in $(seq 1 300); do
    [ -S "$socket_path" ] && break
    sleep 0.1
done
if [ ! -S "$socket_path" ]; then
    echo "run_with_validation_service.sh: the validation service did not start" >&2
    journalctl --user -u "$unit" --no-pager -n 30 >&2 2>/dev/null || true
    exit 98
fi
export PRODUCTION_VALIDATION_SOCKET="$socket_path"

# The transient isadoraair-render service -- SEPARATE from the validator (its
#    own unit, socket, delegated subtree and admission), with the lifecycle
#    properties of deploy/isadoraair-render.service.
render_runtime=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/isadoraair-render-test.XXXXXX")
chmod 700 "$render_runtime"
render_unit="isadoraair-render-test-$$-${RANDOM}"
render_socket="$render_runtime/render.sock"
stop_render() {
    busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
        org.freedesktop.systemd1.Manager StopUnit ss "$render_unit.service" replace >/dev/null 2>&1 || true
    rm -rf "$render_runtime"
}
trap 'stop_render; stop_service' EXIT
systemd-run --user --quiet --collect --unit="$render_unit" \
    -p Delegate="cpu memory pids" -p DelegateSubgroup=supervisor -p KillMode=control-group \
    -p Restart=always -p RestartSec=1 -p UMask=0077 -p WorkingDirectory="$here" \
    -E "PRODUCTION_RENDER_SOCKET=$render_socket" -E "PATH=$PATH" -E "HOME=$HOME" \
    "$python" "$here/manage.py" production_render_service || exit 98
for _ in $(seq 1 300); do
    [ -S "$render_socket" ] && break
    sleep 0.1
done
if [ ! -S "$render_socket" ]; then
    echo "run_with_validation_service.sh: the render service did not start" >&2
    journalctl --user -u "$render_unit" --no-pager -n 30 >&2 2>/dev/null || true
    exit 98
fi
export PRODUCTION_RENDER_SOCKET="$render_socket"
systemd-run --user --scope --quiet -p Delegate=yes "$here/production/tests/run_delegated.sh" "$@"
