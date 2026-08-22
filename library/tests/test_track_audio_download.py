"""Track source playback/download endpoint and Track Detail UI regressions."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.http import FileResponse
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.http import parse_header_parameters

from library.models import Artist, Track


User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class TrackAudioDownloadTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.source_dir = Path(self.tempdir.name) / "private-library" / "album"
        self.source_dir.mkdir(parents=True)
        self.artist = Artist.objects.create(name="Download Test Artist")
        self.staff = User.objects.create_superuser(
            "downloadstaff", "downloadstaff@example.invalid", "pw"
        )
        self.client.force_login(self.staff)

    def make_track(self, filename="Prince - 1999.flac", content=b"original-audio"):
        source = self.source_dir / filename
        source.write_bytes(content)
        track = Track.objects.create(
            filepath=str(source),
            filename=filename,
            format=source.suffix.lstrip(".").lower(),
            title=source.stem,
            artist=self.artist,
        )
        return track, source

    @staticmethod
    def response_bytes(response):
        return b"".join(response.streaming_content)

    def test_authorized_download_streams_original_bytes_as_attachment(self):
        original = b"\x00\x01original codec bytes\xff" * 100
        track, source = self.make_track(content=original)

        response = self.client.get(
            reverse("library:api-track-download", args=[track.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(response.streaming)
        self.assertEqual(self.response_bytes(response), original)
        disposition, params = parse_header_parameters(
            response["Content-Disposition"]
        )
        self.assertEqual(disposition, "attachment")
        self.assertEqual(params["filename"], source.name)

    def test_filename_is_safe_basename_and_never_leaks_source_directory(self):
        track, source = self.make_track(filename='Station "ID".wav')
        url = reverse("library:api-track-download", args=[track.pk])

        response = self.client.get(url)

        self.assertNotIn(str(source.parent), url)
        self.assertNotIn(str(source.parent), response["Content-Disposition"])
        disposition, params = parse_header_parameters(
            response["Content-Disposition"]
        )
        self.assertEqual(disposition, "attachment")
        self.assertEqual(params["filename"], source.name)
        self.response_bytes(response)

    def test_download_404s_for_unknown_track(self):
        response = self.client.get(
            reverse("library:api-track-download", args=[999999999])
        )
        self.assertEqual(response.status_code, 404)

    def test_download_404s_when_track_has_no_configured_file(self):
        track = Track.objects.create(
            filepath="",
            filename="",
            format="",
            title="No File",
            artist=self.artist,
        )
        response = self.client.get(
            reverse("library:api-track-download", args=[track.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_playback_and_download_share_missing_file_behavior(self):
        track, source = self.make_track()
        source.unlink()

        audio = self.client.get(reverse("library:api-track-audio", args=[track.pk]))
        download = self.client.get(
            reverse("library:api-track-download", args=[track.pk])
        )

        self.assertEqual(audio.status_code, 404)
        self.assertEqual(download.status_code, 404)
        self.assertEqual(audio.json(), {"error": "File not found on disk"})
        self.assertNotContains(audio, str(source), status_code=404)
        self.assertNotContains(download, str(source), status_code=404)

    def test_unreadable_file_fails_cleanly_without_leaking_path(self):
        track, source = self.make_track()

        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            audio = self.client.get(
                reverse("library:api-track-audio", args=[track.pk])
            )
            download = self.client.get(
                reverse("library:api-track-download", args=[track.pk])
            )

        self.assertEqual(audio.status_code, 404)
        self.assertEqual(download.status_code, 404)
        self.assertNotContains(audio, str(source), status_code=404)
        self.assertNotContains(download, str(source), status_code=404)

    def test_read_only_roles_cannot_see_or_directly_use_download(self):
        track, _source = self.make_track()
        download_url = reverse("library:api-track-download", args=[track.pk])

        for index, group_name in enumerate(("Contributor", "remote_dj")):
            with self.subTest(group=group_name):
                user = User.objects.create_user(
                    f"readonly{index}", f"readonly{index}@example.invalid", "pw"
                )
                Group.objects.get(name=group_name).user_set.add(user)
                self.client.force_login(user)

                detail = self.client.get(
                    reverse("library:track-detail", args=[track.pk])
                )
                direct = self.client.get(download_url)

                self.assertEqual(detail.status_code, 200)
                self.assertNotContains(detail, "Download Audio")
                self.assertNotContains(detail, download_url)
                self.assertEqual(direct.status_code, 403)

    def test_authorized_track_detail_shows_download_but_not_absolute_path(self):
        track, source = self.make_track()
        detail_url = reverse("library:track-detail", args=[track.pk])
        download_url = reverse("library:api-track-download", args=[track.pk])

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download Audio")
        self.assertContains(response, download_url)
        self.assertContains(response, source.name)
        self.assertNotContains(response, str(source.parent))

    def test_existing_audio_full_and_range_responses_are_unchanged(self):
        content = b"0123456789abcdefghijklmnopqrstuvwxyz"
        track, _source = self.make_track(filename="range-test.mp3", content=content)
        url = reverse("library:api-track-audio", args=[track.pk])

        full = self.client.get(url)
        self.assertEqual(full.status_code, 200)
        self.assertIsInstance(full, FileResponse)
        self.assertNotIn("attachment", full.get("Content-Disposition", ""))
        self.assertEqual(full["Accept-Ranges"], "bytes")
        self.assertEqual(self.response_bytes(full), content)

        partial = self.client.get(url, HTTP_RANGE="bytes=2-5")
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, content[2:6])
        self.assertEqual(partial["Content-Range"], f"bytes 2-5/{len(content)}")
        self.assertEqual(partial["Accept-Ranges"], "bytes")

    def test_download_keeps_bytes_that_flac_preview_intentionally_skips(self):
        id3_prefix = b"ID3\x04\x00\x00\x00\x00\x00\x03abc"
        flac_payload = b"fLaC\x00\x00source-payload"
        original = id3_prefix + flac_payload
        track, _source = self.make_track(
            filename="tagged.flac", content=original
        )

        audio = self.client.get(
            reverse("library:api-track-audio", args=[track.pk])
        )
        download = self.client.get(
            reverse("library:api-track-download", args=[track.pk])
        )

        self.assertEqual(self.response_bytes(audio), flac_payload)
        self.assertEqual(self.response_bytes(download), original)

    def test_large_download_uses_streaming_file_response(self):
        track, _source = self.make_track(
            filename="large-source.mp3", content=b"x" * (4 * 1024 * 1024)
        )

        response = self.client.get(
            reverse("library:api-track-download", args=[track.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(response.streaming)
        self.assertFalse(hasattr(response, "content"))
