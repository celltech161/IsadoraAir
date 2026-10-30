#!/bin/bash
# Run a command (normally the test suite) in the production validation
# topology, inside a kernel sandbox, using only the invoking user's own systemd
# user manager:
#
#   production/tests/run_with_validation_service.sh /path/to/venv/bin/python manage.py test production ...
#
# The service runs under the command's own interpreter ($1), or under
# $VALIDATION_SERVICE_PYTHON when the command is not a Python invocation.
#
# Every process of the run -- the preflight, a disposable PostgreSQL server,
# the two services and the command with all of its children (ffmpeg, shells,
# psql, ...) -- runs inside production/tests/sandbox_exec.sh: a bubblewrap
# sandbox with no station storage mounted, no view of host processes, no
# network, no systemd/D-Bus or host database socket, and only this run's
# scratch directories writable (docs/TEST_ISOLATION.md). Nothing starts unless
# that sandbox can be built, and each service is checked from outside: a
# process in its unit that is not in the sandbox's namespaces fails the run.
#
# 1. a disposable PostgreSQL cluster (its own sandbox, Unix socket only,
#    inside the run directory) holding the development role isadoraair_dev
#    (not a superuser) -- the host's PostgreSQL is unreachable from the sandbox;
# 2. a transient isadoraair-validation service with the SAME lifecycle
#    properties as deploy/isadoraair-validation.service (delegated cpu/memory/
#    pids subtree, DelegateSubgroup=supervisor, KillMode=control-group,
#    Restart=always) -- the tests' web side talks to it over its socket;
# 3. a transient isadoraair-render service (2.22C2A) -- a SEPARATE unit with the
#    properties of deploy/isadoraair-render.service and its own socket
#    (PRODUCTION_RENDER_SOCKET) -- so renders run as on a station;
# 4. the command itself in a delegated scope (production/tests/run_delegated.sh)
#    -- only so the executors' own unit tests can create validation/render
#    leaves exactly as the services do.
# Both services, the database server and all scratch directories are removed
# when the command ends.
set -u
python="${VALIDATION_SERVICE_PYTHON:-$1}"
here=$(cd "$(dirname "$0")/../.." && pwd -P)
sandbox="$here/production/tests/sandbox_exec.sh"
fail() { echo "run_with_validation_service.sh: $*" >&2; exit 98; }
# This runner is outside the sandbox and has the user's full access, so it never
# writes to a caller-chosen path. The former ISADORAAIR_SANDBOX_EVIDENCE and
# ISADORAAIR_PREFLIGHT_COPY copy hooks are gone; refuse them before anything is
# created rather than silently ignore a caller who expects a copy.
for hook in ISADORAAIR_SANDBOX_EVIDENCE ISADORAAIR_PREFLIGHT_COPY; do
    [ -z "${!hook:-}" ] || fail "$hook is no longer supported (unsafe evidence destination: the runner never copies output to a caller-chosen path); unset it -- nothing was started"
done
umask 077

[ -x /usr/bin/bwrap ] || fail "bubblewrap (/usr/bin/bwrap) is required: tests run only inside the kernel sandbox"
pg_bin=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n 1)
[ -n "$pg_bin" ] && [ -x "$pg_bin/postgres" ] && [ -x "$pg_bin/initdb" ] \
    || fail "PostgreSQL server binaries (initdb, postgres) are required for the disposable test database"
case "$python" in /*) ;; *) fail "the interpreter must be an absolute path (got $python)" ;; esac

# Read-only code paths: this checkout first (the sandbox's working directory),
# its git object store when it is a linked worktree, and the interpreter's venv.
ro="$here"
common=$(git -C "$here" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [ -n "$common" ] && [ -d "$common" ]; then
    common=$(cd "$common" && pwd -P)
    case "$common/" in "$here"/*) ;; *) ro="$ro:$common" ;; esac
fi
path_prefix=
if [ -f "$(dirname "$python")/../pyvenv.cfg" ]; then
    venv=$(cd "$(dirname "$python")/.." && pwd -P)
    ro="$ro:$venv"
    path_prefix="$venv/bin"
fi

# Scratch, all directly in /tmp (the sandbox mounts each at the same path):
#   run   writable inside every sandbox: HOME, TMPDIR, sockets
#   data  the per-run data-root namespace (isadoraair/test_isolation.py)
#   pg    the disposable cluster's data directory (only the database sandbox)
#   ctl   control files and logs, never mounted in any sandbox
ctl=$(mktemp -d /tmp/isadoraair-test-ctl.XXXXXX) || exit 98
run=$(mktemp -d /tmp/isadoraair-test-run.XXXXXX) || exit 98
data=$(mktemp -d /tmp/isadoraair-test-data.XXXXXX) || exit 98
pg=$(mktemp -d /tmp/isadoraair-test-pg.XXXXXX) || exit 98
unit="isadoraair-validation-test-$$-${RANDOM}"
render_unit="isadoraair-render-test-$$-${RANDOM}"
pg_pid=
stop_unit() {
    busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
        org.freedesktop.systemd1.Manager StopUnit ss "$1.service" replace >/dev/null 2>&1 || true
}
cleanup() {
    stop_unit "$render_unit"
    stop_unit "$unit"
    if [ -n "$pg_pid" ]; then kill "$pg_pid" 2>/dev/null; wait "$pg_pid" 2>/dev/null; fi
    rm -rf -- "$run" "$data" "$pg" "$ctl"
}
trap cleanup EXIT
mkdir -m 700 "$run/home" "$run/tmp" "$run/xdg" "$run/pgsock" "$run/validation" "$run/render"
: >"$ctl/empty"
export ISADORAAIR_TEST_SANDBOX="$run" ISADORAAIR_TEST_CONTROL="$ctl" ISADORAAIR_TEST_DATA_ROOT="$data"
export ISADORAAIR_SANDBOX_RO="$ro" ISADORAAIR_SANDBOX_PATH_PREFIX="$path_prefix"
unset PRODUCTION_VALIDATION_SOCKET PRODUCTION_RENDER_SOCKET ISADORAAIR_SANDBOX_EXTRA_RW

# 1. The disposable database. The bootstrap superuser has no pg_hba entry, so
#    nothing can log in as it; isadoraair_dev is the only login role.
password=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
printf 'DB_NAME=isadoraair_dev\nDB_USER=isadoraair_dev\nDB_PASSWORD=%s\nDB_HOST=%s\nDB_PORT=5432\nSECRET_KEY=%s\n' \
    "$password" "$run/pgsock" "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >"$ctl/sandbox.env"
ISADORAAIR_SANDBOX_EXTRA_RW="$pg" "$sandbox" plain -- "$pg_bin/initdb" -D "$pg/data" -U isadoraair_bootstrap \
    --auth=reject -E UTF8 --locale=C.UTF-8 --no-sync --no-instructions >"$ctl/initdb.log" 2>&1 \
    || { cat "$ctl/initdb.log" >&2; fail "could not create the disposable database cluster"; }
# $pg is writable by the database sandbox, so the runner does not write into it
# itself: pg_hba.conf is replaced from inside that sandbox.
printf 'local\tpostgres,isadoraair_dev,test_isadoraair_dev\tisadoraair_dev\tscram-sha-256\n' \
    | ISADORAAIR_SANDBOX_EXTRA_RW="$pg" "$sandbox" plain -- /bin/sh -c 'cat >"$1"' sh "$pg/data/pg_hba.conf" \
    >"$ctl/pg_hba.log" 2>&1 || { cat "$ctl/pg_hba.log" >&2; fail "could not write the disposable cluster's pg_hba.conf"; }
printf "CREATE ROLE isadoraair_dev LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '%s';\nCREATE DATABASE isadoraair_dev OWNER isadoraair_dev;\n" "$password" \
    | ISADORAAIR_SANDBOX_EXTRA_RW="$pg" "$sandbox" plain -- "$pg_bin/postgres" --single -D "$pg/data" postgres \
    >"$ctl/bootstrap.log" 2>&1 || { cat "$ctl/bootstrap.log" >&2; fail "could not create the development role"; }
ISADORAAIR_SANDBOX_EXTRA_RW="$pg" "$sandbox" plain -- "$pg_bin/postgres" -D "$pg/data" \
    -c listen_addresses= -c unix_socket_directories="$run/pgsock" -c unix_socket_permissions=0700 -c port=5432 \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off >"$ctl/postgres.log" 2>&1 &
pg_pid=$!
for _ in $(seq 1 300); do
    [ -S "$run/pgsock/.s.PGSQL.5432" ] && break
    kill -0 "$pg_pid" 2>/dev/null || break
    sleep 0.1
done
[ -S "$run/pgsock/.s.PGSQL.5432" ] || { cat "$ctl/postgres.log" >&2; fail "the disposable database did not start"; }

# Fail closed BEFORE any service starts: prove, inside the sandbox, the effective
# settings, the kernel facts and the write boundary (see the command's docstring).
"$sandbox" plain -- "$python" "$here/manage.py" test_isolation_preflight >"$ctl/preflight.json" || {
    echo "run_with_validation_service.sh: test isolation preflight FAILED -- nothing was started" >&2
    cat "$ctl/preflight.json" >&2
    exit 98
}

# Every process in a unit must be the sandbox's outer bwrap or inside the
# sandbox's mount, network and PID namespaces; anything else fails the run.
verify_unit() {
    local name=$1 cgroup pid exe ns inside=0 self_mnt self_net self_pid
    self_mnt=$(readlink /proc/self/ns/mnt); self_net=$(readlink /proc/self/ns/net); self_pid=$(readlink /proc/self/ns/pid)
    cgroup=$(systemctl --user show -p ControlGroup --value "$name.service")
    [ -n "$cgroup" ] && [ -d "/sys/fs/cgroup$cgroup" ] || fail "$name has no cgroup"
    for pid in $(find "/sys/fs/cgroup$cgroup" -name cgroup.procs -exec cat {} +); do
        exe=$(readlink "/proc/$pid/exe" 2>/dev/null) || continue
        ns="$(readlink "/proc/$pid/ns/mnt") $(readlink "/proc/$pid/ns/net") $(readlink "/proc/$pid/ns/pid")"
        if [ "$ns" = "$self_mnt $self_net $self_pid" ]; then
            [ "$exe" = /usr/bin/bwrap ] || fail "$name: process $pid ($exe) runs OUTSIDE the sandbox"
        else
            case "$ns" in *"$self_mnt"*|*"$self_net"*) fail "$name: process $pid ($exe) shares a host namespace" ;; esac
            inside=$((inside + 1))
        fi
        echo "$name pid=$pid exe=$exe ns=[$ns]" >>"$ctl/units.txt"
    done
    [ "$inside" -gt 0 ] || fail "$name has no sandboxed process"
    # The unit's effective view, seen through its own mount namespace
    # (/proc/<pid>/root): host paths absent, the checkout not writable.
    pid=$(systemctl --user show -p MainPID --value "$name.service")
    pid=$(pgrep -P "$(pgrep -P "$pid" | head -n 1)" | head -n 1)
    [ -n "$pid" ] || fail "$name: cannot find the sandboxed service process"
    { echo "== $name pid=$pid status"; grep -E '^(Cap|NoNewPrivs)' "/proc/$pid/status"; echo "== mountinfo"; cat "/proc/$pid/mountinfo"; } \
        >"$ctl/unit-$name.txt"
    for path in /srv /opt /var/lib /run/user /run/postgresql "$HOME/.ssh" "$HOME/.pgpass" "$common/../.env"; do
        [ -e "/proc/$pid/root$path" ] && fail "$name: $path is visible inside the unit's sandbox"
    done
    probe="$here/.sandbox-write-probe-$$"
    if (: >"/proc/$pid/root$probe") 2>>"$ctl/unit-$name.txt"; then
        rm -f -- "$probe"
        fail "$name: the checkout is writable inside the unit's sandbox"
    fi
    echo "write probe through the unit's root refused (expected)" >>"$ctl/unit-$name.txt"
}
unit_env=(-E "ISADORAAIR_TEST_SANDBOX=$run" -E "ISADORAAIR_TEST_CONTROL=$ctl" -E "ISADORAAIR_TEST_DATA_ROOT=$data"
          -E "ISADORAAIR_SANDBOX_RO=$ro" -E "ISADORAAIR_SANDBOX_PATH_PREFIX=$path_prefix" -E "PATH=/usr/bin:/bin")
while IFS='=' read -r name _; do
    case "$name" in GST_*) unit_env+=(-E "$name=${!name}") ;; esac
done < <(env)
start_service() {  # start_service UNIT SOCKET-VARIABLE SOCKET COMMAND
    systemd-run --user --quiet --collect --unit="$1" \
        -p Delegate="cpu memory pids" -p DelegateSubgroup=supervisor -p KillMode=control-group \
        -p Restart=always -p RestartSec=1 -p UMask=0077 -p WorkingDirectory="$here" \
        "${unit_env[@]}" -E "$2=$3" "$sandbox" service -- "$python" "$here/manage.py" "$4" || exit 98
    for _ in $(seq 1 300); do
        [ -S "$3" ] && break
        sleep 0.1
    done
    if [ ! -S "$3" ]; then
        journalctl --user -u "$1" --no-pager -n 30 >&2 2>/dev/null || true
        fail "the $4 service did not start"
    fi
    verify_unit "$1"
}

# 2. The transient isadoraair-validation service.
socket_path="$run/validation/validator.sock"
start_service "$unit" PRODUCTION_VALIDATION_SOCKET "$socket_path" production_validation_service
export PRODUCTION_VALIDATION_SOCKET="$socket_path"

# 3. The transient isadoraair-render service -- SEPARATE from the validator (its
#    own unit, socket, delegated subtree and admission), with the lifecycle
#    properties of deploy/isadoraair-render.service.
render_socket="$run/render/render.sock"
start_service "$render_unit" PRODUCTION_RENDER_SOCKET "$render_socket" production_render_service
export PRODUCTION_RENDER_SOCKET="$render_socket"

# 4. The command, sandboxed, in a delegated scope.
systemd-run --user --scope --quiet -p Delegate=yes "$sandbox" scope -- "$here/production/tests/run_delegated.sh" "$@"
status=$?
exit "$status"
