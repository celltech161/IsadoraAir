"""Django test runner that refuses to run unless test isolation is engaged.

``settings.TEST_RUNNER`` points here.  The filesystem isolation itself is applied
while ``isadoraair/settings.py`` is imported (see ``isadoraair/test_isolation.py``);
this runner re-proves it before any database is created or test is run, prints the
effective roots, and fails the run if the write boundary refused anything -- even
when the code under test swallowed the refusal.
"""
import sys

from django.test.runner import DiscoverRunner

from isadoraair import test_isolation


class IsolatedDiscoverRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        state = test_isolation.assert_engaged()      # before any database or fixture exists
        if self.verbosity >= 1:
            print(test_isolation.describe(state), file=sys.stderr)
        super().setup_test_environment(**kwargs)

    def run_tests(self, test_labels, **kwargs):
        return test_isolation.fail_on_blocked(super().run_tests(test_labels, **kwargs))
