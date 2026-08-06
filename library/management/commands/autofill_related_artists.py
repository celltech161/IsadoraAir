from django.core.management.base import BaseCommand, CommandError

from library.models import Category, Track
from library.services.related_artists import autofill_related_artists_for_queryset
from library.services.track_filters import filter_tracks


class Command(BaseCommand):
    help = (
        "Scan tracks for feat./ft./featuring/with/bare-&-and credits and "
        "append newly discovered related artists (used for rotation "
        "artist-separation) -- never replaces an existing manual value, "
        "only appends deduplicated discoveries. Calls the exact same "
        "service function the /library/ 'Auto-fill Related Artists' "
        "toolbar action uses, so both always behave identically. "
        "Defaults to a dry run; pass --apply to actually write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write changes. Without this flag, the command only reports "
                 "what it would do.",
        )
        parser.add_argument(
            "--query", type=str, default="",
            help="Search filter -- same '+'-joins-required-terms semantics as "
                 "/library/'s search box (e.g. 'Pink Floyd + Money'). Matches "
                 "title, primary artist, album, or related_artists.",
        )
        parser.add_argument(
            "--category", type=str, default=None,
            help="Category CODE (not name) to restrict to -- matches a "
                 "track's primary category OR one it's additionally filed "
                 "under, same as /library/'s category filter.",
        )
        parser.add_argument(
            "--ready2air", type=str, choices=["true", "false"], default=None,
            help="Restrict to ready2air=true or ready2air=false. Omit for no "
                 "filter on this field.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Only scan the first N matching tracks (0 = no limit). "
                 "Applied as a queryset slice, so combine with --query/"
                 "--category for a predictable subset rather than an "
                 "arbitrary one.",
        )
        parser.add_argument(
            "--samples", type=int, default=10,
            help="How many before/after examples to print for changed "
                 "tracks (default 10, capped at 25 regardless of what's "
                 "passed here -- see related_artists.SAMPLE_CAP). Avoids "
                 "dumping tens of thousands of lines by default.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        query = options["query"]
        category_code = options["category"]
        ready2air_raw = options["ready2air"]
        limit = options["limit"]
        sample_count = max(0, options["samples"])

        ready2air = None
        if ready2air_raw == "true":
            ready2air = True
        elif ready2air_raw == "false":
            ready2air = False

        category_id = None
        if category_code:
            try:
                category_id = Category.objects.get(code=category_code).id
            except Category.DoesNotExist:
                raise CommandError(
                    f"No Category with code '{category_code}'. Category codes "
                    f"are case-sensitive folder names, e.g. 'HOT_CURR' -- see "
                    f"/admin/library/category/ for the exact list."
                )

        mode = "APPLY" if apply_changes else "DRY RUN (pass --apply to write)"
        self.stdout.write(f"Mode: {mode}")
        if query:
            self.stdout.write(f"Query: {query!r}")
        if category_code:
            self.stdout.write(f"Category: {category_code}")
        if ready2air is not None:
            self.stdout.write(f"ready2air: {ready2air}")

        qs = filter_tracks(Track.objects.all(), q=query, category_id=category_id, ready2air=ready2air)
        qs = qs.order_by("id")
        if limit:
            qs = qs[:limit]

        result = autofill_related_artists_for_queryset(qs, apply=apply_changes)

        self.stdout.write("")
        self.stdout.write("=== Summary ===")
        self.stdout.write(f"Tracks scanned:            {result['scanned']}")
        verb = "Tracks updated" if apply_changes else "Tracks that would change"
        self.stdout.write(f"{verb}:{' ' * max(1, 27 - len(verb))}{result['changed']}")
        self.stdout.write(f"Artists appended:          {result['appended']}")
        self.stdout.write(f"Already current:           {result['unchanged_already_current']}")
        self.stdout.write(f"No discoveries:            {result['unchanged_no_discoveries']}")
        if result["unchanged_overflow"]:
            self.stdout.write(self.style.WARNING(
                f"Skipped (500-char limit):  {result['unchanged_overflow']} track(s), "
                f"{result['overflow_skipped']} artist name(s) -- existing values preserved"
            ))
        if result["errors"]:
            self.stdout.write(self.style.ERROR(f"Errors:                    {result['errors']}"))

        samples = result["samples"][:sample_count]
        if samples:
            self.stdout.write("")
            self.stdout.write(f"=== Sample changes (showing {len(samples)} of {result['changed']}) ===")
            for s in samples:
                label = f"{s['artist']} - {s['title']}" if s["artist"] else s["title"]
                self.stdout.write(f"  [{s['track_id']}] {label}")
                self.stdout.write(f"      before: {s['before'] or '(empty)'}")
                self.stdout.write(f"      after:  {s['after']}")

        if not apply_changes and result["changed"]:
            self.stdout.write("")
            self.stdout.write("Dry run only -- pass --apply to write these changes.")
