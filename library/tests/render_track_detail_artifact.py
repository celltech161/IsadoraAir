"""QA helper (not collected by default test discovery): render Track Detail and its cue-marker and
waveform API payloads for a representative Track, inside the guarded test sandbox, and print them
between markers so a browser outside the sandbox can inspect the real page with the APIs stubbed
from these payloads -- no server, database or station data involved.

    run_with_validation_service.sh <python> manage.py test library.tests.render_track_detail_artifact
"""
import json

from library.models import Track
from library.services import cue_markers, waveform_generation as wg
from library.tests.test_waveform_integrity_package_b import _Base


class RenderArtifact(_Base):
    def test_render(self):
        from django.test import Client

        from library.services import waveform_cache
        client = Client()
        client.force_login(self.user)
        track = self.track(name="Representative Song.flac")
        wg.refresh(track.pk)                                               # trusted v2 cache
        current = waveform_cache.current_audio_revision(Track.objects.get(pk=track.pk))
        edit = cue_markers.operator_edit
        edit(track.pk, expected_revision=0, user=self.user, values={"sweep_start_seconds": 0.6})
        cue_markers.apply_calculation(track.pk, {"cue_out_seconds": 2.9}, basis_revision=1, media_replaced=True,
                                      audio_revision="f" * 64, source=cue_markers.SOURCE_GENERATED)  # sweep -> review
        edit(track.pk, expected_revision=2, user=self.user, values={"next_start_seconds": 2.4})      # unverified
        edit(track.pk, expected_revision=3, user=self.user, values={"intro_until_seconds": 0.9},
             audio_revision=current)                                                                  # approved
        edit(track.pk, expected_revision=4, user=self.user, values={"outro_starts_seconds": 2.2},
             audio_revision="e" * 64)                                                                 # other audio
        cue_markers.apply_calculation(track.pk, {"hook_in_seconds": 1.0, "hook_out_seconds": 1.6}, basis_revision=5,
                                      source=cue_markers.SOURCE_GENERATED)                           # auto
        edit(track.pk, expected_revision=6, user=self.user, approve=["hook_out_seconds"])
        edit(track.pk, expected_revision=7, user=self.user, unlock=["hook_out_seconds"])              # unlocked
        page = client.get(f"/track/{track.pk}/").content.decode()
        state = client.get(f"/api/tracks/{track.pk}/cue-markers/").content.decode()
        wave = client.get(f"/api/waveform/{track.pk}/").content.decode()
        for name, blob in (("PAGE", page), ("STATE", state), ("WAVEFORM", wave)):
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
        self.assertIn("cuePointsSection", page)
        json.loads(state)
