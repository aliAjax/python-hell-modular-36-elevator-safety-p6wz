import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FlakyRepository(SQLiteRepository):
    """Fails the next `failures_left` atomic writes to simulate write errors."""

    def __init__(self, path, failures_left=0):
        super().__init__(path)
        self.failures_left = failures_left

    def apply_atomic(self, updates, audit_entries):
        if self.failures_left > 0:
            self.failures_left -= 1
            raise sqlite3.OperationalError("simulated write failure")
        return super().apply_atomic(updates, audit_entries)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.tech_a = Actor("tech-a", "maintenance", team="Team-A")
        self.tech_a2 = Actor("tech-a2", "maintenance", team="Team-A")
        self.tech_b = Actor("tech-b", "maintenance", team="Team-B")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="EQ-1"):
        return self.service.create(self.admin, "equipment", {
            "asset_no": asset_no, "equipment_type": "elevator",
            "location": "Tower A", "inspection_interval_days": 365,
        })

    def order(self, equipment, **extra):
        payload = {
            "equipment_id": equipment["id"],
            "work_type": "component_replacement",
            "planned_at": "2026-10-02",
            "part_serial": "SN-1",
            "team": "Team-A",
            "steps": ["open_cover", "replace_part", "calibrate"],
        }
        payload.update(extra)
        return self.service.create(self.admin, "maintenance", payload)

    def started_order(self):
        equipment = self.equipment()
        order = self.order(equipment)
        order = self.service.transition(self.tech_a, order["id"], "start", {})
        return equipment, order

    def transfer(self, actor, order, data, version=None):
        return self.service.transition(actor, order["id"], "transfer", data, version)

    def handover(self, **extra):
        data = {
            "to_team": "Team-B",
            "part_serials": ["SN-1", "SN-2"],
            "completed_steps": ["open_cover", "replace_part"],
        }
        data.update(extra)
        return data

    def test_transfer_updates_order_equipment_and_audit(self):
        equipment, order = self.started_order()
        updated = self.transfer(self.tech_a, order, self.handover(transfer_id="t-1"), order["version"])

        self.assertEqual(updated["status"], "in_progress")
        self.assertEqual(updated["data"]["assigned_team"], "Team-B")
        self.assertEqual(updated["data"]["part_serials"], ["SN-1", "SN-2"])
        self.assertEqual(updated["data"]["completed_steps"], ["open_cover", "replace_part"])
        self.assertEqual(updated["data"]["current_step"], "calibrate")
        self.assertEqual(updated["data"]["last_transfer_id"], "t-1")
        self.assertEqual(updated["data"]["transfer_history"][-1]["from_team"], "Team-A")

        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "suspended")

        order_audit = self.service.audit_log(order["id"])
        transfer_entries = [row for row in order_audit if row["action"] == "transfer"]
        self.assertEqual(len(transfer_entries), 1)
        self.assertEqual(transfer_entries[0]["detail"]["to_team"], "Team-B")
        equipment_audit = self.service.audit_log(equipment["id"])
        suspend_entries = [row for row in equipment_audit if row["action"] == "suspend"]
        self.assertEqual(len(suspend_entries), 1)
        self.assertEqual(suspend_entries[0]["detail"]["reason"], "maintenance_transfer")

    def test_equipment_stays_suspended_during_transfer(self):
        equipment, order = self.started_order()
        self.service.transition(self.admin, equipment["id"], "suspend", {})
        updated = self.transfer(self.tech_a, order, self.handover(), order["version"])
        self.assertEqual(updated["data"]["assigned_team"], "Team-B")
        self.assertEqual(self.service.get(equipment["id"])["status"], "suspended")

    def test_second_transfer_sees_already_transferred(self):
        _, order = self.started_order()
        self.transfer(self.tech_a, order, self.handover(), order["version"])
        # Concurrent loser replays with the stale version it read before.
        with self.assertRaises(ConflictError) as ctx:
            self.transfer(self.tech_a2, order, self.handover(to_team="Team-C"), order["version"])
        self.assertIn("already transferred", str(ctx.exception))
        # Same outcome without an explicit expected_version.
        with self.assertRaises(ConflictError) as ctx:
            self.transfer(self.tech_a2, order, self.handover(to_team="Team-C"))
        self.assertIn("already transferred", str(ctx.exception))

    def test_concurrent_transfer_only_one_wins(self):
        _, order = self.started_order()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def attempt(actor, to_team):
            try:
                barrier.wait(timeout=5)
                results.append(self.transfer(
                    actor, order, self.handover(to_team=to_team), order["version"]
                ))
            except ConflictError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(self.tech_a, "Team-B")),
            threading.Thread(target=attempt, args=(self.tech_a2, "Team-C")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("already transferred", str(errors[0]))
        final = self.service.get(order["id"])
        self.assertIn(final["data"]["assigned_team"], ("Team-B", "Team-C"))

    def test_old_team_cannot_complete_after_transfer(self):
        _, order = self.started_order()
        self.transfer(self.tech_a, order, self.handover(), order["version"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.tech_a, order["id"], "complete", {"completed_at": "2026-10-02T18:00:00Z"}
            )
        completed = self.service.transition(
            self.tech_b, order["id"], "complete", {"completed_at": "2026-10-02T18:00:00Z"}
        )
        self.assertEqual(completed["status"], "completed")

    def test_serial_on_other_open_order_is_rejected_with_reason(self):
        _, order = self.started_order()
        other_equipment = self.equipment("EQ-2")
        other = self.order(other_equipment, part_serial="SN-9", team="Team-C")
        with self.assertRaises(ConflictError) as ctx:
            self.transfer(self.tech_a, order, self.handover(part_serials=["SN-9"]))
        message = str(ctx.exception)
        self.assertIn("SN-9", message)
        self.assertIn(other["id"], message)
        rejected = [
            row for row in self.service.audit_log(order["id"])
            if row["action"] == "transfer_rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn("SN-9", rejected[0]["detail"]["reason"])
        # Order stays with the original team after the rejection.
        self.assertEqual(self.service.get(order["id"])["data"]["assigned_team"], "Team-A")

    def test_serial_on_completed_order_does_not_block(self):
        _, order = self.started_order()
        other_equipment = self.equipment("EQ-2")
        other = self.order(other_equipment, part_serial="SN-9", team="Team-C")
        other = self.service.transition(self.admin, other["id"], "start", {})
        self.service.transition(self.admin, other["id"], "complete", {"completed_at": "2026-10-02T12:00:00Z"})
        updated = self.transfer(self.tech_a, order, self.handover(part_serials=["SN-9"]))
        self.assertIn("SN-9", updated["data"]["part_serials"])

    def test_create_rejects_serial_used_by_open_order(self):
        self.started_order()
        other_equipment = self.equipment("EQ-2")
        with self.assertRaises(ConflictError):
            self.order(other_equipment, part_serial="SN-1", team="Team-C")

    def test_retry_after_write_failure(self):
        flaky = FlakyRepository(Path(self.tmp.name) / "flaky.db", failures_left=1)
        service = DomainService(flaky, RuleEngine())
        equipment = service.create(self.admin, "equipment", {
            "asset_no": "EQ-9", "equipment_type": "elevator",
            "location": "Tower B", "inspection_interval_days": 365,
        })
        order = service.create(self.admin, "maintenance", {
            "equipment_id": equipment["id"], "work_type": "component_replacement",
            "planned_at": "2026-10-02", "part_serial": "SN-1", "team": "Team-A",
        })
        order = service.transition(self.tech_a, order["id"], "start", {})
        with self.assertRaises(sqlite3.OperationalError):
            service.transition(self.tech_a, order["id"], "transfer", self.handover(), order["version"])
        # Failed write left no partial state, so the same request can be retried.
        unchanged = service.get(order["id"])
        self.assertEqual(unchanged["version"], order["version"])
        self.assertEqual(unchanged["data"]["assigned_team"], "Team-A")
        self.assertEqual(service.get(equipment["id"])["status"], "in_service")
        self.assertEqual(
            [row for row in service.audit_log(order["id"]) if row["action"] == "transfer"], []
        )
        retried = service.transition(self.tech_a, order["id"], "transfer", self.handover(), order["version"])
        self.assertEqual(retried["data"]["assigned_team"], "Team-B")
        self.assertEqual(service.get(equipment["id"])["status"], "suspended")

    def test_transfer_id_replay_is_idempotent(self):
        _, order = self.started_order()
        updated = self.transfer(self.tech_a, order, self.handover(transfer_id="t-7"), order["version"])
        replayed = self.transfer(self.tech_a, order, self.handover(transfer_id="t-7"), order["version"])
        self.assertEqual(replayed["version"], updated["version"])
        transfers = [
            row for row in self.service.audit_log(order["id"]) if row["action"] == "transfer"
        ]
        self.assertEqual(len(transfers), 1)

    def test_transfer_requires_handover_fields(self):
        _, order = self.started_order()
        with self.assertRaises(ValidationError):
            self.transfer(self.tech_a, order, {"to_team": "Team-B"})
        with self.assertRaises(ValidationError):
            self.transfer(self.tech_a, order, {
                "to_team": "Team-B", "part_serials": ["SN-1"],
                "completed_steps": ["no_such_step"],
            })


if __name__ == "__main__":
    unittest.main()
