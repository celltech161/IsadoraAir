"""Shared /library/ search-and-filter queryset logic. Used by
api_track_list (the normal library page), the related-artists autofill
endpoint (api_track_autofill_related_artists), and the
autofill_related_artists management command -- all three need to agree
on exactly which tracks a given search/category/ready2air combination
matches, so the autofill action's "process everything currently
matching the filter" promise is actually true regardless of which of
the three ever calls this.

Deliberately takes plain values (not a Django request) so the
management command's --query/--category/--ready2air flags can drive
the identical filter without constructing a fake request."""
from django.db.models import Q


def filter_tracks(qs, q="", category_id=None, ready2air=None):
    """Apply the standard library search/category/ready2air filter to
    `qs` (a Track queryset) and return the filtered queryset.
    Sorting/pagination stay the caller's responsibility -- this is
    filtering only.

    q: multi-term search using the existing "+"-joins-required-terms
       semantics (e.g. "Pink Floyd + Money" requires both "Pink Floyd"
       and "Money" to each match somewhere). For each term, matches
       title, primary artist, album, OR related_artists -- every
       required term must match at least one of those fields (not
       necessarily the same one).
    category_id: matches EITHER the track's primary category or one of
       its additional_categories (secondary rotation membership) --
       same as the existing /library/ behavior.
    ready2air: True/False to filter, or None for no filter on this
       field."""
    if q:
        terms = [t.strip() for t in q.split("+") if t.strip()]
        for term in terms:
            qs = qs.filter(
                Q(title__icontains=term)
                | Q(artist__name__icontains=term)
                | Q(album__title__icontains=term)
                | Q(related_artists__icontains=term)
            )

    if category_id:
        qs = qs.filter(Q(category_id=category_id) | Q(additional_categories=category_id)).distinct()

    if ready2air is not None:
        qs = qs.filter(ready2air=ready2air)

    return qs
