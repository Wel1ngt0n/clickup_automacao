"""State contracts without connecting to a production database."""

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import ModuleType
from unittest.mock import MagicMock, patch
from uuid import uuid4

from partner_state import PartnerStateError, PartnerStore, _lock_id


class PartnerStateTests(unittest.TestCase):
    def setUp(self):
        self.store = PartnerStore("postgresql://test.invalid/test")
        self.connection = MagicMock()

    @contextmanager
    def connected(self):
        with patch.object(self.store, "_connection") as connect:
            connect.return_value.__enter__.return_value = self.connection
            yield self.connection

    def test_rejects_non_postgresql_storage(self):
        with self.assertRaises(PartnerStateError):
            PartnerStore("sqlite:///ephemeral.db")

    def test_lock_key_is_stable_and_scoped_to_parent(self):
        self.assertEqual(_lock_id("parent-1"), _lock_id("parent-1"))
        self.assertNotEqual(_lock_id("parent-1"), _lock_id("parent-2"))
        self.assertGreaterEqual(_lock_id("parent-1"), -(2**63))
        self.assertLess(_lock_id("parent-1"), 2**63)

    def test_holds_session_lock_until_work_finishes(self):
        with self.connected() as connection:
            connection.execute.return_value.fetchone.return_value = {"acquired": True}
            with self.store.store_lock("parent-1") as acquired:
                self.assertTrue(acquired)
                self.assertEqual(connection.execute.call_count, 1)
            self.assertEqual(connection.execute.call_count, 2)
            self.assertIn("pg_advisory_unlock", connection.execute.call_args.args[0])

    def test_releases_acquired_lock_after_work_failure(self):
        with self.connected() as connection:
            connection.execute.return_value.fetchone.return_value = {"acquired": True}
            with (
                self.assertRaisesRegex(ValueError, "work failed"),
                self.store.store_lock("parent-1"),
            ):
                raise ValueError("work failed")
            self.assertIn("pg_advisory_unlock", connection.execute.call_args.args[0])

    def test_busy_parent_does_not_unlock_another_session(self):
        with self.connected() as connection:
            connection.execute.return_value.fetchone.return_value = {"acquired": False}
            with self.store.store_lock("parent-1") as acquired:
                self.assertFalse(acquired)
            self.assertEqual(connection.execute.call_count, 1)

    def test_new_reservation_is_distinct_from_existing_pending_operation(self):
        with patch.object(
            self.store, "_one", side_effect=[{"operation_key": "create"}, None]
        ):
            self.assertTrue(self.store.reserve_operation("parent", "create"))
            self.assertFalse(self.store.reserve_operation("parent", "create"))

    def test_conflicting_completion_is_rejected(self):
        with (
            patch.object(self.store, "_one", return_value=None),
            self.assertRaises(PartnerStateError),
        ):
            self.store.complete_operation("parent", "create", "different-task")

    def test_empty_result_never_completes_reservation(self):
        with patch.object(self.store, "_one") as query:
            with self.assertRaises(ValueError):
                self.store.complete_operation("parent", "create", " ")
            query.assert_not_called()

    def test_context_returns_original_snapshot(self):
        saved = {"existing_client": False, "config": {"revision": 1}}
        edited = {"existing_client": True, "config": {"revision": 2}}
        with patch.object(self.store, "_one", return_value={"context_json": saved}):
            self.assertEqual(self.store.job_context("parent", edited), saved)

    def test_retry_stores_only_a_safe_error_code(self):
        with patch.object(self.store, "_one") as query:
            self.store.retry("parent", 30, "invalid response password=example")
            self.assertEqual(
                query.call_args.args[1], (30, "processing_error", "parent")
            )

    def test_invalid_query_limits_are_rejected_before_connecting(self):
        with patch.object(self.store, "_connection") as connect:
            for value in (0, -1, 1001, True, 1.5):
                with self.subTest(limit=value), self.assertRaises(ValueError):
                    self.store.pending_jobs(value)
            connect.assert_not_called()

    def test_database_error_does_not_expose_connection_details(self):
        driver = ModuleType("psycopg")
        rows = ModuleType("psycopg.rows")
        driver.Error = type("DatabaseError", (Exception,), {})
        driver.connect = MagicMock(side_effect=driver.Error("password=example-only"))
        rows.dict_row = object()
        with (
            patch.dict(sys.modules, {"psycopg": driver, "psycopg.rows": rows}),
            self.assertRaises(PartnerStateError) as caught,
        ):
            self.store.enqueue("parent")
        self.assertNotIn("example-only", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


@unittest.skipUnless(
    os.environ.get("PARTNER_TEST_DATABASE_URL"),
    "Defina PARTNER_TEST_DATABASE_URL para testar um PostgreSQL dedicado a testes.",
)
class PartnerPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url = os.environ["PARTNER_TEST_DATABASE_URL"]
        cls.store = PartnerStore(cls.database_url)
        cls.store.initialize()

    def setUp(self):
        self.parent_id = f"state-test-{uuid4()}"

    def tearDown(self):
        with self.store._connection() as connection:
            connection.execute(
                "DELETE FROM clickup_partner_operations WHERE parent_id = %s",
                (self.parent_id,),
            )
            connection.execute(
                "DELETE FROM clickup_partner_jobs WHERE parent_id = %s",
                (self.parent_id,),
            )

    def pending(self):
        return [
            job
            for job in self.store.pending_jobs(1000)
            if job["parent_id"] == self.parent_id
        ]

    def test_finishing_old_version_preserves_a_new_event(self):
        first = self.store.enqueue(self.parent_id)
        second = self.store.enqueue(self.parent_id)
        self.store.finish(self.parent_id, first)
        self.assertEqual(
            self.pending(), [{"parent_id": self.parent_id, "version": second}]
        )
        self.store.finish(self.parent_id, second)
        self.assertEqual(self.pending(), [])

    def test_parallel_enqueue_increments_versions_atomically(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            versions = list(
                executor.map(lambda _: self.store.enqueue(self.parent_id), range(8))
            )
        self.assertEqual(sorted(versions), list(range(1, 9)))
        self.assertEqual(self.pending()[0]["version"], 8)

    def test_lock_prevents_another_connection_from_processing_the_same_parent(self):
        another = PartnerStore(self.database_url)
        with self.store.store_lock(self.parent_id) as first:
            self.assertTrue(first)
            with another.store_lock(self.parent_id) as second:
                self.assertFalse(second)
        with another.store_lock(self.parent_id) as after_release:
            self.assertTrue(after_release)

    def test_parallel_creation_reservation_has_only_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            reservations = list(
                executor.map(
                    lambda _: self.store.reserve_operation(
                        self.parent_id, "create:onboarding"
                    ),
                    range(8),
                )
            )
        self.assertEqual(reservations.count(True), 1)
        self.assertEqual(
            self.store.operation(self.parent_id, "create:onboarding"),
            {"state": "pending", "result_id": None},
        )

    def test_pending_creation_survives_a_fresh_store_instance(self):
        self.assertTrue(
            self.store.reserve_operation(self.parent_id, "create:onboarding")
        )
        restarted = PartnerStore(self.database_url)
        restarted.initialize()
        self.assertFalse(
            restarted.reserve_operation(self.parent_id, "create:onboarding")
        )
        self.assertEqual(
            restarted.operation(self.parent_id, "create:onboarding")["state"], "pending"
        )

    def test_saved_snapshot_does_not_change_after_template_or_client_answer_changes(
        self,
    ):
        original = {
            "existing_client": False,
            "config": {"description": "Descrição inicial"},
        }
        self.store.job_context(self.parent_id, original)
        edited = {"existing_client": True, "config": {"description": "Texto alterado"}}
        self.assertEqual(self.store.job_context(self.parent_id, edited), original)

    def test_completed_operation_cannot_be_repointed_to_another_card(self):
        self.store.reserve_operation(self.parent_id, "create:onboarding")
        self.store.complete_operation(self.parent_id, "create:onboarding", "first-card")
        with self.assertRaises(PartnerStateError):
            self.store.complete_operation(
                self.parent_id, "create:onboarding", "different-card"
            )
        self.assertEqual(
            self.store.operation(self.parent_id, "create:onboarding"),
            {"state": "done", "result_id": "first-card"},
        )

    def test_retry_defers_job_and_a_new_event_wakes_it(self):
        self.store.enqueue(self.parent_id)
        self.store.retry(self.parent_id, 60, "clickup_timeout")
        self.assertEqual(self.pending(), [])
        next_version = self.store.enqueue(self.parent_id)
        self.assertEqual(
            self.pending(), [{"parent_id": self.parent_id, "version": next_version}]
        )


if __name__ == "__main__":
    unittest.main()
