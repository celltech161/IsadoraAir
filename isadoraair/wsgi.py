"""
WSGI config for isadoraair project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'isadoraair.settings')

application = get_wsgi_application()

# Release/version-skew visibility (1.7 roadmap item) -- captures THIS
# gunicorn worker's own runtime commit identity exactly once, at the
# moment this worker process loads the application (no --preload is
# configured, so each of the configured workers imports this module
# independently after forking and gets its own correct capture; see
# isadoraair/version_info.py's own docstring for the full reasoning).
# monitoring/services/release_status.py reads this back via
# get_web_runtime_commit() to answer "what code is the process serving
# THIS /monitoring/ request actually running" -- deliberately not a
# live re-read of the checkout on every request, which would just be
# `git rev-parse HEAD` in disguise and prove nothing about what this
# worker actually loaded.
from isadoraair.version_info import capture_web_runtime_commit  # noqa: E402

capture_web_runtime_commit()
