# Django public-site example

`django_example.py` is an instructional sketch, not a drop-in Django app. It
shows the important boundary:

- `listener_submit` is browser-facing, CSRF-protected, and never uses the
  IsadoraAir shared secret.
- `pending_requests`, `catalog_sync`, and `status_update` are authenticated
  server-to-server views called only by IsadoraAir.

Adapt the model names, templates, URL routing, rate limiting, catalog search,
retention policy, and operator UI to the station's site. Put the shared key in
that site's secret manager/environment as `ISADORAAIR_API_KEY`; do not render it
into HTML or JavaScript.

Example routes:

```python
urlpatterns = [
    path("requests/submit/", views.listener_submit),
    path("api/isadoraair/catalog-sync/", views.catalog_sync),
    path("api/isadoraair/requests/pending/", views.pending_requests),
    path("api/isadoraair/requests/status/", views.status_update),
]
```

The full transport and acknowledgement contract is in
`docs/WEB_REQUESTS_INTEGRATION.md`.
