"""Test-only setup for schedule profile state.

The 3.1A migration creates the "Default Schedule" profile and the singleton
ScheduleProfileState. A TransactionTestCase flush removes those rows, and
production code deliberately does NOT recreate them (see
ScheduleProfileState.load()), so fixtures that need them recreate exactly what
the migration provides. This is not a runtime recovery path.
"""
from library.models import ScheduleProfile, ScheduleProfileState

INITIAL_PROFILE_NAME = "Default Schedule"


def ensure_schedule_profile_state():
    """Return the state row, creating the migration's initial profile and
    state only if the database has neither (call before creating any other
    profile)."""
    state = ScheduleProfileState.objects.filter(pk=1).first()
    if state is not None:
        return ScheduleProfileState.load()
    profile, _ = ScheduleProfile.objects.get_or_create(name=INITIAL_PROFILE_NAME)
    ScheduleProfileState.objects.create(active_profile=profile, default_profile=profile)
    return ScheduleProfileState.load()
