"""QA helper (not collected by default test discovery): render /library/ and the REAL /api/tracks/ answers for a
sequence of filter states inside the guarded test sandbox, and print them between markers so a browser outside the
sandbox can drive the real page JavaScript with those answers -- no server, database or station data involved.

    run_with_validation_service.sh <python> manage.py test library.tests.render_library_artist_count_artifact
"""
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import Artist, Category, CategoryKind, Track

User = get_user_model()

# (name, params) -- the page requests exactly sort/dir/page/per_page in addition to these.
STATES = (
    ("all", {}),
    ("search", {"q": "Sun"}),
    ("category", {"q": "Sun", "category": "POP"}),
    ("status", {"q": "Sun", "category": "POP", "ready2air": "true"}),
    ("rock_not_ready", {"category": "ROCK", "ready2air": "false"}),
    ("none", {"q": "zzz-nothing"}),
)


@override_settings(SECURE_SSL_REDIRECT=False)
class RenderArtifact(TestCase):
    def test_render(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        cats = {c: Category.objects.create(code=c, name=c.title(), kind=kind) for c in ("ROCK", "POP")}
        artists = {n: Artist.objects.create(name=n) for n in ("Alpha Band", "Beta Trio", "Gamma Quartet")}

        def track(title, artist, category, ready2air=True):
            return Track.objects.create(
                filepath=str(root / f"{title}.flac"), filename=f"{title}.flac", title=title,
                artist=artists[artist], category=cats[category], format="flac", duration_seconds=180.0,
                ready2air=ready2air)
        track("Sunrise", "Alpha Band", "ROCK")
        track("Sunset", "Alpha Band", "ROCK", ready2air=False)
        track("Moonlight", "Alpha Band", "POP")
        track("Sunburst", "Beta Trio", "POP")
        track("Harbor", "Gamma Quartet", "ROCK", ready2air=False)
        self.client.force_login(User.objects.create_superuser("artifact-su", "s@example.invalid", "pw"))
        page = self.client.get(reverse("library:library")).content.decode()
        answers = {}
        for name, params in STATES:
            query = {**{k: (str(cats[v].pk) if k == "category" else v) for k, v in params.items()},
                     "sort": "title", "dir": "asc", "page": "1", "per_page": "50"}
            response = self.client.get(reverse("library:api-track-list"), query)
            self.assertEqual(response.status_code, 200)
            answers[name] = {"params": query, "body": response.json()}
        answers["_categories"] = {c: cats[c].pk for c in cats}
        for name, blob in (("PAGE", page), ("ANSWERS", json.dumps(answers))):
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
