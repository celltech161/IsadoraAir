"""QA helper (not collected by default test discovery): inside the guarded test sandbox, render /library/ and capture the
REAL answers of /api/tracks/ and /api/tracks/export/ (status, headers, body bytes) for a fixed sequence of filter/sort/
page states, and print them between markers so a browser outside the sandbox can drive the real page JavaScript against
them -- including a real browser download event. No server, production database or station data is involved.

    run_with_validation_service.sh <python> manage.py test library.tests.render_library_csv_export_artifact

The browser driver (evidence dir, standalone) requests exactly the states listed in STATES.
"""
import base64
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import Artist, Category, CategoryKind, Track
from library.services import track_csv_export

User = get_user_model()

# Params the page sends (sort/dir/page/per_page are added per state by the driver's expectations).
LIST_STATES = (
    {"sort": "title", "dir": "asc", "page": "1", "per_page": "50"},
    {"sort": "title", "dir": "asc", "page": "2", "per_page": "50"},
    {"q": "Sun", "category": "POP", "ready2air": "true", "sort": "title", "dir": "asc", "page": "1", "per_page": "50"},
    {"q": "Sun", "category": "POP", "ready2air": "true", "sort": "artist", "dir": "asc", "page": "1", "per_page": "50"},
    {"q": "Sun", "category": "POP", "ready2air": "true", "sort": "artist", "dir": "desc", "page": "1", "per_page": "50"},
    {"q": "zzz-nothing", "sort": "title", "dir": "asc", "page": "1", "per_page": "50"},
)
EXPORT_STATES = (
    {"sort": "title", "dir": "asc"},
    {"q": "Sun", "category": "POP", "ready2air": "true", "sort": "artist", "dir": "desc"},
    {"q": "zzz-nothing", "sort": "artist", "dir": "desc"},   # clicked right after typing, sort state carried over
)
DURATIONS = (None, 0, 0.4, 0.5, 1.5, 2.5, 59.4, 59.5, 59.6, 60, 119.49, 119.5, 119.6, 3599.4, 3599.5, 3600, 36000)


@override_settings(SECURE_SSL_REDIRECT=False)
class RenderArtifact(TestCase):
    def test_render(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        cats = {c: Category.objects.create(code=c, name=c.title(), kind=kind) for c in ("ROCK", "POP")}
        artists = {}

        def track(i, title, artist, category, ready2air=True, duration=180.0):
            if artist not in artists:
                artists[artist] = Artist.objects.create(name=artist)
            return Track.objects.create(
                filepath=str(root / f"{i}.flac"), filename=f"{i}.flac", title=title, artist=artists[artist],
                category=cats[category], format="flac", duration_seconds=duration, ready2air=ready2air)
        # 120 plain Tracks = three Library pages of 50; "Sun*" ones are the search/category/status targets.
        for i in range(110):
            track(i, f"Song {i:03d}", f"Artist {i % 7}", "ROCK" if i % 2 else "POP")
        sun = [("Sunrise", "Zed Band", "POP", True, 119.6), ("Sunset", "Yan, The Band", "POP", True, 59.5),
               ("Sunburst", "Xia", "POP", False, 61.2), ("=Sun Formula", "+Plus Artist", "POP", True, 3599.5),
               ("Sünny \"Day\"", "Beyoncé", "ROCK", True, None)]
        for j, (title, artist, cat, ready, dur) in enumerate(sun):
            track(1000 + j, title, artist, cat, ready, dur if dur is not None else None)
        self.client.force_login(User.objects.create_superuser("artifact-su", "s@example.invalid", "pw"))
        page = self.client.get(reverse("library:library")).content.decode()

        def params(d):
            return {k: (str(cats[v].pk) if k == "category" else v) for k, v in d.items()}
        lists, exports = [], []
        for state in LIST_STATES:
            q = params(state)
            response = self.client.get(reverse("library:api-track-list"), q)
            self.assertEqual(response.status_code, 200)
            lists.append({"params": q, "body": response.json()})
        for state in EXPORT_STATES:
            q = params(state)
            response = self.client.get(reverse("library:api-track-export"), q)
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.streaming_content)
            exports.append({"params": q, "status": response.status_code, "body_b64": base64.b64encode(body).decode(),
                            "headers": {k: response[k] for k in
                                        ("Content-Type", "Content-Disposition", "Cache-Control", "X-Accel-Buffering")}})
        answers = {
            "lists": lists, "exports": exports, "categories": {c: cats[c].pk for c in cats},
            "durations": [[d, track_csv_export.format_duration(d)] for d in DURATIONS],
            "total": Track.objects.count(),
        }
        for name, blob in (("PAGE", page), ("ANSWERS", json.dumps(answers))):
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
