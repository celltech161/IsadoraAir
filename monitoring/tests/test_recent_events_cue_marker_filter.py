"""r0124: routine cue-marker audit rows stay out of Monitoring Recent Events.

The records remain durable and admin-visible; only the operational dashboard
query excludes the cue-marker authority's stable deduplication-key namespace.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from monitoring.models import SystemEvent


CUE_PREFIX = "library|cue-markers|"


class RecentEventsCueMarkerFilterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "events-admin", "events-admin@example.invalid", "pw",
        )
        self.client.force_login(self.user)
        self.url = reverse("monitoring:api-recent-events")

    def event(self, title, *, category="library", level="info", key=None):
        return SystemEvent.objects.create(
            category=category,
            level=level,
            title=title,
            detail={"test": True},
            source="test",
            dedupe_key=key or f"{category}|{level}|{title}",
        )

    def body(self, **query):
        response = self.client.get(self.url, query, secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_operator_and_superseded_cue_marker_events_are_hidden_but_retained(self):
        operator = self.event(
            "Cue markers changed by operator",
            key=f"{CUE_PREFIX}12|1|Cue markers changed by operator",
        )
        superseded = self.event(
            "Cue-marker protection superseded by new media",
            level="warning",
            key=f"{CUE_PREFIX}12|2|Cue-marker protection superseded by new media",
        )
        self.assertEqual(self.body()["events"], [])
        self.assertEqual(
            set(SystemEvent.objects.values_list("pk", flat=True)),
            {operator.pk, superseded.pk},
        )

    def test_other_library_and_monitoring_events_remain_visible(self):
        library = self.event("Library media missing", level="error", key="library|media-missing|12")
        monitor = self.event(
            "Engine check recovered", category="monitor", level="warning", key="monitor|engine|recovered",
        )
        # Human-readable words never drive suppression.
        named = self.event("Cue marker import failed", level="warning", key="library|import-failed|12")
        found = {row["id"] for row in self.body()["events"]}
        self.assertEqual(found, {library.pk, monitor.pk, named.pk})

    def test_severity_category_and_time_window_filters_still_apply(self):
        info = self.event("Library info", level="info", key="library|info")
        warning = self.event("Library warning", level="warning", key="library|warning")
        old = self.event("Old critical", category="monitor", level="critical", key="monitor|old")
        SystemEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=8))

        filtered = self.body(window="7d", level="warning+", category="library")
        self.assertEqual([row["id"] for row in filtered["events"]], [warning.pk])
        all_recent = {row["id"] for row in self.body(window="7d")["events"]}
        self.assertEqual(all_recent, {info.pk, warning.pk})

    def test_limit_is_applied_after_cue_marker_exclusion(self):
        hidden = [
            SystemEvent(
                category="library", level="info", title=f"Hidden {i}", detail={}, source="test",
                dedupe_key=f"{CUE_PREFIX}{i}|1|Hidden",
            )
            for i in range(120)
        ]
        visible = [
            SystemEvent(
                category="engine", level="warning", title=f"Visible {i}", detail={}, source="test",
                dedupe_key=f"engine|visible|{i}",
            )
            for i in range(105)
        ]
        SystemEvent.objects.bulk_create(hidden + visible)
        rows = self.body()["events"]
        self.assertEqual(len(rows), 100)
        self.assertTrue(all(row["category"] == "engine" for row in rows))

    def test_category_choices_use_the_same_visible_base_queryset(self):
        self.event("Hidden", key=f"{CUE_PREFIX}9|1|Hidden")
        self.event("Monitor", category="monitor", key="monitor|visible")
        self.assertEqual(self.body()["categories"], ["monitor"])

    def test_admin_can_still_inspect_the_hidden_record(self):
        event = self.event("Retained cue audit", key=f"{CUE_PREFIX}9|1|Retained")
        response = self.client.get(
            reverse("admin:monitoring_systemevent_change", args=[event.pk]),
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Retained cue audit")
