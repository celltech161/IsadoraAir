"""QA helper (not collected by default test discovery): render /library/ and representative list, preview
and apply payloads for the r0125 bulk editor inside the guarded test sandbox, and print them between
markers so a browser outside the sandbox can exercise the real page with those APIs stubbed -- no
server, database or station data involved.

    run_with_validation_service.sh <python> manage.py test library.tests.render_library_bulk_artifact
"""
import json

from library.models import Track
from library.tests.test_library_bulk_edit_r0125 import _Fixture


class RenderArtifact(_Fixture):
    def test_render(self):
        for i in range(120):
            self.track(f"Song {i:03d}", category=self.rock if i % 2 else self.pop, energy="slow" if i % 3 else "")
        page = self.client.get("/library/").content.decode()
        lists = [self.client.get(f"/api/tracks/?sort=title&dir=asc&page={n}&per_page=50").content.decode()
                 for n in (1, 2)]
        first = json.loads(lists[0])["items"]
        selection = {"mode": "filter", "filter": {"q": "", "category": "", "ready2air": ""},
                     "exclude_ids": [first[0]["id"]]}
        changes = {"energy": {"op": "set", "value": "upbeat"}, "category": {"op": "set", "value": self.blues.pk},
                   "artist": {"op": "set", "value": "A Brand New Artist"}}
        status, preview = self.preview(selection, changes)
        self.assertEqual(status, 200, preview)
        status, applied = self.post("library:api-track-bulk-edit-apply",
                                    {"selection": selection, "changes": changes, "token": preview["token"]})
        self.assertEqual(status, 200, applied)
        applied["failed"] = [{"id": first[1]["id"], "title": first[1]["title"], "artist": "Bulk Artist",
                              "reason": "Track ['artist'] of a projected spoken Track change only through the "
                                        "spoken projection"}]
        for name, blob in (("PAGE", page), ("LIST1", lists[0]), ("LIST2", lists[1]),
                           ("PREVIEW", json.dumps(preview)), ("APPLY", json.dumps(applied))):
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
        self.assertEqual(Track.objects.count(), 120)
