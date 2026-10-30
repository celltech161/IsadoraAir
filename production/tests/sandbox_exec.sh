#!/bin/bash
# Run one process tree of a test run inside a bubblewrap kernel sandbox:
#
#   production/tests/sandbox_exec.sh <role> -- <command ...>
#
# Only production/tests/run_with_validation_service.sh calls this (see
# docs/TEST_ISOLATION.md). It is the operating-system boundary under the Python
# write guard: whatever the command starts -- ffmpeg, a shell, another
# interpreter, psql -- inherits it, because every process is a descendant in the
# same namespaces.
#
#   * a user namespace with no capabilities, no_new_privs, and nested user
#     namespaces disabled (nothing inside can remount or create mounts);
#   * private PID, IPC, UTS and network namespaces: no host process can be seen
#     or signalled (the station's engine runs as this same user), only loopback,
#     no TCP route to the production database, no abstract Unix sockets;
#   * a minimal mount view: /usr and /etc read-only (station and service
#     configuration directories masked empty), the code paths in
#     ISADORAAIR_SANDBOX_RO read-only with any .env file masked, fresh private
#     /tmp /run /var /dev, and only the run's scratch directories writable.
#     /srv, /opt, /var/lib, the invoking user's home, /run/user (the systemd
#     user manager and D-Bus), /run/postgresql and every station path are simply
#     not there;
#   * an allow-listed environment (--clearenv): HOME, TMPDIR and XDG_* point
#     into the run directory; database settings come from the run's control
#     directory and name the run's disposable PostgreSQL server.
#
# Roles:
#   plain    no cgroup write access (the preflight, the disposable PostgreSQL server)
#   service  a transient unit with DelegateSubgroup=supervisor: the unit's
#            delegated cgroup (the parent of supervisor) is the one writable cgroup
#   scope    a delegated scope: this launcher first moves itself to
#            <scope>/supervisor, so the sandbox's own processes sit where a
#            service's supervisor does and the command can create siblings
#
# Required environment, set by the runner:
#   ISADORAAIR_TEST_SANDBOX     the run directory   <base>/isadoraair-test-run.XXXXXX (0700)
#   ISADORAAIR_TEST_CONTROL     the control directory <base>/isadoraair-test-ctl.XXXXXX (0700),
#                               never mounted in any sandbox
#   ISADORAAIR_TEST_DATA_ROOT   the per-run data namespace <base>/isadoraair-test-data.XXXXXX
#   ISADORAAIR_SANDBOX_RO       colon-separated read-only code paths
# Optional: ISADORAAIR_SANDBOX_EXTRA_RW (the PostgreSQL data directory, for the
# server only), PRODUCTION_VALIDATION_SOCKET / PRODUCTION_RENDER_SOCKET (inside
# the run directory), GST_* variables.
#
# Exit 96 means the sandbox could not be established; nothing was started.
set -euo pipefail
die() { echo "sandbox_exec.sh: REFUSED: $*" >&2 || true; exit 96; }

[ $# -ge 3 ] && [ "$2" = "--" ] || die "usage: sandbox_exec.sh plain|service|scope -- <command ...>"
role=$1
shift 2
case "$role" in plain|service|scope) ;; *) die "unknown role $role" ;; esac
bwrap=/usr/bin/bwrap
[ -x "$bwrap" ] || die "$bwrap is not installed"

# Station storage, the live checkout and the station's service configuration.
live=$(readlink -f /opt/isadoraair 2>/dev/null || true)
protected=(/srv/isadoraair /var/lib/isadoraair /opt/isadoraair /opt/isadoraair-runtime /etc/isadoraair)
[ -n "$live" ] && [ "$live" != /opt/isadoraair ] && protected+=("$live")
overlaps() {  # overlaps PATH: inside or containing a protected root
    local p=$1 r
    for r in "${protected[@]}"; do
        case "$p/" in "$r"/*) return 0 ;; esac
        case "$r/" in "$p"/*) return 0 ;; esac
    done
    return 1
}

# A scratch directory the runner made: canonical, ours, 0700, with the expected name.
scratch_dir() {  # scratch_dir VAR PREFIX
    local value=${!1:-} base
    [ -n "$value" ] || die "$1 is not set"
    overlaps "$value" && die "$1=$value overlaps station storage"
    [ "$(readlink -f -- "$value")" = "$value" ] || die "$1=$value is not canonical"
    [ -d "$value" ] && [ ! -L "$value" ] || die "$1=$value is not a directory"
    [ "$(stat -c %u -- "$value")" = "$(id -u)" ] || die "$1=$value is not owned by $(id -un)"
    [ "$(stat -c %a -- "$value")" = 700 ] || die "$1=$value is not mode 0700"
    case "$(basename -- "$value")" in "$2"*) ;; *) die "$1=$value is not named $2*" ;; esac
    base=$(dirname -- "$value")
    overlaps "$base" && die "$1=$value overlaps station storage"
    printf '%s\n' "$base"
}
run_base=$(scratch_dir ISADORAAIR_TEST_SANDBOX isadoraair-test-run.)
ctl_base=$(scratch_dir ISADORAAIR_TEST_CONTROL isadoraair-test-ctl.)
data_base=$(scratch_dir ISADORAAIR_TEST_DATA_ROOT isadoraair-test-data.)
[ "$run_base" = "$ctl_base" ] && [ "$run_base" = "$data_base" ] || die "run, control and data directories must share one base"
run=$ISADORAAIR_TEST_SANDBOX
ctl=$ISADORAAIR_TEST_CONTROL
data=$ISADORAAIR_TEST_DATA_ROOT

writable=("$data")
if [ -n "${ISADORAAIR_SANDBOX_EXTRA_RW:-}" ]; then
    [ "$role" = plain ] || die "ISADORAAIR_SANDBOX_EXTRA_RW is only for the database server"
    extra=$(ISADORAAIR_X=$ISADORAAIR_SANDBOX_EXTRA_RW scratch_dir ISADORAAIR_X isadoraair-test-pg.)
    [ "$extra" = "$run_base" ] || die "the database directory must share the run's base"
    writable+=("$ISADORAAIR_SANDBOX_EXTRA_RW")
fi

# Read-only code paths. A path inside the live checkout is refused except that
# checkout's .git directory (the worktree's object store; git reads only).
ro=()
IFS=: read -r -a requested <<<"${ISADORAAIR_SANDBOX_RO:-}"
[ ${#requested[@]} -gt 0 ] || die "ISADORAAIR_SANDBOX_RO is empty"
for p in "${requested[@]}"; do
    [ -n "$p" ] || continue
    [ "$(readlink -f -- "$p")" = "$p" ] && [ -d "$p" ] || die "read-only path $p is not a canonical directory"
    if overlaps "$p" && ! { [ -n "$live" ] && [ "$p" = "$live/.git" ]; }; then
        die "read-only path $p overlaps station storage or the live checkout"
    fi
    ro+=("$p")
done

# The run's disposable database and a per-run SECRET_KEY, from the control
# directory (sandboxed code cannot write there). Parsed, never sourced.
run_env=()
while IFS='=' read -r key value; do
    case "$key" in DB_NAME|DB_USER|DB_PASSWORD|DB_HOST|DB_PORT|SECRET_KEY) ;; *) continue ;; esac
    [[ "$value" =~ ^[A-Za-z0-9_./-]+$ ]] || die "malformed $key in the control directory"
    run_env+=(--setenv "$key" "$value")
done <"$ctl/sandbox.env"
[ ${#run_env[@]} -eq 18 ] || die "the control directory does not define the disposable database"

# Standard streams may not hand a directory or station storage into the sandbox.
# (/proc/$$, not /proc/self: inside $(...) fd 1 would be the substitution's pipe.)
for fd in 0 1 2; do
    target=$(readlink "/proc/$$/fd/$fd" 2>/dev/null || true)
    [ -n "$target" ] || continue
    [ -d "/proc/$$/fd/$fd" ] && die "fd $fd is a directory ($target)"
    case "$target" in /*) overlaps "$(dirname -- "$target")" && die "fd $fd is in station storage ($target)" ;; esac
done
# Close every other inherited descriptor (bash's own script fd 255 is close-on-exec).
for path in /proc/$$/fd/*; do
    fd=${path##*/}
    if [ "$fd" -gt 2 ] && [ "$fd" -ne 255 ]; then eval "exec $fd>&-" 2>/dev/null || true; fi
done

cgroup_args=()
own=$(sed -n 's/^0:://p' /proc/self/cgroup)
case "$role" in
    service)
        [ "${own##*/}" = supervisor ] || die "service role outside a DelegateSubgroup=supervisor unit ($own)"
        cgroot="/sys/fs/cgroup${own%/supervisor}" ;;
    scope)
        cgroot="/sys/fs/cgroup${own}"
        [ -w "$cgroot/cgroup.procs" ] && [ -w "$cgroot/cgroup.subtree_control" ] || die "$cgroot is not a delegated scope"
        mkdir -p "$cgroot/supervisor"
        echo $$ >"$cgroot/supervisor/cgroup.procs" ;;
    plain) cgroot= ;;
esac
if [ -n "$cgroot" ]; then
    case "$cgroot" in /sys/fs/cgroup/user.slice/user-"$(id -u)".slice/user@"$(id -u)".service/*) ;;
        *) die "$cgroot is not in this user's own service manager subtree" ;; esac
    [ "$(stat -c %u -- "$cgroot")" = "$(id -u)" ] || die "$cgroot is not delegated to $(id -un)"
    cgroup_args=(--bind "$cgroot" "$cgroot")
    writable+=("$cgroot")
fi

args=(--unshare-user --disable-userns --unshare-pid --unshare-ipc --unshare-net --unshare-uts
      --hostname isadoraair-test-sandbox --die-with-parent --new-session --cap-drop ALL
      --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin
      --symlink usr/lib /lib --symlink usr/lib64 /lib64
      --ro-bind /etc /etc)
for masked in /etc/isadoraair /etc/ssl/isadoraair /etc/systemd /etc/nginx /etc/postgresql /etc/postgresql-common /etc/ssh \
              /etc/default /etc/needrestart /etc/sudoers.d /etc/credstore /etc/credstore.encrypted; do
    if [ -d "$masked" ]; then args+=(--tmpfs "$masked" --remount-ro "$masked"); fi
done
args+=(--proc /proc --dev /dev --tmpfs /tmp --tmpfs /run --tmpfs /var --ro-bind /sys /sys)
for p in "${ro[@]}"; do
    args+=(--ro-bind "$p" "$p")
    for secret in "$p"/.env "$p"/.env.*; do
        if [ -f "$secret" ] && [ "${secret##*/}" != .env.example ]; then args+=(--ro-bind "$ctl/empty" "$secret"); fi
    done
done
args+=(--bind "$run" "$run")
for p in "${writable[@]}"; do
    [ "$p" = "$cgroot" ] || args+=(--bind "$p" "$p")
done
args+=("${cgroup_args[@]}")

args+=(--clearenv
       --setenv PATH "${ISADORAAIR_SANDBOX_PATH_PREFIX:+$ISADORAAIR_SANDBOX_PATH_PREFIX:}/usr/local/bin:/usr/bin:/bin"
       --setenv HOME "$run/home" --setenv TMPDIR "$run/tmp" --setenv XDG_RUNTIME_DIR "$run/xdg"
       --setenv XDG_CACHE_HOME "$run/home/.cache" --setenv XDG_CONFIG_HOME "$run/home/.config"
       --setenv LANG C.UTF-8 --setenv PYTHONDONTWRITEBYTECODE 1 --setenv PYTHONNOUSERSITE 1
       --setenv ISADORAAIR_TEST_SANDBOX "$run" --setenv ISADORAAIR_TEST_DATA_ROOT "$data"
       --setenv ISADORAAIR_SANDBOX_WRITABLE "$(IFS=:; echo "${writable[*]}")"
       "${run_env[@]}")
for name in PRODUCTION_VALIDATION_SOCKET PRODUCTION_RENDER_SOCKET; do
    value=${!name:-}
    [ -n "$value" ] || continue
    case "$value" in "$run"/*) args+=(--setenv "$name" "$value") ;; *) die "$name=$value is not inside the run directory" ;; esac
done
while IFS='=' read -r name value; do
    [[ "$name" =~ ^GST_[A-Z0-9_]+$ ]] && args+=(--setenv "$name" "$value")
done < <(env)
[ -n "${TZ:-}" ] && args+=(--setenv TZ "$TZ")
args+=(--chdir "${ISADORAAIR_SANDBOX_CHDIR:-${ro[0]}}" --remount-ro /)

exec "$bwrap" "${args[@]}" -- "$@"
