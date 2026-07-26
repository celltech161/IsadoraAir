"""Custom Django FileSystemStorage bound to REPORTS_ROOT.

The RoyaltyReport model's FileField uses this so generated regulatory
filings land at /var/lib/isadoraair/reports/ (operational data) rather
than in the repo's media/ tree. Kept in its own module so migrations
can `from library.storage import royalty_report_storage` without
importing the full models package.
"""

from django.conf import settings
from django.core.files.storage import FileSystemStorage


royalty_report_storage = FileSystemStorage(location=str(settings.REPORTS_ROOT))
