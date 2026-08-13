"""Read-only runtime/dependency baseline preflight -- IsadoraAir 1.2
Phase 3. Reports whether this host has what IsadoraAir (and, best-
effort, the companion projects it depends on) needs to actually run --
never modifies anything. Deliberately small and deterministic rather
than a general diagnostics framework: one check per real dependency
this phase's audit actually identified, not an open-ended health
scanner.

Distinguishes four states per check, not a flat pass/fail:
  PASS      -- present and working
  DEGRADED  -- present but with a caveat worth knowing about
  MISSING   -- absent and required for what it checks
  OPTIONAL  -- absent, but the feature it supports is optional/unused
               here, so its absence does not fail the overall run

Reusable by the restore procedure (Phase 4), an installer, or release
validation -- run any time, any host, with no side effects.
"""
import os
import shutil
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection

REQUIRED_GST_ELEMENTS = [
    "alsasrc", "alsasink", "audioconvert", "audiodynamic", "audiomixer",
    "audioresample", "audiotestsrc", "capsfilter", "concat", "decodebin",
    "fakesink", "filesrc", "input-selector", "level", "opusdec", "opusenc",
    "queue", "rglimiter", "rtpopusdepay", "rtpopuspay", "tee", "volume",
    "webrtcbin",
]
# Format-specific decode path -- see docs/GSTREAMER_ELEMENT_INVENTORY.md.
REQUIRED_GST_DECODE_ELEMENTS = [
    "flacparse", "flacdec", "qtdemux", "avdec_aac", "mpegaudioparse",
    "id3demux", "avdec_mp3", "aiffparse", "wavparse",
]

MIN_PYTHON = (3, 14)


class Command(BaseCommand):
    help = (
        "Read-only preflight: reports whether this host has the runtime/"
        "dependency baseline IsadoraAir 1.2 Phase 3 established (Python, "
        "PostgreSQL tools, GStreamer + required elements, Liquidsoap, "
        "ALSA utils + snd-aloop layout, fdkaac + HE-AAC support, required "
        "directories, canonical /opt/isadoraair path, Kokoro, Piper). "
        "Never modifies the host. Exit code 0 if every REQUIRED check "
        "passes; nonzero otherwise. Missing OPTIONAL dependencies never "
        "fail the run."
    )

    def handle(self, *args, **options):
        results = []
        results.append(self._check_python())
        results.append(self._check_postgres_tools())
        results.append(self._check_postgres_connection())
        gst_ver, gst_results = self._check_gstreamer()
        results.append(gst_ver)
        results.extend(gst_results)
        results.append(self._check_liquidsoap())
        results.append(self._check_alsa_utils())
        results.extend(self._check_snd_aloop())
        results.append(self._check_fdkaac())
        results.extend(self._check_directories())
        results.extend(self._check_kokoro())
        results.extend(self._check_piper())

        required_failed = 0
        for state, label, detail in results:
            self.stdout.write(self._line(state, label, detail))
            if state == "MISSING":
                required_failed += 1

        self.stdout.write("")
        if required_failed:
            self.stdout.write(self.style.ERROR(
                f"FAIL: {required_failed} required dependency check(s) missing."
            ))
            self.stderr.write("check_deploy_baseline: FAIL")
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("PASS: runtime baseline satisfied."))

    # ---- output formatting -------------------------------------------
    def _line(self, state, label, detail):
        marker = {
            "PASS": self.style.SUCCESS("PASS    "),
            "DEGRADED": self.style.WARNING("DEGRADED"),
            "MISSING": self.style.ERROR("MISSING "),
            "OPTIONAL": "OPTIONAL",
        }[state]
        suffix = f": {detail}" if detail else ""
        return f"{marker}  {label}{suffix}"

    # ---- checks ---------------------------------------------------------
    def _check_python(self):
        import sys
        v = sys.version_info
        actual = f"{v.major}.{v.minor}.{v.micro}"
        if (v.major, v.minor) >= MIN_PYTHON:
            return ("PASS", "Python", actual)
        return ("MISSING", "Python", f"{actual} (need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})")

    def _check_postgres_tools(self):
        missing = [b for b in ("psql", "pg_dump", "pg_restore") if shutil.which(b) is None]
        if missing:
            return ("MISSING", "PostgreSQL client tools", f"missing: {', '.join(missing)}")
        return ("PASS", "PostgreSQL client tools", "psql, pg_dump, pg_restore on PATH")

    def _check_postgres_connection(self):
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT version()")
                version_str = cur.fetchone()[0]
            return ("PASS", "PostgreSQL connection", version_str.split(",")[0])
        except Exception as exc:
            return ("MISSING", "PostgreSQL connection", str(exc))

    def _check_gstreamer(self):
        gst_inspect = shutil.which("gst-inspect-1.0")
        if gst_inspect is None:
            ver_result = ("MISSING", "GStreamer", "gst-inspect-1.0 not found")
            elem_results = [("MISSING", f"  element {e}", "gst-inspect-1.0 unavailable")
                             for e in REQUIRED_GST_ELEMENTS + REQUIRED_GST_DECODE_ELEMENTS]
            return ver_result, elem_results

        try:
            out = subprocess.run([gst_inspect, "--version"], capture_output=True, text=True, timeout=10)
            ver_line = out.stdout.strip().splitlines()[0] if out.stdout else "unknown version"
        except Exception as exc:
            ver_line = f"version check failed: {exc}"
        ver_result = ("PASS", "GStreamer", ver_line)

        elem_results = []
        for elem in REQUIRED_GST_ELEMENTS:
            elem_results.append(self._check_one_gst_element(gst_inspect, elem, optional=False))
        for elem in REQUIRED_GST_DECODE_ELEMENTS:
            # Decode-path elements are format-specific (FLAC/AAC/MP3/AIFF/WAV) --
            # a library that happens to contain none of one format wouldn't
            # strictly need its decoder, but IsadoraAir's own production
            # library uses all five, so these are checked as required too.
            elem_results.append(self._check_one_gst_element(gst_inspect, elem, optional=False))
        return ver_result, elem_results

    def _check_one_gst_element(self, gst_inspect, elem, optional):
        try:
            result = subprocess.run([gst_inspect, elem], capture_output=True, text=True, timeout=10)
        except Exception as exc:
            return ("MISSING", f"  element {elem}", str(exc))
        if result.returncode != 0 or "No such element" in result.stdout + result.stderr:
            state = "OPTIONAL" if optional else "MISSING"
            return (state, f"  element {elem}", "not found")
        return ("PASS", f"  element {elem}", None)

    def _check_liquidsoap(self):
        liq = shutil.which("liquidsoap")
        if liq is None:
            return ("MISSING", "Liquidsoap", "not found on PATH")
        try:
            out = subprocess.run([liq, "--version"], capture_output=True, text=True, timeout=10)
            first_line = out.stdout.strip().splitlines()[0] if out.stdout else "version unknown"
        except Exception as exc:
            first_line = f"version check failed: {exc}"
        return ("PASS", "Liquidsoap", first_line)

    def _check_alsa_utils(self):
        missing = [b for b in ("aplay", "arecord") if shutil.which(b) is None]
        if missing:
            return ("MISSING", "ALSA utils", f"missing: {', '.join(missing)}")
        return ("PASS", "ALSA utils", "aplay, arecord on PATH")

    def _check_snd_aloop(self):
        results = []
        try:
            loaded = Path("/proc/modules").read_text()
            if "snd_aloop " not in loaded and not loaded.startswith("snd_aloop "):
                results.append(("MISSING", "snd-aloop module", "not loaded (modprobe snd-aloop)"))
                return results
        except Exception as exc:
            results.append(("MISSING", "snd-aloop module", f"could not read /proc/modules: {exc}"))
            return results
        results.append(("PASS", "snd-aloop module", "loaded"))

        try:
            cards = Path("/proc/asound/cards").read_text()
        except Exception as exc:
            results.append(("MISSING", "snd-aloop card layout", f"could not read /proc/asound/cards: {exc}"))
            return results

        loopback_indices = set()
        for line in cards.splitlines():
            line = line.strip()
            if not line or not line[0].isdigit():
                continue
            idx_str = line.split()[0]
            if "Loopback" in line:
                try:
                    loopback_indices.add(int(idx_str))
                except ValueError:
                    pass

        # Expected layout per deploy/isadoraair-aloop.conf: index=0,3,4.
        # A different but still-3-instance layout is DEGRADED, not
        # MISSING -- /etc/asound.conf's exact hw:Loopback_1,1,0 reference
        # would need re-checking against whatever indices are actually
        # present, which this preflight doesn't attempt (station-specific
        # asound.conf content, not a generic dependency question).
        expected = {0, 3, 4}
        if loopback_indices == expected:
            results.append(("PASS", "snd-aloop card layout", f"3 instances at indices {sorted(loopback_indices)}"))
        elif len(loopback_indices) >= 1:
            results.append(("DEGRADED", "snd-aloop card layout",
                             f"found indices {sorted(loopback_indices)}, expected {sorted(expected)} "
                             "(deploy/isadoraair-aloop.conf not installed, or a different layout in use)"))
        else:
            results.append(("MISSING", "snd-aloop card layout", "no Loopback cards found"))
        return results

    def _check_fdkaac(self):
        try:
            from encoders.services.encoder_manager import FDKAAC_PATH
        except Exception as exc:
            return ("MISSING", "fdkaac + HE-AAC", f"could not import FDKAAC_PATH: {exc}")

        if not Path(FDKAAC_PATH).is_file():
            return ("MISSING", "fdkaac + HE-AAC", f"{FDKAAC_PATH} does not exist -- see deploy/build_fdkaac.sh")
        if not os.access(FDKAAC_PATH, os.X_OK):
            return ("MISSING", "fdkaac + HE-AAC", f"{FDKAAC_PATH} exists but is not executable")

        check_script = Path(__file__).resolve().parents[3] / "deploy" / "check_he_aac.sh"
        if not check_script.is_file():
            return ("DEGRADED", "fdkaac + HE-AAC", f"{FDKAAC_PATH} present, but deploy/check_he_aac.sh not found to verify LC/HE/HEv2")
        try:
            result = subprocess.run(
                ["bash", str(check_script), FDKAAC_PATH],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            return ("DEGRADED", "fdkaac + HE-AAC", f"{FDKAAC_PATH} present, but validation could not run: {exc}")
        if result.returncode == 0:
            return ("PASS", "fdkaac + HE-AAC", "LC / HE / HEv2 supported")
        return ("MISSING", "fdkaac + HE-AAC", "encode/decode validation failed -- see deploy/check_he_aac.sh output")

    def _check_directories(self):
        results = []
        opt_root = Path("/opt/isadoraair")
        if opt_root.is_dir() and (opt_root / "manage.py").is_file():
            kind = "symlink" if opt_root.is_symlink() else "directory"
            results.append(("PASS", "/opt/isadoraair (canonical app root)", f"{kind}, manage.py present"))
        else:
            results.append(("MISSING", "/opt/isadoraair (canonical app root)", "missing, or manage.py not found under it"))

        library_root = Path(os.environ.get("LIBRARY_ROOT", "/srv/isadoraair/music"))
        if library_root.is_dir():
            results.append(("PASS", "Library root", str(library_root)))
        else:
            results.append(("MISSING", "Library root", f"{library_root} does not exist"))

        run_dir = Path("/run/isadoraair")
        if run_dir.is_dir():
            results.append(("PASS", "/run/isadoraair", "present"))
        else:
            results.append(("DEGRADED", "/run/isadoraair", "missing -- created by systemd-tmpfiles at boot (deploy/isadoraair-tmpfiles.conf); absence is normal if services haven't started yet"))
        return results

    def _check_kokoro(self):
        results = []
        kokoro_root = Path(os.environ.get("KOKORO_ROOT", "/home/jreed/kokoro"))
        binary = kokoro_root / "bin" / "kokoro_synth"
        model = kokoro_root / "kokoro-v1.0.onnx"
        voices = kokoro_root / "voices-v1.0.bin"

        if not binary.exists():
            results.append(("OPTIONAL", "Kokoro runtime", f"{binary} not found -- see docs/KOKORO_PROVENANCE.md"))
            return results
        if not os.access(binary, os.X_OK):
            results.append(("MISSING", "Kokoro runtime", f"{binary} exists but is not executable"))
            return results
        results.append(("PASS", "Kokoro runtime", str(binary)))

        if model.is_file() and voices.is_file():
            results.append(("PASS", "Kokoro configured model", f"{model.name} + {voices.name} present"))
        else:
            missing = [p.name for p in (model, voices) if not p.is_file()]
            results.append(("MISSING", "Kokoro configured model", f"missing: {', '.join(missing)}"))
        return results

    def _check_piper(self):
        results = []
        weather_root = Path(os.environ.get("WEATHER_ROOT", "/home/jreed/weather-ingest"))
        binary = weather_root / "venv" / "bin" / "piper"
        piper_models_dir = Path(os.environ.get("PIPER_MODELS_DIR", "/home/jreed/piper"))

        if not binary.exists():
            results.append(("OPTIONAL", "Piper runtime", f"{binary} not found -- see docs/PIPER_PROVENANCE.md"))
            return results
        if not os.access(binary, os.X_OK):
            results.append(("MISSING", "Piper runtime", f"{binary} exists but is not executable"))
            return results
        results.append(("PASS", "Piper runtime", str(binary)))

        if piper_models_dir.is_dir():
            voice_count = len(list(piper_models_dir.glob("*.onnx")))
            if voice_count:
                results.append(("PASS", "Piper voices found", str(voice_count)))
            else:
                results.append(("OPTIONAL", "Piper voices found", f"0 -- {piper_models_dir} has no .onnx files"))
        else:
            results.append(("OPTIONAL", "Piper voices found", f"{piper_models_dir} does not exist"))
        return results
