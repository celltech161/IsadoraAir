# Django public-site example

`django_example.py` is an instructional sketch, not a drop-in Django app. It
shows the important boundary:

- `listener_submit` is browser-facing, CSRF-protected, and never uses the
  IsadoraAir shared secret.
- `pending_requests`, `catalog_sync`, and `status_update` are authenticated
  server-to-server views called only by IsadoraAir.

Before using the sketch, adapt all of the following to the station's site:

- models and migrations;
- URL routing, templates, branding, and listener UI;
- catalog search and inactive-track handling;
- `ISADORAAIR_STATION_TIME_ZONE`, set to the same IANA timezone selected in
  IsadoraAir under **Config → Station Time**;
- shared-secret storage (`ISADORAAIR_API_KEY`) in the website backend's secret
  manager/environment;
- request validation, CSRF protection, rate limiting, and bot/spam mitigation;
- retention/privacy policy and operator support tools.

Never render `ISADORAAIR_API_KEY` into HTML or JavaScript. The availability
helper is a public-site UX gate; IsadoraAir remains authoritative about request
eligibility and scheduling.

Example routes:

```python
urlpatterns = [
    path("requests/submit/", views.listener_submit),
    path("api/isadoraair/catalog-sync/", views.catalog_sync),
    path("api/isadoraair/requests/pending/", views.pending_requests),
    path("api/isadoraair/requests/status/", views.status_update),
]
```

The complete Protocol v1 transport, storage, timezone, status, and
acknowledgement contract is in
[`docs/WEB_REQUESTS_INTEGRATION.md`](../../WEB_REQUESTS_INTEGRATION.md).
