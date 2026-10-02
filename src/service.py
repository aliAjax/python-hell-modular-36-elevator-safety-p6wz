import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if action == "transfer" and self.rules.normalize_kind(entity["kind"]) == "maintenance":
            return self.transfer_maintenance(actor, entity_id, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    @staticmethod
    def _audit_entry(entity_id, actor, action, from_status, to_status, detail):
        return {
            "entity_id": entity_id,
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "detail": detail,
        }

    @staticmethod
    def _transfer_conflict(entity, actor, expected):
        team = entity["data"].get("assigned_team")
        if team and team != actor.effective_team:
            return ConflictError("maintenance order already transferred to team " + str(team))
        return ConflictError(
            "version conflict: expected %s, found %s" % (expected, entity["version"])
        )

    def _maintenance_equipment(self, entity):
        equipment_id = entity["data"].get("equipment_id")
        if not equipment_id:
            return None
        rows = self._lookup("equipment", "id", equipment_id)
        return rows[0] if rows else None

    def _audit_transfer_rejected(self, actor, entity, payload, exc):
        try:
            self.audit.record(
                entity["id"],
                actor,
                "transfer_rejected",
                entity["status"],
                entity["status"],
                {
                    "reason": str(exc),
                    "to_team": payload.get("to_team"),
                    "part_serials": payload.get("part_serials"),
                },
            )
        except Exception:
            pass  # best effort: the rejection audit must not mask the domain error

    def transfer_maintenance(self, actor, entity_id, data=None, expected_version=None):
        """Hand an in-flight maintenance order over to another team.

        The order (assignee, part serials, completed steps), the equipment
        status (kept suspended) and the audit trail are written in one
        transaction, so a failed write leaves no partial state and the caller
        can retry. Passing a unique ``transfer_id`` makes retries idempotent.
        """
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(entity["kind"]) != "maintenance":
            raise InvalidTransition("transfer only applies to maintenance")
        payload = dict(data or {})
        transfer_id = payload.get("transfer_id")
        if transfer_id and entity["data"].get("last_transfer_id") == transfer_id:
            return entity
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if expected != entity["version"]:
            raise self._transfer_conflict(entity, actor, expected)
        try:
            next_status, patch = self.rules.validate_transfer(actor, entity, payload, self._lookup)
        except ConflictError as exc:
            self._audit_transfer_rejected(actor, entity, payload, exc)
            raise
        merged = dict(entity["data"])
        merged.update(patch)
        updates = [
            {"id": entity_id, "expected_version": expected, "status": next_status, "data": merged}
        ]
        audits = [
            self._audit_entry(entity_id, actor, "transfer", entity["status"], next_status, {
                "from_team": entity["data"].get("assigned_team"),
                "to_team": patch["assigned_team"],
                "part_serials": payload.get("part_serials"),
                "completed_steps": payload.get("completed_steps"),
                "transfer_id": transfer_id,
            })
        ]
        equipment = self._maintenance_equipment(entity)
        if equipment and entity["status"] == "in_progress" and equipment["status"] == "in_service":
            updates.append({
                "id": equipment["id"],
                "expected_version": equipment["version"],
                "status": "suspended",
                "data": dict(equipment["data"]),
            })
            audits.append(self._audit_entry(
                equipment["id"], actor, "suspend", "in_service", "suspended",
                {"reason": "maintenance_transfer", "maintenance_id": entity_id},
            ))
        try:
            self.repository.apply_atomic(updates, audits)
        except ConflictError:
            current = self.repository.get_entity(entity_id)
            if current:
                raise self._transfer_conflict(current, actor, expected)
            raise
        return self.repository.get_entity(entity_id)

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
