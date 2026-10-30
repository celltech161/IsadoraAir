"""Prove, without running any test, that this process is isolated from station storage.

Used by production/tests/run_with_validation_service.sh before it starts any
service, and by hand to show the effective settings. It never touches the
database. Every probe targets a path that does not exist and uses a
non-creating mode, so even if the write boundary were absent nothing would be
created or changed; an unrefused probe fails the command.

Inside the kernel sandbox (production/tests/sandbox_exec.sh) the station paths
the probes name are not even mounted; the probes then show that the Python layer
refuses them on its own as well.

Exit status: 0 isolated and every probe refused; 2 not isolated / a probe was not refused.
"""
import json
import os
import socket
import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from isadoraair import test_isolation


def _connect(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)


class Command(BaseCommand):
    help = "Print the effective test-isolation state and prove protected paths are unwritable."

    def handle(self, *args, **options):
        try:
            state = test_isolation.assert_engaged(settings)
        except test_isolation.UnsafeTestEnvironmentError as exc:
            self.stderr.write(str(exc))
            sys.exit(2)

        namespace = state["namespace"]
        probe_name = f".isadoraair-preflight-probe-{os.getpid()}"
        symlink = os.path.join(namespace, f"alias-to-waveforms-{os.getpid()}")
        os.symlink("/srv/isadoraair/waveforms", symlink)       # inside the namespace: allowed
        probes = [
            ("open r+  production waveforms", lambda: os.open(f"/srv/isadoraair/waveforms/{probe_name}", os.O_RDWR)),
            ("open w   production music (no O_CREAT)", lambda: os.open(f"/srv/isadoraair/music/{probe_name}", os.O_WRONLY)),
            ("open r+  via symlink alias", lambda: os.open(os.path.join(symlink, probe_name), os.O_RDWR)),
            ("open r+  via '..' traversal", lambda: os.open(f"{namespace}{'/..' * namespace.count('/')}/srv/isadoraair/waveforms/{probe_name}", os.O_RDWR)),
            ("open r+  live checkout", lambda: os.open(f"/opt/isadoraair/{probe_name}", os.O_RDWR)),
            ("rename   out of production waveforms", lambda: os.rename(f"/srv/isadoraair/waveforms/{probe_name}", os.path.join(namespace, "x"))),
            ("remove   production reports", lambda: os.remove(f"/var/lib/isadoraair/reports/{probe_name}")),
            ("chmod    production weather", lambda: os.chmod(f"/var/lib/isadoraair/weather/{probe_name}", 0o600)),
            ("open r+  station runtime state", lambda: os.open(f"/run/isadoraair/{probe_name}", os.O_RDWR)),
            ("rename   into station runtime (write-and-rename)",
             lambda: os.rename(os.path.join(namespace, probe_name), f"/run/isadoraair/{probe_name}")),
            ("connect  station updater socket directory", lambda: _connect(f"/run/isadoraair-updater/{probe_name}.sock")),
        ]
        results, failed = [], False
        try:
            for label, action in probes:
                try:
                    action()
                    outcome = "NOT REFUSED (operation was attempted)"
                except test_isolation.ProtectedPathWriteBlocked:
                    outcome = "refused"
                except OSError as exc:
                    outcome = f"NOT REFUSED ({type(exc).__name__}: reached the filesystem)"
                if outcome != "refused":
                    failed = True
                results.append({"probe": label, "outcome": outcome})
        finally:
            os.unlink(symlink)                                  # removing a link never touches its target
        test_isolation.clear_blocked_writes()                    # these refusals were deliberate

        ancestor = {path: bool(test_isolation.protected_match(path, ancestors=True))
                    for path in ("/srv", "/srv/isadoraair", "/var/lib", "/opt", "/run")}
        sandbox = test_isolation.sandbox_problems()
        failed = failed or bool(sandbox)
        report = {
            "isolated": not failed,
            "kernel_sandbox": {"run_directory": os.environ.get(test_isolation.SANDBOX_ENV),
                               "problems": sandbox},
            "namespace": namespace,
            "roots": state["roots"],
            "database": state["database"],
            "ingest_url_blank": not settings.WEB_REQUESTS_INGEST_URL,
            "ingest_key_blank": not settings.WEB_REQUESTS_INGEST_API_KEY,
            "hour_refill_install_armed": settings.HOUR_REFILL_INSTALL_ARMED,
            "validation_socket": settings.PRODUCTION_VALIDATION_SOCKET,
            "render_socket": settings.PRODUCTION_RENDER_SOCKET,
            "stale_configuration_not_honoured": state["stale_configuration"],
            "protected_roots": sorted(test_isolation.protected_roots()),
            "ancestor_of_protected": ancestor,
            "probes": results,
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        if failed:
            sys.exit(2)
