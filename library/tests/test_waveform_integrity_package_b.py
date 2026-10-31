"""r0123 Package B: decoded-audio identity, trusted version-2 waveform caches, the shared atomic
cache writer, Refresh Waveform Only, approvals across audio revisions and trusted Repick.

Real audio is generated with ffmpeg into per-test temporary directories, and WAVEFORMS_DIR points
at one of them; no station media, waveform, database or service is touched (the suite runs only
inside the test sandbox)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from library.management.commands import analyze_tracks, check_waveform_integrity
from library.models import Artist, Category, CategoryKind, PlayEvent, Track
from library.services import audio_identity, cue_markers, waveform_cache, waveform_generation as wg

M = cue_markers.MARKERS
#: quiet 0-0.5 s, a tone to 2.5 s, quiet to 3 s: Cue In ~0.5 s and Next Start ~2.5 s by detection
SHAPED = "if(between(t,0.5,2.5),0.5*sin(2*PI*{f}*t),0)"


def _audio(path, *, freq=440, seconds=3.0, channels=2, rate=44100, shaped=True):
    expr = SHAPED.format(f=freq) if shaped else f"0.5*sin(2*PI*{freq}*t)"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc=exprs='{expr}':d={seconds}:s={rate}", "-ac", str(channels), str(path)],
                   check=True, stdin=subprocess.DEVNULL)
    return Path(path)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _row(track_id):
    return cue_markers.snapshot(track_id)


class _Base(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.media = self.dir / "media"
        self.media.mkdir()
        self.waves = self.dir / "waveforms"
        self.waves.mkdir()
        settings = override_settings(WAVEFORMS_DIR=str(self.waves), SECURE_SSL_REDIRECT=False)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = User.objects.create_superuser("wave-op", "wave-op@example.invalid", "pw")
        self.artist, _ = Artist.get_or_create_ci("Waveform Test Artist")
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="WAVETEST", name="Wave Test", kind=kind)

    def track(self, name="song.flac", *, analyzed=True, **audio):
        path = _audio(self.media / name, **audio)
        track = Track.objects.create(filepath=str(path), filename=path.name, title=name, artist=self.artist,
                                     duration_seconds=audio.get("seconds", 3.0), category=self.category,
                                     format=path.suffix.lstrip("."), ready2air=True)
        if analyzed:
            legacy = {"track_id": track.pk, "samples_left": [1, 2], "samples_right": [1, 2],
                      "envelope_times": [0.5, 1.0], "envelope_db": [-30.0, -30.0],
                      "analysis_duration": audio.get("seconds", 3.0), "next_start": None, "cue_in_seconds": 0.0}
            (self.waves / f"{track.pk}.json").write_text(json.dumps(legacy))
            Track.objects.filter(pk=track.pk).update(waveform_path=str(self.waves / f"{track.pk}.json"))
        return Track.objects.get(pk=track.pk)

    def cache(self, track):
        return self.waves / f"{track.pk}.json"

    def verdict(self, track):
        return waveform_cache.validate(Track.objects.get(pk=track.pk))

    def full_row(self, track):
        row = Track.objects.filter(pk=track.pk).values().get()
        row["additional_categories"] = sorted(Track.objects.get(pk=track.pk).additional_categories.values_list(
            "pk", flat=True))
        return row


# ------------------------------------------------------------------ audio identity

class AudioIdentityTests(_Base):
    def test_tag_and_container_changes_keep_the_identity_and_audio_changes_do_not(self):
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3, TIT2
        flac = _audio(self.media / "a.flac")
        before = audio_identity.compute(flac)
        tags = FLAC(str(flac))
        tags["title"] = "a much longer title " * 50
        tags.save()
        self.assertTrue(audio_identity.same_audio(before, audio_identity.compute(flac)))
        mp3 = self.media / "a.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(flac), "-c:a", "libmp3lame",
                        str(mp3)], check=True, stdin=subprocess.DEVNULL)
        mp3_before = audio_identity.compute(mp3)
        id3 = ID3()
        id3.add(TIT2(encoding=3, text="Retitled"))
        id3.save(str(mp3))
        self.assertTrue(audio_identity.same_audio(mp3_before, audio_identity.compute(mp3)))
        _audio(flac, freq=441)                                   # same path, different sound
        self.assertFalse(audio_identity.same_audio(before, audio_identity.compute(flac)))

    def test_a_same_size_replacement_with_different_audio_is_detected(self):
        one, two = _audio(self.media / "one.wav", freq=440), _audio(self.media / "two.wav", freq=550)
        self.assertEqual(os.path.getsize(one), os.path.getsize(two))
        self.assertFalse(audio_identity.same_audio(audio_identity.compute(one), audio_identity.compute(two)))

    def test_mono_sources_are_identified_in_the_canonical_stereo_form_and_lossless_conversion_is_equal(self):
        wav = _audio(self.media / "mono.wav", channels=1)
        flac = self.media / "mono.flac"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), str(flac)], check=True,
                       stdin=subprocess.DEVNULL)
        identity = audio_identity.compute(wav)
        self.assertEqual((identity["channels"], identity["sample_format"], identity["sample_rate"]),
                         (2, "s32le", 44100))
        self.assertEqual(identity["frames"], 3 * 44100)
        self.assertTrue(audio_identity.same_audio(identity, audio_identity.compute(flac)))

    def test_identity_is_deterministic(self):
        path = _audio(self.media / "d.flac")
        self.assertEqual(audio_identity.compute(path)["sha256"], audio_identity.compute(path)["sha256"])

    def test_invalid_or_truncated_audio_has_no_identity(self):
        good = _audio(self.media / "g.flac", shaped=False, seconds=5.0)
        truncated = self.media / "t.flac"
        truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 3])
        junk = self.media / "j.flac"
        junk.write_bytes(b"not audio at all " * 100)
        for path in (truncated, junk, self.media / "absent.flac"):
            with self.subTest(path.name):
                with self.assertRaises(audio_identity.IdentityUnavailable):
                    audio_identity.compute(path)


# ------------------------------------------------------------------ cache format

class CacheFormatTests(_Base):
    def refreshed(self, **audio):
        track = self.track(**audio)
        result = wg.refresh(track.pk)
        self.assertEqual(result.status, wg.REPLACED, result.detail)
        return Track.objects.get(pk=track.pk), json.loads(self.cache(track).read_text())

    def check(self, track, payload):
        path = self.dir / "variant.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return waveform_cache.validate(track, path)

    def test_a_refreshed_cache_is_a_valid_trusted_version_2_cache(self):
        track, payload = self.refreshed()
        self.assertEqual((payload["schema"], payload["schema_version"], payload["track_id"]),
                         (waveform_cache.SCHEMA, 2, track.pk))
        for key in ("audio_identity", "source", "analysis", "generation", "markers", "marker_revision",
                    "samples_left", "samples_right", "envelope_times", "envelope_db", "cue_in_seconds", "next_start"):
            self.assertIn(key, payload)
        self.assertEqual(self.verdict(track).status, waveform_cache.TRUSTED)
        self.assertEqual(check_waveform_integrity.inspect_file(self.cache(track), track.pk, track.duration_seconds),
                         [])                                       # the incident checker accepts it as is

    def test_every_invalid_variant_is_refused_trust(self):
        track, good = self.refreshed()
        variants = {
            "wrong track": (good | {"track_id": track.pk + 1}, waveform_cache.WRONG_TRACK),
            "unsupported version": (good | {"schema_version": 3}, waveform_cache.UNSUPPORTED),
            "unsupported schema": (good | {"schema": "other"}, waveform_cache.UNSUPPORTED),
            "identity of other audio": (good | {"audio_identity": good["audio_identity"] | {"sha256": "f" * 64},
                                                "source": good["source"] | {"ctime_ns": 1}},
                                        waveform_cache.UNVERIFIED_AUDIO),
            "unknown identity algorithm": (good | {"audio_identity": good["audio_identity"] | {"algorithm": "x"}},
                                           waveform_cache.UNVERIFIED_AUDIO),
            "missing identity field": (good | {"audio_identity": {"sha256": "a" * 64}}, waveform_cache.MALFORMED),
            "invalid source": (good | {"source": {"path": 7}}, waveform_cache.MALFORMED),
            "invalid analysis": (good | {"analysis": {"sample_rate": 0}}, waveform_cache.MALFORMED),
            "non-integer sample": (good | {"samples_left": good["samples_left"][:-1] + [1.5]},
                                   waveform_cache.MALFORMED),
            "non-finite envelope": (json.dumps(good).replace(f'"envelope_db": [{good["envelope_db"][0]}',
                                                             '"envelope_db": [Infinity', 1), waveform_cache.MALFORMED),
            "unequal stereo": (good | {"samples_right": good["samples_right"][:-1]}, waveform_cache.MALFORMED),
            "empty stereo": (good | {"samples_left": [], "samples_right": []}, waveform_cache.INCOMPLETE),
            "envelope length": (good | {"envelope_db": good["envelope_db"][:-1]}, waveform_cache.MALFORMED),
            "envelope order": (good | {"envelope_times": list(reversed(good["envelope_times"]))},
                               waveform_cache.MALFORMED),
            "identity duration": (good | {"audio_identity": good["audio_identity"] | {"frames": 44100 * 9}},
                                  waveform_cache.INCONSISTENT),
            "track duration": (good, None),
            "truncated": (json.dumps(good)[:300], waveform_cache.MALFORMED),
        }
        for label, (payload, status) in variants.items():
            with self.subTest(label):
                target = track
                if label == "track duration":
                    Track.objects.filter(pk=track.pk).update(duration_seconds=60.0)
                    target, status = Track.objects.get(pk=track.pk), waveform_cache.INCONSISTENT
                verdict = self.check(target, payload)
                self.assertEqual(verdict.status, status, verdict.reason)
                self.assertFalse(verdict.trusted)

    def test_a_legitimately_short_recording_is_trusted(self):
        track, _ = self.refreshed(name="short.flac", seconds=0.4, shaped=False)
        self.assertTrue(self.verdict(track).trusted)

    def test_a_stale_marker_snapshot_keeps_the_waveform_trusted_and_readers_get_database_markers(self):
        track, _ = self.refreshed()
        cue_markers.operator_edit(track.pk, expected_revision=track.cue_marker_revision, user=self.user,
                                  values={"hook_in_seconds": 1.0})
        self.assertEqual(self.verdict(track).status, waveform_cache.TRUSTED_STALE_MARKERS)
        self.assertTrue(self.verdict(track).trusted)
        client = Client()
        client.force_login(self.user)
        body = client.get(f"/api/waveform/{track.pk}/").json()
        self.assertEqual((body["markers"]["hook_in_seconds"], body["marker_revision"]), (1.0, 1))
        self.assertEqual((body["trust"]["trusted"], body["trust"]["markers_current"]), (True, False))
        self.assertNotIn("source", body)

    def test_older_or_unverified_audio_is_distinguished_from_stale_markers(self):
        track, _ = self.refreshed()
        os.utime(track.filepath, ns=(time.time_ns(), time.time_ns()))      # touched: needs re-verification
        self.assertEqual(self.verdict(track).status, waveform_cache.UNVERIFIED_AUDIO)
        legacy = self.track(name="legacy.flac")
        verdict = self.verdict(legacy)
        self.assertEqual((verdict.status, verdict.displayable, verdict.trusted), (waveform_cache.LEGACY, True, False))


# ------------------------------------------------------------------ shared writer

class PromotionTests(_Base):
    def payload(self, track, *, started_ns=None, **changes):
        times = [0.05 * (i + 0.5) for i in range(40)]
        payload = waveform_cache.build_payload(
            track.pk, identity=audio_identity.compute(track.filepath), source=audio_identity.stat_source(track.filepath),
            analysis={"sample_rate": 4410, "window_seconds": 0.05, "waveform_points": 1000}, writer="test",
            started_ns=started_ns or time.time_ns(), analysis_duration=times[-1] + 1.0, times=times,
            envelope_db=[-30.0] * 40, samples_left=[10] * 40, samples_right=[12] * 40)
        payload.update(changes)
        return payload

    def leftovers(self):
        return sorted(p.name for p in self.waves.iterdir() if p.name not in (waveform_cache.LOCK_DIRNAME,)
                      and not p.name[:-5].isdigit())

    def test_successful_promotion_is_atomic_validated_and_leaves_nothing_behind(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        promotion = waveform_cache.promote(track.pk, self.payload(track), self.waves, keep_previous=True)
        self.assertEqual(promotion.previous.read_bytes(), old)              # same-filesystem rollback copy
        promotion.discard()
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(json.loads(self.cache(track).read_text())["markers"], {n: getattr(track, n) for n in M})

    def test_failures_at_each_step_keep_the_previous_cache(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        steps = {
            "staging": patch.object(waveform_cache, "_stage", side_effect=OSError(28, "No space left on device")),
            "validation": None,
            "replacement": patch("library.services.waveform_cache.os.replace", side_effect=OSError(5, "I/O error")),
        }
        for step, failure in steps.items():
            with self.subTest(step):
                if failure is None:
                    with self.assertRaises(waveform_cache.PromotionRefused):
                        waveform_cache.promote(track.pk, self.payload(track, samples_left=[]), self.waves)
                else:
                    with failure, self.assertRaises(OSError):
                        waveform_cache.promote(track.pk, self.payload(track), self.waves)
                self.assertEqual(self.cache(track).read_bytes(), old)
                self.assertEqual(self.leftovers(), [])

    def test_an_interrupted_writer_never_damages_the_cache_and_its_lock_dies_with_it(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        (self.waves / f".{track.pk}.json.999.dead.tmp").write_text("{\"partial")   # a crashed writer's debris
        lock = self.waves / waveform_cache.LOCK_DIRNAME / f"{track.pk}.lock"
        lock.parent.mkdir(exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl, os, sys, time\n"
             f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('locked', flush=True)\ntime.sleep(60)\n"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with self.assertRaises(waveform_cache.WaveformBusy):                  # bounded, never indefinite
            waveform_cache.promote(track.pk, self.payload(track), self.waves, lock_timeout=0.3)
        self.assertEqual(self.cache(track).read_bytes(), old)
        holder.kill()
        holder.wait()
        holder.stdout.close()
        waveform_cache.promote(track.pk, self.payload(track), self.waves, lock_timeout=2)
        self.assertEqual(self.verdict(track).status, waveform_cache.TRUSTED)

    def test_a_stale_writer_cannot_replace_newer_data_or_a_changed_source(self):
        track = self.track()
        waveform_cache.promote(track.pk, self.payload(track, started_ns=2_000), self.waves)
        newer = self.cache(track).read_bytes()
        with self.assertRaises(waveform_cache.PromotionRefused) as refused:
            waveform_cache.promote(track.pk, self.payload(track, started_ns=1_000), self.waves)
        self.assertEqual((refused.exception.code, self.cache(track).read_bytes()), ("superseded", newer))
        witness = audio_identity.stat_source(track.filepath)
        _audio(Path(track.filepath), freq=660)
        with self.assertRaises(waveform_cache.PromotionRefused) as refused:
            waveform_cache.promote(track.pk, self.payload(track, started_ns=3_000), self.waves,
                                   expected_source=witness)
        self.assertEqual((refused.exception.code, self.cache(track).read_bytes()), ("source_changed", newer))

    def test_locks_are_station_wide_per_track_and_work_read_only(self):
        track = self.track()
        scratch = self.dir / "scratch"
        scratch.mkdir()
        waveform_cache.promote(track.pk, self.payload(track), scratch)          # a publisher's private analysis
        self.assertEqual(sorted(os.listdir(scratch)), [f"{track.pk}.json"])  # no lock debris in scratch
        lock = self.waves / waveform_cache.LOCK_DIRNAME / f"{track.pk}.lock"
        self.assertTrue(lock.exists())
        os.chmod(lock, 0o444)                                                 # e.g. created by another service user
        with waveform_cache.track_lock(scratch, track.pk):
            with self.assertRaises(waveform_cache.WaveformBusy):              # the same Track, any directory
                waveform_cache.promote(track.pk, self.payload(track), self.waves, lock_timeout=0.2)
        waveform_cache.promote(track.pk, self.payload(track), self.waves)

    def test_undo_restores_exactly_the_previous_state(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        waveform_cache.promote(track.pk, self.payload(track), self.waves, keep_previous=True).undo()
        self.assertEqual(self.cache(track).read_bytes(), old)
        fresh = self.track(name="fresh.flac", analyzed=False)
        waveform_cache.promote(fresh.pk, self.payload(fresh), self.waves, keep_previous=True).undo()
        self.assertFalse(self.cache(fresh).exists())

    def _staged_cache(self, track, payload, name):
        scratch = self.dir / name
        scratch.mkdir()
        return waveform_cache.promote(track.pk, payload, scratch).path

    def _pending_writer(self, track, staged, *, attempted, finished, errors):
        try:
            attempted.set()
            waveform_cache.install_file(track.pk, staged, self.waves, lock_timeout=2)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def test_rollback_capable_promotions_fence_pending_writers_for_commit_and_rollback(self):
        for finalize in ("discard", "undo"):
            with self.subTest(finalize=finalize):
                track = self.track(name=f"{finalize}.flac")
                old = self.cache(track).read_bytes()
                staged = self._staged_cache(
                    track, self.payload(track, started_ns=2_000), f"staged-{finalize}")
                first = waveform_cache.promote(
                    track.pk, self.payload(track, started_ns=1_000), self.waves, keep_previous=True)
                first_bytes = self.cache(track).read_bytes()
                attempted, finished, errors = threading.Event(), threading.Event(), []
                worker = threading.Thread(
                    target=self._pending_writer,
                    args=(track, staged),
                    kwargs={"attempted": attempted, "finished": finished, "errors": errors},
                    daemon=True,
                )
                worker.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(finished.is_set())
                getattr(first, finalize)()
                self.assertTrue(finished.wait(3))
                worker.join()
                self.assertEqual(errors, [])
                self.assertNotEqual(self.cache(track).read_bytes(), old)
                self.assertNotEqual(self.cache(track).read_bytes(), first_bytes)

    def test_pending_writer_cannot_steal_or_delete_an_earlier_unique_rollback(self):
        track = self.track()
        staged = self._staged_cache(track, self.payload(track, started_ns=2_000), "staged-pending")
        first = waveform_cache.promote(track.pk, self.payload(track, started_ns=1_000), self.waves,
                                       keep_previous=True)
        rollback = first.previous
        attempted, finished, errors = threading.Event(), threading.Event(), []
        worker = threading.Thread(
            target=self._pending_writer,
            args=(track, staged),
            kwargs={"attempted": attempted, "finished": finished, "errors": errors},
            daemon=True,
        )
        worker.start()
        self.assertTrue(attempted.wait(1))
        self.assertTrue(rollback.is_file())
        first.discard()
        self.assertTrue(finished.wait(3))
        worker.join()
        self.assertEqual(errors, [])
        self.assertFalse(rollback.exists())

    def test_directory_fsync_failure_after_replace_restores_the_previous_cache(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        real_fsync = waveform_cache._fsync_dir
        calls = []

        def fail_once(directory):
            calls.append(directory)
            if len(calls) == 1:
                raise OSError(5, "injected directory fsync failure")
            return real_fsync(directory)

        with patch.object(waveform_cache, "_fsync_dir", side_effect=fail_once):
            with self.assertRaises(OSError):
                waveform_cache.promote(track.pk, self.payload(track), self.waves)
        self.assertEqual(self.cache(track).read_bytes(), old)
        self.assertEqual(self.leftovers(), [])

    def test_lost_ownership_is_reported_without_deleting_the_promotions_rollback(self):
        track = self.track()
        promotion = waveform_cache.promote(track.pk, self.payload(track), self.waves, keep_previous=True)
        rollback = promotion.previous
        self.cache(track).write_bytes(b"an out-of-contract writer bypassed the lock")
        with self.assertRaises(waveform_cache.PromotionRefused) as refused:
            promotion.discard()
        self.assertEqual(refused.exception.code, "ownership_lost")
        self.assertTrue(rollback.is_file())
        rollback.unlink()


# ------------------------------------------------------------------ Refresh Waveform Only

class RefreshTests(_Base):
    def test_refresh_changes_no_track_column_and_no_source_byte_tag_or_format(self):
        from mutagen.flac import FLAC
        track = self.track()
        tags = FLAC(track.filepath)
        tags["title"] = "Keep My Tag"
        tags.save()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user,
                                  values={"intro_until_seconds": 1.0, "hook_in_seconds": 1.5, "hook_out_seconds": 2.0})
        cue_markers.operator_edit(track.pk, expected_revision=1, user=self.user, unlock=["hook_out_seconds"])
        track.additional_categories.add(self.category)
        PlayEvent_count = PlayEvent.objects.count()
        before_row, before_sha = self.full_row(track), _sha(track.filepath)
        before_stat = os.stat(track.filepath)
        before_tags = dict(FLAC(track.filepath).tags)
        result = wg.refresh(track.pk)
        self.assertEqual(result.status, wg.REPLACED, result.detail)
        self.assertEqual(self.full_row(track), before_row)                  # the WHOLE row, not just markers
        self.assertEqual(_sha(track.filepath), before_sha)
        after_stat = os.stat(track.filepath)
        self.assertEqual((after_stat.st_mtime_ns, after_stat.st_ino), (before_stat.st_mtime_ns, before_stat.st_ino))
        self.assertEqual(dict(FLAC(track.filepath).tags), before_tags)
        self.assertEqual(PlayEvent.objects.count(), PlayEvent_count)
        self.assertEqual(self.verdict(track).status, waveform_cache.TRUSTED)

    def test_wav_and_aif_sources_stay_unconverted(self):
        for name in ("plain.wav", "plain.aif"):
            with self.subTest(name):
                track = self.track(name=name)
                before = (self.full_row(track), _sha(track.filepath), sorted(os.listdir(self.media)))
                result = wg.refresh(track.pk)
                self.assertEqual(result.status, wg.REPLACED, result.detail)
                self.assertEqual((self.full_row(track), _sha(track.filepath), sorted(os.listdir(self.media))), before)
                self.assertTrue(Path(track.filepath).exists())
                self.assertTrue(self.verdict(track).trusted)

    def test_a_failed_refresh_keeps_the_previous_cache(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        Path(track.filepath).write_bytes(b"no longer audio " * 200)
        self.assertEqual(wg.refresh(track.pk).status, wg.DECODE_FAILED)
        Path(track.filepath).unlink()
        self.assertEqual(wg.refresh(track.pk).status, wg.SOURCE_MISSING)
        self.assertEqual(self.cache(track).read_bytes(), old)
        never = self.track(name="never.flac", analyzed=False)
        self.assertEqual(wg.refresh(never.pk).status, wg.NOT_ANALYZED)
        self.assertFalse(self.cache(never).exists())

    def during_measure(self, action):
        real = wg.measure

        def measure(path, settings, *limits):            # limits: r0123 B3's bounded decoding
            values = real(path, settings, *limits)
            action()
            return values
        return patch.object(wg, "measure", side_effect=measure)

    def test_a_marker_edit_during_refresh_is_kept_and_the_cache_snapshot_is_current(self):
        track = self.track()
        with self.during_measure(lambda: cue_markers.operator_edit(
                track.pk, expected_revision=0, user=self.user, values={"hook_in_seconds": 0.9})):
            result = wg.refresh(track.pk)
        self.assertEqual(result.status, wg.REPLACED, result.detail)
        row = _row(track.pk)
        self.assertEqual((row["hook_in_seconds"], row["cue_marker_revision"]), (0.9, 1))
        payload = json.loads(self.cache(track).read_text())
        self.assertEqual((payload["markers"]["hook_in_seconds"], payload["marker_revision"]), (0.9, 1))
        self.assertEqual(self.verdict(track).status, waveform_cache.TRUSTED)

    def test_a_source_replaced_during_refresh_aborts_and_keeps_the_previous_cache(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        with self.during_measure(lambda: _audio(Path(track.filepath), freq=880)):
            result = wg.refresh(track.pk)
        self.assertEqual((result.status, result.conflict, result.previous_cache_retained),
                         (wg.SOURCE_CHANGED, True, True))
        self.assertEqual(self.cache(track).read_bytes(), old)

    def test_concurrent_refreshes_and_a_newer_analysis_never_promote_stale_data(self):
        track = self.track()
        fd = waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock")
        try:
            self.assertEqual(wg.refresh(track.pk).status, wg.BUSY)            # a second refresh of the Track
        finally:
            os.close(fd)

        def analyze_meanwhile():
            row = (track.pk, track.filepath, track.filename, 3.0, track.title, "A", "")
            cfg = (4410, 0.05, 1000, -26.0, -45.0, 0.1)
            with patch.object(analyze_tracks, "emit_event"):
                self.assertTrue(analyze_tracks.analyze_one_track(row, cfg, self.waves, force=True))
        with self.during_measure(analyze_meanwhile):
            result = wg.refresh(track.pk)
        self.assertEqual((result.status, result.conflict), (wg.SUPERSEDED, True))
        self.assertEqual(json.loads(self.cache(track).read_text())["generation"]["writer"], "analysis")
        self.assertTrue(self.verdict(track).trusted)

    def test_the_endpoint_contract_capability_and_bounded_long_form_path(self):
        track = self.track()
        client = Client()
        client.force_login(User.objects.create_user("listener", password="pw"))
        self.assertEqual(client.post(f"/api/tracks/{track.pk}/refresh-waveform/").status_code, 403)
        client.force_login(self.user)
        response = client.post(f"/api/tracks/{track.pk}/refresh-waveform/")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["track_id"], body["status"], body["replaced"], body["previous_cache_retained"]),
                         (track.pk, "replaced", True, False))
        self.assertEqual((body["old"]["status"], body["new"]["status"], body["new"]["trusted"]),
                         ("legacy", "trusted", True))
        self.assertEqual(client.get(f"/api/tracks/{track.pk}/refresh-waveform/").status_code, 405)

        long_form = self.track(name="programme.flac")
        Track.objects.filter(pk=long_form.pk).update(duration_seconds=wg.SYNC_MAX_SECONDS + 1)
        with patch.object(wg.subprocess, "Popen") as popen:
            response = client.post(f"/api/tracks/{long_form.pk}/refresh-waveform/")
        self.assertEqual(response.status_code, 202, response.content)
        command = popen.call_args.args[0]
        self.assertEqual(command[2:5], ["refresh_waveform", str(long_form.pk), "--background"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 3)   # operation, slot and request locks (B2)
        slot = waveform_cache.try_operation_lock(self.waves, wg.BACKGROUND_SLOT_LOCK)   # one long refresh at a time
        try:
            with patch.object(wg.subprocess, "Popen") as popen:
                self.assertEqual(client.post(f"/api/tracks/{long_form.pk}/refresh-waveform/").status_code, 409)
            popen.assert_not_called()
        finally:
            os.close(slot)

    def test_the_background_command_runs_one_track_and_releases_its_locks(self):
        from django.core.management import call_command
        track = self.track()
        operation = waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock")
        slot = waveform_cache.try_operation_lock(self.waves, wg.BACKGROUND_SLOT_LOCK)
        from io import StringIO
        with patch("os.nice"):
            call_command("refresh_waveform", str(track.pk), "--background", "--operation-lock-fd", str(operation),
                         "--slot-lock-fd", str(slot), stdout=StringIO())
        self.assertTrue(self.verdict(track).trusted)
        os.close(waveform_cache.try_operation_lock(self.waves, wg.BACKGROUND_SLOT_LOCK))     # released
        os.close(waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock"))


# ------------------------------------------------------------------ analysis

class AnalyzerTests(_Base):
    CFG = (4410, 0.05, 1000, -26.0, -45.0, 0.1)

    def analyze(self, track, **kwargs):
        row = (track.pk, track.filepath, track.filename, track.duration_seconds, track.title, "A", "")
        with patch.object(analyze_tracks, "emit_event"):
            return analyze_tracks.analyze_one_track(row, self.CFG, self.waves, force=True, **kwargs)

    def test_analysis_of_current_media_produces_a_trusted_cache_and_audio_bound_markers(self):
        track = self.track(analyzed=False)
        self.assertTrue(self.analyze(track))
        track = Track.objects.get(pk=track.pk)
        self.assertEqual(self.verdict(track).status, waveform_cache.TRUSTED)
        revision = audio_identity.compute(track.filepath)["sha256"]
        self.assertEqual(track.cue_marker_state["next_start_seconds"]["audio_revision"], revision)
        self.assertAlmostEqual(track.next_start_seconds, 2.5, delta=0.1)

    def test_a_source_changed_during_analysis_applies_nothing(self):
        track = self.track(analyzed=False)
        real = analyze_tracks.decode_audio_to_pcm

        def decode_then_replace(path, rate):
            pcm = real(path, rate)
            _audio(Path(track.filepath), freq=700, seconds=3.5)
            return pcm
        with patch.object(analyze_tracks, "decode_audio_to_pcm", side_effect=decode_then_replace):
            outcome = self.analyze(track)
        self.assertEqual((bool(outcome), outcome.reason), (False, "source changed during analysis"))
        self.assertEqual(_row(track.pk)["cue_marker_revision"], 0)
        self.assertFalse(self.cache(track).exists())


# ------------------------------------------------------------------ approvals across audio revisions

class ApprovalRevisionTests(_Base):
    def approved(self):
        track = self.track()
        wg.refresh(track.pk)
        track = Track.objects.get(pk=track.pk)
        revision = waveform_cache.current_audio_revision(track)
        self.assertIsNotNone(revision)
        result = cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, audio_revision=revision,
                                           values={"next_start_seconds": 2.0, "hook_in_seconds": 1.0})
        self.assertTrue(result.ok)
        return Track.objects.get(pk=track.pk), revision

    def test_an_edit_records_the_audio_it_approves(self):
        track, revision = self.approved()
        self.assertEqual(track.cue_marker_state["hook_in_seconds"]["audio_revision"], revision)
        review = cue_markers.review(_row(track.pk), revision)
        self.assertEqual(review["hook_in_seconds"]["audio"], "current")
        self.assertEqual(cue_markers.review(_row(track.pk), "e" * 64)["hook_in_seconds"]["audio"], "other")

    def test_new_audio_retires_approvals_durably_without_touching_readiness(self):
        track, revision = self.approved()
        result = cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.4}, basis_revision=1,
                                               media_replaced=True, audio_revision="b" * 64)
        self.assertEqual((result.superseded, result.review), ({"next_start_seconds": "locked"}, ["hook_in_seconds"]))
        state = _row(track.pk)["cue_marker_state"]
        retired = state["next_start_seconds"]["previous_approval"]
        self.assertEqual((retired["value"], retired["approved_by"], retired["audio_revision"]),
                         (2.0, self.user.pk, revision))
        hook = state["hook_in_seconds"]
        self.assertEqual((hook["protection"], hook["previous_approval"]["value"]), ("review", 1.0))
        row = _row(track.pk)
        self.assertEqual(cue_markers.protection_of("hook_in_seconds", row), cue_markers.REVIEW)
        self.assertEqual(cue_markers.review(row, "b" * 64)["hook_in_seconds"]["audio"], "previous")
        self.assertTrue(Track.objects.get(pk=track.pk).ready2air)
        # A review marker is still protected from automatic analysis ...
        later = cue_markers.apply_calculation(track.pk, {"hook_in_seconds": 0.2}, basis_revision=2)
        self.assertEqual(later.preserved, {"hook_in_seconds": "review"})
        # ... and an operator's approval of the current audio resolves it.
        cue_markers.operator_edit(track.pk, expected_revision=2, user=self.user, approve=["hook_in_seconds"],
                                  audio_revision="b" * 64)
        hook = _row(track.pk)["cue_marker_state"]["hook_in_seconds"]
        # Package C: the retired approval stays as history after the operator resolves the review.
        self.assertEqual((hook["protection"], hook["audio_revision"], "review_reason" in hook,
                          hook["previous_approval"]["value"]), ("locked", "b" * 64, False, 1.0))

    def test_the_same_decoded_audio_republished_keeps_its_approvals(self):
        track, revision = self.approved()
        result = cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.4}, basis_revision=1,
                                               media_replaced=True, audio_revision=revision)
        self.assertEqual((result.superseded, result.review, result.preserved),
                         ({}, [], {"next_start_seconds": "locked"}))
        self.assertEqual(_row(track.pk)["next_start_seconds"], 2.0)

    def test_an_approval_of_unidentified_audio_is_never_assumed_to_match_new_media(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, values={"hook_in_seconds": 1.0})
        self.assertIsNone(_row(track.pk)["cue_marker_state"]["hook_in_seconds"]["audio_revision"])
        result = cue_markers.apply_calculation(track.pk, {"cue_in_seconds": 0.0}, basis_revision=1,
                                               source=cue_markers.SOURCE_GENERATED, media_replaced=True)
        self.assertTrue(result.ok)                                # generated-content publishing still works
        self.assertEqual(result.review, ["hook_in_seconds"])

    def test_equivalent_media_copy_needs_verified_identity(self):
        source, revision = self.approved()
        identity = audio_identity.compute(source.filepath)
        twin = self.track(name="twin.flac", analyzed=False)
        verified = cue_markers.copy_for_equivalent_media(source.pk, twin.pk, ("hook_in_seconds",),
                                                         source_identity=identity,
                                                         target_identity=audio_identity.compute(twin.filepath))
        self.assertTrue(verified.ok, verified.error)
        entry = _row(twin.pk)["cue_marker_state"]["hook_in_seconds"]
        self.assertEqual((entry["protection"], entry["equivalence"]["verified"]), ("locked", True))

        unverified = self.track(name="unverified.flac", analyzed=False)
        cue_markers.copy_for_equivalent_media(source.pk, unverified.pk, ("hook_in_seconds",))
        row = _row(unverified.pk)
        self.assertEqual(cue_markers.protection_of("hook_in_seconds", row), cue_markers.LEGACY)
        self.assertEqual(row["cue_marker_state"]["hook_in_seconds"]["previous_approval"]["value"], 1.0)

        other = self.track(name="other.flac", analyzed=False, freq=990)
        refused = cue_markers.copy_for_equivalent_media(source.pk, other.pk, ("hook_in_seconds",),
                                                        source_identity=identity,
                                                        target_identity=audio_identity.compute(other.filepath))
        self.assertEqual((refused.ok, _row(other.pk)["cue_marker_revision"]), (False, 0))
        self.assertFalse(cue_markers.copy_for_equivalent_media(source.pk, source.pk, ("hook_in_seconds",)).ok)


# ------------------------------------------------------------------ trusted Repick

class TrustedRepickTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def test_refresh_then_repick_recalculates_eligible_markers_and_keeps_protected_ones(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, unlock=["cue_in_seconds"],
                                  values={"next_start_seconds": 2.0})                     # locked by the operator
        self.assertEqual(self.client.post(f"/api/tracks/{track.pk}/repick-cue-points/").status_code, 409)  # legacy
        self.assertEqual(self.client.post(f"/api/tracks/{track.pk}/refresh-waveform/").status_code, 200)
        response = self.client.post(f"/api/tracks/{track.pk}/repick-cue-points/")
        self.assertEqual(response.status_code, 200, response.content)
        markers = response.json()["cue_markers"]
        self.assertEqual(markers["preserved"], {"next_start_seconds": "locked"})
        row = _row(track.pk)
        self.assertAlmostEqual(row["cue_in_seconds"], 0.5, delta=0.06)      # recalculated from the envelope
        self.assertEqual(row["next_start_seconds"], 2.0)                    # protected
        self.assertIsNotNone(row["cue_marker_state"]["cue_in_seconds"]["audio_revision"])

    def test_category_repick_uses_trusted_members_and_reports_the_rest(self):
        trusted = self.track(name="trusted.flac")
        cue_markers.operator_edit(trusted.pk, expected_revision=0, user=self.user, unlock=["cue_in_seconds"])
        wg.refresh(trusted.pk)
        legacy = self.track(name="legacy.flac")                          # Next Start NULL, legacy cache
        touched = self.track(name="touched.flac")
        wg.refresh(touched.pk)
        os.utime(touched.filepath, ns=(1, 1))
        before = {t.pk: _row(t.pk) for t in (legacy, touched)}
        body = self.client.post(f"/api/categories/{self.category.pk}/repick-cue-points/").json()
        self.assertEqual(body["repicked"], 1)
        self.assertEqual(body["skipped_by_reason"], {"legacy": [legacy.pk], "unverified_audio": [touched.pk]})
        self.assertAlmostEqual(_row(trusted.pk)["cue_in_seconds"], 0.5, delta=0.06)
        for pk, row in before.items():
            self.assertEqual(_row(pk), row)                              # Next Start NULL stays NULL, nothing queued

    def test_a_stale_revision_conflicts_safely(self):
        from library.views import _category_thresholds, _repick_track
        track = self.track()
        wg.refresh(track.pk)
        stale = Track.objects.get(pk=track.pk)
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, values={"hook_in_seconds": 1.0})
        outcome = _repick_track(stale, _category_thresholds(self.category), requested_by=self.user)
        self.assertEqual((outcome["ok"], outcome["status"]), (False, 409))
        self.assertEqual(_row(track.pk)["cue_marker_revision"], 1)
