"""D3D: the operator board's execution-history reads stay bounded as history
grows -- on a generated, representative history in the isolated test
database (never a station database).

Correctness (newest airing, deterministic ties, exact message membership,
keyset pages that visit every airing once) and access paths (PostgreSQL
EXPLAIN after ANALYZE): the newest-first reads use active_execution_recent,
membership uses the active_execution_members GIN index, the unfinished
airing uses the open_slot unique index, and no query of the 20-second status
poll scans the execution or message tables. No timing is asserted.

With AMB_EXPLAIN=<directory> the EXPLAIN (ANALYZE, BUFFERS) text of every
checked query -- and of the pre-correction forms without the new indexes --
is written there as evidence."""
import json
import os
import uuid
from pathlib import Path

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from active_messages import board, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage

from .support import Domain, compatibility_type, raw

X = ActiveBroadcastExecution
M = ActiveBroadcastMessage
EXECUTIONS = 20000
MESSAGES = 20000
POOL = 2000
MEMBER_OF = 40
TIES = 5
DIGEST = "a" * 64
TABLES = ("active_messages_activebroadcastexecution", "active_messages_activebroadcastmessage")


class HistoryAtScaleTests(Domain, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.target = uuid.uuid4()
        now = timezone.now()
        with connection.cursor() as cursor:
            # One INSERT ... SELECT (no later UPDATE, so no dead index entries a
            # station database would have vacuumed): one airing a minute going
            # back from now, 1-3 messages from a pool of POOL, the target message
            # in MEMBER_OF of them, and TIES airings sharing the newest timestamp.
            cursor.execute(f"""
                INSERT INTO active_messages_activebroadcastexecution
                    (id, reason, state, open_slot, policy_generation, composition_digest, composition_snapshot,
                     component_count, requested_by_username, created_at, due_at, terminal_at, attempt_count,
                     engine_instance, failure_code, failure_detail)
                SELECT md5('execution' || n)::uuid, 'scheduled', 'suppressed', NULL, 1, %(digest)s,
                       jsonb_build_object('v', 1, 'components', c.components, 'stinger', NULL,
                                          'stinger_seconds', NULL, 'accounted_seconds', '30.000000'),
                       jsonb_array_length(c.components), '', t.at, t.at, t.at, 0, '', 'stale_generation', ''
                FROM generate_series(1, {EXECUTIONS}) AS n
                CROSS JOIN LATERAL (SELECT CASE WHEN n <= {TIES} THEN %(now)s + interval '1 minute'
                                                ELSE %(now)s - make_interval(mins => n) END AS at) AS t
                CROSS JOIN LATERAL (
                    SELECT jsonb_agg(jsonb_build_object(
                               'message', m, 'kind', 'public_address', 'content_type', 1,
                               'content_type_key', 'public_address', 'media', md5('media' || n || m)::uuid::text,
                               'sha256', %(digest)s, 'producer_key', 'user:1', 'producer_username', 'producer',
                               'activated_at', '2026-01-01T00:00:00+00:00', 'duration_seconds', '30.000000'))
                           AS components
                    FROM unnest((ARRAY[md5('pool' || ((n * 7) %% {POOL}))::uuid::text,
                                       md5('pool' || ((n * 13 + 1) %% {POOL}))::uuid::text,
                                       md5('pool' || ((n * 31 + 2) %% {POOL}))::uuid::text])[1:1 + n %% 3]
                                || CASE WHEN n %% ({EXECUTIONS} / {MEMBER_OF}) = 7 THEN ARRAY[%(target)s]
                                        ELSE ARRAY[]::text[] END) AS m) AS c
            """, {"digest": DIGEST, "now": now, "target": str(cls.target)})
        content_type = compatibility_type("public_address")
        raw(M).bulk_create([M(id=uuid.uuid4(), kind="public_address", content_type=content_type, state="cleared",
                              title=f"old {index}", producer_key="user:1", producer_username="producer",
                              state_changed_at=now, terminal_at=now, terminal_reason="history")
                            for index in range(MESSAGES)], batch_size=2000)
        with connection.cursor() as cursor:
            for table in TABLES:
                cursor.execute(f"ANALYZE {table}")

    # -- helpers ---------------------------------------------------------------------------------
    def plan(self, sql, params=(), *, analyze=False) -> str:
        options = "ANALYZE, BUFFERS, " if analyze else ""
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN ({options}FORMAT TEXT) {sql}", params)
            return "\n".join(row[0] for row in cursor.fetchall())

    def query_plan(self, queryset, name, **kwargs) -> str:
        sql, params = queryset.query.sql_with_params()
        return self.record(name, sql, params, **kwargs)

    def record(self, name, sql, params=(), *, uses=None):
        text = self.plan(sql, params)
        directory = os.environ.get("AMB_EXPLAIN")
        if directory:
            with open(Path(directory) / "explain.txt", "a") as out:
                out.write(f"=== {name}\n{sql % tuple(repr(p) for p in params) if params else sql}\n"
                          f"{self.plan(sql, params, analyze=True)}\n\n")
        if uses:
            self.assertIn(uses, text, f"{name}:\n{text}")
        for table in TABLES:
            self.assertNotIn(f"Seq Scan on {table}", text, f"{name}:\n{text}")
        return text

    def ordered(self):
        return list(X.objects.order_by("-created_at", "-id").values_list("id", "created_at"))

    # -- correctness -------------------------------------------------------------------------------
    def test_the_newest_airing_and_ties_are_deterministic(self):
        ordered = self.ordered()
        top = [row for row in ordered if row[1] == ordered[0][1]]
        self.assertEqual(len(top), TIES)
        self.assertEqual([row[0] for row in top], sorted((row[0] for row in top), reverse=True))   # id breaks ties
        self.assertEqual(X.objects.order_by(*board.EXECUTION_ORDER).first().pk, ordered[0][0])
        self.assertIsNone(board._unfinished_execution())

    def test_keyset_pages_visit_every_airing_once_in_order(self):
        expected = [row[0] for row in self.ordered()]
        seen, cursor = [], None
        while True:
            page, cursor = board._execution_page(X.objects.all(), cursor)
            seen.extend(execution.pk for execution in page)
            self.assertLessEqual(len(page), board.AIRINGS_PAGE)
            if cursor is None:
                break
        self.assertEqual(seen, expected)                       # ties included: no duplicate, nothing skipped

    def test_membership_is_exactly_the_airings_containing_the_message(self):
        message = M(pk=self.target)
        expected = [execution.pk for execution in X.objects.order_by(*board.EXECUTION_ORDER)
                    if str(self.target) in {c["message"] for c in execution.composition_snapshot["components"]}]
        self.assertEqual(len(expected), MEMBER_OF)
        self.assertEqual(sorted(X.objects.filter(composition_snapshot__contains={"components": [
            {"message": str(self.target)}]}).values_list("id", flat=True)), sorted(expected))
        page, cursor = board._member_page(message)
        self.assertEqual([execution.pk for execution in page], expected[:board.AIRINGS_PAGE])
        rest, last = board._member_page(message, cursor)
        self.assertEqual([execution.pk for execution in rest], expected[board.AIRINGS_PAGE:])
        self.assertIsNone(last)
        self.assertEqual(board._member_page(M(pk=uuid.uuid4())), ([], None))

    # -- access paths ----------------------------------------------------------------------------
    def test_history_reads_use_their_indexes(self):
        middle = X.objects.order_by(*board.EXECUTION_ORDER)[EXECUTIONS // 2]
        self.query_plan(X.objects.order_by(*board.EXECUTION_ORDER)[:1], "newest airing (status poll, no unfinished)",
                        uses="active_execution_recent")
        self.query_plan(X.objects.order_by(*board.EXECUTION_ORDER)[:board.RECENT_AIRINGS], "board: recent airings",
                        uses="active_execution_recent")
        self.query_plan(X.objects.filter(open_slot=1), "unfinished airing (status poll)",
                        uses="active_execution_one_unfinished")
        deep = (X.objects.order_by(*board.EXECUTION_ORDER).filter(created_at__lte=middle.created_at)
                .exclude(created_at=middle.created_at, id__gte=middle.pk)[:board.AIRINGS_PAGE + 1])
        text = self.query_plan(deep, "all airings: a deep keyset page", uses="active_execution_recent")
        self.assertIn("Index Cond", text)                     # the scan starts at the cursor, not at the top
        table = X._meta.db_table
        probe = json.dumps({"components": [{"message": str(self.target)}]})
        self.record("message page: airings with this message", board.MEMBER_SQL.format(table=table, after=""),
                    [probe, board.AIRINGS_PAGE + 1], uses="active_execution_members")
        page, cursor = board._member_page(M(pk=self.target))
        self.record("message page: older airings with this message",
                    board.MEMBER_SQL.format(table=table, after="WHERE (members.created_at, members.id) < (%s, %s)"),
                    [probe, cursor.created_at, cursor.pk, board.AIRINGS_PAGE + 1], uses="active_execution_members")

    def test_the_status_poll_never_scans_history(self):
        services.current_policy()
        with CaptureQueriesContext(connection) as captured:
            board._status(services.current_policy())
        selects = [query["sql"] for query in captured if query["sql"].lstrip().upper().startswith("SELECT")]
        self.assertTrue(selects)
        for index, sql in enumerate(selects):
            self.record(f"status poll query {index + 1}", sql)

    def test_the_pre_correction_forms_scanned_history(self):
        """Evidence only: without the D3D indexes the reviewed forms read the
        whole execution table (run inside the test's transaction: the DROP is
        rolled back)."""
        with connection.cursor() as cursor:
            cursor.execute("DROP INDEX active_execution_recent")
            cursor.execute("DROP INDEX active_execution_members")
        before = {
            "BEFORE: newest airing": X.objects.order_by("-created_at")[:1],
            "BEFORE: unfinished airing": X.objects.filter(state__in=("pending_render", "ready", "accepted", "claimed",
                                                                      "started"))[:1],
            "BEFORE: airings with this message": X.objects.filter(
                composition_snapshot__components__contains=[{"message": str(self.target)}]).order_by("-created_at")[:10],
        }
        for name, queryset in before.items():
            sql, params = queryset.query.sql_with_params()
            text = self.plan(sql, params)
            if os.environ.get("AMB_EXPLAIN"):
                with open(Path(os.environ["AMB_EXPLAIN"]) / "explain.txt", "a") as out:
                    out.write(f"=== {name}\n{self.plan(sql, params, analyze=True)}\n\n")
            if name != "BEFORE: unfinished airing":           # (state, created_at) serves that one already
                self.assertIn("Seq Scan on active_messages_activebroadcastexecution", text, f"{name}:\n{text}")
