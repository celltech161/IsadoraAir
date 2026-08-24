"""Small unauthenticated loopback-compatible application/database probe."""
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import HttpResponse
from django.views.decorators.http import require_GET


@login_not_required
@require_GET
def healthz(_request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            healthy = cursor.fetchone() == (1,)
    except Exception:
        healthy = False
    return HttpResponse(
        "ok\n" if healthy else "unhealthy\n",
        status=200 if healthy else 503,
        content_type="text/plain; charset=utf-8",
    )
