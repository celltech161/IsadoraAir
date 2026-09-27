"""Roadmap 2.5C -- active Remote DJ session periodic re-authorization
(PlaybackEngine._remote_dj_authorization_tick).

Uses the established object.__new__(PlaybackEngine) minimal-stand-in
technique (see test_engine_mic_recovery.py's own docstring) to isolate
the tick's DECISION logic from the real GStreamer teardown --
_remote_dj_session_stop is mocked here specifically so these tests can
assert "was termination requested" without needing a real pipeline;
engine.py's actual, unmocked _remote_dj_session_stop is exercised by
the broader Remote DJ lifecycle test suite (e.g.
test_remote_dj_signaling_lifecycle.py) and is not re-tested here."""
import datetime as dt
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TransactionTestCase

import library.services.engine as eng_module
from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig, TalentAssignment

User = get_user_model()


def make_bare_engine():
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.remote_dj_session = None
    obj._remote_dj_session_stop = MagicMock()
    return obj


def make_remote_dj_user(group_name="Authz Tick Test Group", role_name="Authz Tick Test Role"):
    # get_or_create, not a plain .get(): this test class is a
    # TransactionTestCase (required for the tick's real cross-thread-
    # shaped authorize() call to see committed data), which truncates
    # between tests -- self-sufficient here rather than depending on
    # authz.migrations.0002's seed data still being present (avoids a
    # documented Django TransactionTestCase+serialized_rollback
    # ContentType-collision hazard when combined with OTHER such classes
    # in the same full-suite run; see PROJECT_NOTES.md's "Roadmap 2.5"
    # section).
    capability, _ = Capability.objects.get_or_create(
        slug="remote_dj.connect",
        defaults={"label": "Connect as a Remote DJ", "requires_schedule": True},
    )
    role = Role.objects.create(name=role_name)
    RoleCapability.objects.create(role=role, capability=capability)
    group = Group.objects.create(name=group_name)
    GroupRole.objects.create(group=group, role=role)
    user = User.objects.create_user(f"tick_test_{group_name}", password="pw")
    user.groups.add(group)
    return user


class RemoteDjAuthorizationTickTests(TransactionTestCase):
    """TransactionTestCase (not TestCase) -- same reasoning as
    test_remote_dj_signaling_lifecycle.py's class docstring AND matches
    this codebase's own established convention for TestCase classes
    that import library.services.engine (e.g. test_fx_fires_state.py,
    test_engine_playback_accounting_semantics.py): that module's
    unconditional top-level django.setup() call (needed for its
    standalone `manage.py run_engine` entry point) can invalidate a
    plain TestCase's already-open atomic connection when the module is
    imported for the first time in a process.

    Deliberately NOT serialized_rollback=True: combining that with
    OTHER serialized_rollback TransactionTestCase classes in the same
    full-suite run can hit a documented Django/ContentType duplicate-key
    collision (post_migrate's Permission/ContentType re-creation racing
    the serialized-fixture restore). make_remote_dj_user() above is
    self-sufficient (get_or_create) instead, so this class never depends
    on migration-seeded data surviving a prior test's flush."""

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        # Ensures the real "remote_dj.connect" Capability row exists
        # regardless of migration-seed/flush ordering -- see class
        # docstring. Needed even for tests (e.g. the staff one below)
        # that don't call make_remote_dj_user(), since authorize()
        # itself requires this row to exist for ANY caller.
        Capability.objects.get_or_create(
            slug="remote_dj.connect",
            defaults={"label": "Connect as a Remote DJ", "requires_schedule": True},
        )

    def _session_for(self, user):
        session = eng_module.RemoteDJSession()
        session.user_id = user.id
        session.connection_attempt = MagicMock(attempt_id="tick-test-attempt")
        return session

    def test_no_active_session_is_a_safe_noop(self):
        engine = make_bare_engine()
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_not_called()

    def test_session_with_no_user_id_is_a_safe_noop(self):
        engine = make_bare_engine()
        session = eng_module.RemoteDJSession()
        session.user_id = None
        engine.remote_dj_session = session
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_not_called()

    def test_still_authorized_session_is_left_alone(self):
        user = make_remote_dj_user()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_not_called()

    def test_capability_removed_retires_session(self):
        user = make_remote_dj_user()
        RoleCapability.objects.filter(role__group_bindings__group__user=user).delete()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_called_once()

    def test_account_disabled_retires_session(self):
        user = make_remote_dj_user()
        user.is_active = False
        user.save()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_called_once()

    def test_account_deleted_retires_session(self):
        user = make_remote_dj_user()
        session = self._session_for(user)
        user.delete()
        engine = make_bare_engine()
        engine.remote_dj_session = session
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_called_once()

    def test_scheduled_effective_end_reached_retires_session_when_enforcement_on(self):
        user = make_remote_dj_user()
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.pre_schedule_allowance_minutes = 0
        cfg.post_schedule_allowance_minutes = 0
        cfg.save()
        # An assignment that ended in the past, every day of the week --
        # guaranteed to already be outside its window regardless of when
        # this test actually runs.
        for dow in range(7):
            TalentAssignment.objects.create(
                user=user, day_of_week=dow, start_time=dt.time(0, 0), end_time=dt.time(0, 1),
            )
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_called_once()

    def test_no_assignment_at_all_retires_session_when_enforcement_on(self):
        user = make_remote_dj_user()
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_called_once()

    def test_compatibility_mode_session_survives_with_no_assignment_when_enforcement_off(self):
        """Enforcement OFF (this test's default via setUp): a Remote
        Host with the capability but no TalentAssignment stays connected
        -- proves compatibility policy applies to the ONGOING tick, not
        just token issuance."""
        user = make_remote_dj_user()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_not_called()

    def test_turning_enforcement_on_mid_session_is_applied_on_the_next_tick(self):
        user = make_remote_dj_user()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(user)

        # First tick: enforcement OFF (compatibility), no assignment -- survives.
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_not_called()

        # Operator flips the switch mid-session; still no assignment.
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()

        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_called_once()

    def test_staff_session_is_never_retired_even_with_nothing_configured(self):
        staff = User.objects.create_user("tick_test_staff", password="pw", is_staff=True)
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()
        engine = make_bare_engine()
        engine.remote_dj_session = self._session_for(staff)
        self.assertTrue(engine._remote_dj_authorization_tick())
        engine._remote_dj_session_stop.assert_not_called()
