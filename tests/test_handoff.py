import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.team_a = Actor("alice", "maintenance", team="team-a")
        self.team_b = Actor("bob", "maintenance", team="team-b")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, actor, kind, data):
        return self.service.create(actor, kind, data)

    def act(self, actor, entity, action, data=None, version=None):
        return self.service.transition(actor, entity["id"], action, data or {}, version)

    def setup_maintenance_in_progress(self, part_serial="SN-001"):
        equipment = self.create(self.admin, "equipment", {
            "asset_no": "E-100", "equipment_type": "elevator",
            "location": "Tower A", "inspection_interval_days": 365,
        })
        equipment = self.act(self.admin, equipment, "suspend", {})
        maintenance = self.create(self.admin, "maintenance", {
            "equipment_id": equipment["id"], "work_type": "component_replacement",
            "planned_at": "2026-10-01T09:00:00Z", "part_serial": part_serial,
        })
        maintenance = self.act(self.team_a, maintenance, "start", {})
        return equipment, maintenance

    def test_full_handoff_flow(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        self.assertEqual(maintenance["status"], "in_progress")

        maintenance = self.act(self.team_a, maintenance, "handoff", {
            "part_serial": "SN-001",
            "completed_steps": ["remove cover", "disconnect wiring"],
            "to_team": "team-b",
            "reason": "shift change",
        })
        self.assertEqual(maintenance["status"], "handing_over")
        self.assertEqual(maintenance["data"]["handoff"]["to_team"], "team-b")
        self.assertEqual(maintenance["data"]["handoff"]["status"], "pending")
        self.assertEqual(maintenance["data"]["completed_steps"], ["remove cover", "disconnect wiring"])

        equipment = self.service.get(equipment["id"])
        self.assertEqual(equipment["status"], "suspended")
        self.assertEqual(equipment["data"]["last_handoff"], maintenance["id"])

        maintenance = self.act(self.team_b, maintenance, "accept", {})
        self.assertEqual(maintenance["status"], "in_progress")
        self.assertEqual(maintenance["data"]["assigned_to"], "team-b")
        self.assertEqual(maintenance["data"]["handoff"]["status"], "accepted")

        maintenance = self.act(self.team_b, maintenance, "complete", {"completed_at": "2026-10-02T17:00:00Z"})
        self.assertEqual(maintenance["status"], "completed")

    def test_handoff_requires_suspended_equipment(self):
        equipment = self.create(self.admin, "equipment", {
            "asset_no": "E-200", "equipment_type": "elevator",
            "location": "Tower B", "inspection_interval_days": 365,
        })
        maintenance = self.create(self.admin, "maintenance", {
            "equipment_id": equipment["id"], "work_type": "component_replacement",
            "planned_at": "2026-10-01T09:00:00Z", "part_serial": "SN-002",
        })
        with self.assertRaises(ConflictError):
            self.act(self.team_a, maintenance, "start", {})

    def test_handoff_requires_part_serial(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        with self.assertRaises(ValidationError):
            self.act(self.team_a, maintenance, "handoff", {
                "completed_steps": ["step1"], "to_team": "team-b",
            })

    def test_handoff_requires_completed_steps(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        with self.assertRaises(ValidationError):
            self.act(self.team_a, maintenance, "handoff", {
                "part_serial": "SN-001", "to_team": "team-b",
            })

    def test_handoff_requires_to_team(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        with self.assertRaises(ValidationError):
            self.act(self.team_a, maintenance, "handoff", {
                "part_serial": "SN-001", "completed_steps": ["step1"],
            })

    def test_concurrent_handoff_version_conflict(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        handoff_data = {
            "part_serial": "SN-001", "completed_steps": ["step1"],
            "to_team": "team-b", "reason": "shift change",
        }
        v1 = maintenance["version"]
        first = self.act(self.team_a, maintenance, "handoff", handoff_data, version=v1)
        self.assertEqual(first["status"], "handing_over")
        with self.assertRaises(ConflictError):
            self.act(self.team_a, maintenance, "handoff", handoff_data, version=v1)

    def test_old_team_cannot_complete_after_handoff(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        maintenance = self.act(self.team_a, maintenance, "handoff", {
            "part_serial": "SN-001", "completed_steps": ["step1"], "to_team": "team-b",
        })
        with self.assertRaises(InvalidTransition):
            self.act(self.team_a, maintenance, "complete", {"completed_at": "2026-10-02T17:00:00Z"})

        maintenance = self.act(self.team_b, maintenance, "accept", {})
        with self.assertRaises(PermissionDenied):
            self.act(self.team_a, maintenance, "complete", {"completed_at": "2026-10-02T17:00:00Z"})

    def test_serial_number_uniqueness(self):
        equipment, maint1 = self.setup_maintenance_in_progress(part_serial="SN-DUP")
        maint1 = self.act(self.team_a, maint1, "handoff", {
            "part_serial": "SN-DUP", "completed_steps": ["step1"], "to_team": "team-b",
        })

        equipment2 = self.create(self.admin, "equipment", {
            "asset_no": "E-300", "equipment_type": "escalator",
            "location": "Mall", "inspection_interval_days": 180,
        })
        equipment2 = self.act(self.admin, equipment2, "suspend", {})
        maint2 = self.create(self.admin, "maintenance", {
            "equipment_id": equipment2["id"], "work_type": "component_replacement",
            "planned_at": "2026-10-03T09:00:00Z", "part_serial": "SN-DUP",
        })
        maint2 = self.act(self.team_a, maint2, "start", {})
        with self.assertRaises(ConflictError):
            self.act(self.team_a, maint2, "handoff", {
                "part_serial": "SN-DUP", "completed_steps": ["step1"], "to_team": "team-b",
            })

    def test_handoff_retry_after_failure(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        handoff_data = {
            "part_serial": "SN-001", "completed_steps": ["step1"], "to_team": "team-b",
        }
        v1 = maintenance["version"]
        first = self.act(self.team_a, maintenance, "handoff", handoff_data, version=v1)
        self.assertEqual(first["status"], "handing_over")
        with self.assertRaises(ConflictError):
            self.act(self.team_a, maintenance, "handoff", handoff_data, version=v1)
        retry = self.service.get(maintenance["id"])
        self.assertEqual(retry["status"], "handing_over")

    def test_handoff_writes_audit_entries(self):
        equipment, maintenance = self.setup_maintenance_in_progress()
        maintenance = self.act(self.team_a, maintenance, "handoff", {
            "part_serial": "SN-001", "completed_steps": ["step1"], "to_team": "team-b",
        })
        audit = self.service.audit_log(entity_id=maintenance["id"])
        actions = [entry["action"] for entry in audit]
        self.assertIn("handoff", actions)
        equipment_audit = self.service.audit_log(entity_id=equipment["id"])
        equipment_actions = [entry["action"] for entry in equipment_audit]
        self.assertIn("handoff_ref", equipment_actions)


if __name__ == "__main__":
    unittest.main()
