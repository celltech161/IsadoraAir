"""QA helper (not collected by default test discovery): render /library/ and the REAL list, preview and apply answers
for the Bulk Edit Related Artists workflow inside the guarded test sandbox, and print them between markers so a
browser outside the sandbox can drive the real page with exactly those answers -- no server, database or station
data involved.  The browser checks that the page sends exactly the request these answers were computed for.

    run_with_validation_service.sh <python> manage.py test library.tests.render_library_bulk_related_artifact
"""
import json

from library.models import Artist
from library.tests.test_library_bulk_edit_r0125 import _Fixture


class RenderArtifact(_Fixture):
    def test_render(self):
        gilmour = Artist.objects.create(name="David Gilmour")
        full = ", ".join(f"N{i:03d}".ljust(9, "x") for i in range(45))          # 493 characters
        tracks = {
            "existing": self.track("A Existing Guests", artist=gilmour, related_artists="Roger Waters"),
            "empty": self.track("B No Guests", related_artists=""),
            "full": self.track("C Nearly Full", related_artists=full),
        }
        page = self.client.get("/library/").content.decode()
        listing = self.client.get("/api/tracks/?sort=title&dir=asc&page=1&per_page=50").content.decode()
        selection = {"mode": "ids", "ids": sorted(t.pk for t in tracks.values())}
        flows = {}
        for name, changes in (
            ("append", {"related_artists": {"op": "append", "value": "Nick Mason, DAVID GILMOUR, roger waters"}}),
            ("set_with_artist", {"artist": {"op": "set", "value": "David Gilmour"},
                                 "related_artists": {"op": "set", "value": "David Gilmour, Rick Wright"}}),
        ):
            status, preview = self.preview(selection, changes)
            self.assertEqual(status, 200, preview)
            status, applied = self.post("library:api-track-bulk-edit-apply",
                                        {"selection": selection, "changes": changes, "token": preview["token"]})
            self.assertEqual(status, 200, applied)
            flows[name] = {"changes": changes, "preview": preview, "apply": applied}
        meta = {"ids": {k: t.pk for k, t in tracks.items()}, "selection": selection, "full_length": len(full)}
        for name, blob in (("PAGE", page), ("LIST", listing), ("FLOWS", json.dumps(flows)), ("META", json.dumps(meta))):
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
