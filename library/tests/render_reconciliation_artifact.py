"""QA helper (not collected by default test discovery): render /library/reconciliation/ and its API payloads for
a representative scan inside the guarded sandbox, printed between markers, so a browser outside the sandbox
can exercise the real page with those APIs stubbed -- no server, database or station data involved.

    run_with_validation_service.sh <python> manage.py test library.tests.render_reconciliation_artifact
"""
import json

from library.services import library_reconciliation as reconciliation
from library.tests.test_library_reconciliation_b import _RelinkSetup, _Recon


class RenderArtifact(_RelinkSetup, _Recon):
    HOSTILE = '"><img src=x onerror="window.__pwned=1">'

    def test_render(self):
        self.prepare()                                                   # a provable move
        for i in range(30):                                              # enough findings for two pages
            self.track(f"ROCK/missing-{i:02d}.flac", create_file=False, title=f"Lost {i:02d}")
        for i in range(40):
            self.track(f"ROCK/ok-{i:02d}.flac")
        self.track("ROCK/twin-a.flac", title="Twin", duration=200.0)
        self.track("POP/twin-b.flac", title="Twin", duration=200.5)
        self.track("ROCK/hostile.flac", create_file=False, title=self.HOSTILE)
        self.file(f"INBOX/{self.HOSTILE.replace('/', '_')}.flac")
        # Exact-byte file reviews: a registered Track with an identical, hostile-named unregistered copy, and two
        # identical unregistered files with no Track at all.
        exact = b"exact duplicate bytes" * 300
        self.track("ROCK/exact-registered.flac", data=exact, title="Exact Registered")
        self.file('INBOX/copy "quoted" <b onmouseover=window.__pwned=3>.flac', exact)
        orphan = b"another identical pair" * 300
        self.file("INBOX/orphan-a.flac", orphan)
        self.file("INBOX/orphan-b.flac", orphan)
        report = reconciliation.run_scan()
        moved = next(f for f in report["findings"] if f["type"] == "moved")
        duplicate = next(f for f in report["findings"] if f["type"] == "duplicate" and len(f["members"]) >= 2)
        page = self.client.get("/library/reconciliation/").content.decode()
        get = lambda url, **q: self.client.get(url, q).content.decode()
        blobs = {
            "PAGE": page,
            "SUMMARY": get("/api/tracks/reconciliation/"),
            "ALL1": get("/api/tracks/reconciliation/findings/", page=1, per_page=25, open=1),
            "ALL2": get("/api/tracks/reconciliation/findings/", page=2, per_page=25, open=1),
            "MOVED": get("/api/tracks/reconciliation/findings/", type="moved", page=1, per_page=25, open=1),
            "DUPLICATE": get("/api/tracks/reconciliation/findings/", type="duplicate", page=1, per_page=25, open=1),
        }
        preview = self.client.post("/api/tracks/reconciliation/relink/preview/",
                                   {"scan_id": report["scan_id"], "finding_id": moved["id"]},
                                   content_type="application/json").json()
        blobs["PREVIEW"] = json.dumps(preview)
        blobs["APPLY"] = self.client.post("/api/tracks/reconciliation/relink/apply/", {"token": preview["token"]},
                                          content_type="application/json").content.decode()
        members = duplicate["members"]
        blobs["DECISION"] = self.client.post("/api/tracks/reconciliation/duplicates/not-duplicate/", {
            "scan_id": report["scan_id"], "finding_id": duplicate["id"], "track_a": members[0],
            "track_b": members[1]}, content_type="application/json").content.decode()
        for name, blob in blobs.items():
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
        self.assertIn("relinked", blobs["APPLY"])
