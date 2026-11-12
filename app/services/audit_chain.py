"""Tamper-evident audit chain (v1.6.1 plan D28-D36, feature eight).

A separate hash ledger, ``audit_chain_entries``, links every governance event, review decision, approval decision,
retention tombstone and checkpoint (hashes and identifiers only, never content). Retention purges never delete it.

Writing (D31, D32): entries are appended **inside the source transaction, just before commit**, by SQLAlchemy session
hooks registered when this module is imported:

- ``after_flush`` collects every newly inserted ``GovernanceEvent``, ``RetentionTombstone`` and already-decided
  ``ApprovalEvent`` (whatever code path added it); ``track()`` adds the rows whose *update* is the audited fact (a
  review decision, a pending approval that is decided or expires) and new checkpoints.
- ``before_commit`` flushes, takes ``pg_advisory_xact_lock`` on Postgres (held until commit; SQLite needs none), reads
  the head, and adds one entry per collected row (the first append also writes a ``genesis`` entry describing the
  legacy rows). Tombstones get ``chain_version`` / ``prev_hash`` / ``entry_hash`` written back. If anything fails the
  commit fails: there is never an event without its entry.

Hash definition ``rh-chain/1`` (D29): ``payload_digest = sha256(canonical_json(payload))`` with the payload fields in
``PAYLOAD_FIELDS``; ``entry_hash = sha256(canonical_json({chain_version, seq, entry_type, ref_table, ref_id,
tenant_id, run_id, payload_digest, prev_hash, created_at}))``; the first ``prev_hash`` is 64 zeros. Changing a payload
field list requires a new ``chain_version``.

What this guarantees (D36): later modification, deletion or insertion becomes *detectable* by ``verify()``, provided
the verifier holds a checkpoint (or the public key and signature) kept outside this database. Someone holding both
database write access and the signing key can rewrite the whole chain, so keep the private key apart from the
database and export checkpoints regularly. There is no external anchoring in this release.

The chain only records: it never changes a verdict, releases a run or alters the source rows (monitor-only).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.digests import canonical_json, iso_utc, sha256_hex
from app.models.approval_event import ApprovalEvent
from app.models.audit_chain import AuditChainCheckpoint, AuditChainEntry
from app.models.evaluation_review import EvaluationReview
from app.models.governance_event import GovernanceEvent
from app.models.retention import RetentionTombstone
from app.observability.metrics import AUDIT_CHAIN_ENTRIES_TOTAL, AUDIT_CHAIN_VERIFY_FAILURES_TOTAL

logger = logging.getLogger(__name__)

CHAIN_VERSION = "rh-chain/1"
ZERO_HASH = "0" * 64
LOCK_KEY = 5_206_143_317_001  # pg_advisory_xact_lock key of the single global chain (Q6)

GENESIS = "genesis"
GOVERNANCE_EVENT = "governance_event"
REVIEW_DECISION = "review_decision"
APPROVAL_DECISION = "approval_decision"
TOMBSTONE = "tombstone"
CHECKPOINT = "checkpoint"
ENTRY_TYPES = (GENESIS, GOVERNANCE_EVENT, REVIEW_DECISION, APPROVAL_DECISION, TOMBSTONE, CHECKPOINT)

REF_TABLES = {
    GOVERNANCE_EVENT: "governance_events",
    REVIEW_DECISION: "evaluation_reviews",
    APPROVAL_DECISION: "approval_events",
    TOMBSTONE: "retention_tombstones",
    CHECKPOINT: "audit_chain_checkpoints",
}

# D29: the payload fields of each entry type (documented in docs/security.md). Free text is stored as a digest.
PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    GOVERNANCE_EVENT: ("id", "tenant_id", "user_id", "run_id", "event_type", "decision", "reason", "limit_value", "used",
                       "remaining", "details", "created_at"),
    REVIEW_DECISION: ("review_id", "run_id", "tenant_id", "label", "label_categories", "reviewer", "reviewer_key_id",
                      "reason_digest", "decided_at"),
    APPROVAL_DECISION: ("id", "run_id", "status", "reviewer", "reason_digest", "created_at"),
    TOMBSTONE: ("tombstone_id", "run_id", "tenant_id", "purge_id", "content_digest", "record_counts"),
    CHECKPOINT: ("checkpoint_id", "chain_version", "through_seq", "head_hash", "entry_count", "period_start", "period_end",
                 "prev_checkpoint_hash", "algorithm", "key_id"),
    GENESIS: ("chain_version", "governance_events", "retention_tombstones", "evaluation_reviews_decided",
              "approval_decisions", "enabled_at"),
}

# D35 result kinds. ``legacy_unchained`` is informational (rows older than the genesis entry).
OK = "ok"
HASH_MISMATCH = "hash_mismatch"
CHAIN_BREAK = "chain_break"
MISSING_SOURCE = "missing_source"
UNCHAINED_RECORD = "unchained_record"
CHECKPOINT_SIGNATURE_INVALID = "checkpoint_signature_invalid"
LEGACY_UNCHAINED = "legacy_unchained"
FAILURE_KINDS = (HASH_MISMATCH, CHAIN_BREAK, MISSING_SOURCE, UNCHAINED_RECORD, CHECKPOINT_SIGNATURE_INVALID)

_PENDING = "audit_chain_pending"
_BUSY = "audit_chain_busy"
_BATCH = 1000
_DETAIL_LIMIT = 50


class AuditChainError(Exception):
    """A refused audit-chain operation (``code`` is the API error code)."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def text_digest(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def payload_for(entry_type: str, row: Any) -> dict[str, Any]:
    """The D29 payload of a source row (values, not yet digested)."""
    if entry_type == GOVERNANCE_EVENT:
        return {
            "id": row.id, "tenant_id": row.tenant_id or "", "user_id": row.user_id or "", "run_id": row.run_id or "",
            "event_type": row.event_type, "decision": row.decision, "reason": row.reason or "",
            "limit_value": int(row.limit_value or 0), "used": int(row.used or 0), "remaining": int(row.remaining or 0),
            "details": dict(row.details or {}), "created_at": iso_utc(row.created_at),
        }
    if entry_type == REVIEW_DECISION:
        return {
            "review_id": row.review_id, "run_id": row.run_id, "tenant_id": row.tenant_id or "", "label": row.label or "",
            "label_categories": [str(item) for item in (row.label_categories or [])], "reviewer": row.reviewer or "",
            "reviewer_key_id": row.reviewer_key_id or "", "reason_digest": text_digest(row.reason),
            "decided_at": iso_utc(row.decided_at),
        }
    if entry_type == APPROVAL_DECISION:
        return {
            "id": row.id, "run_id": row.run_id, "status": row.status, "reviewer": row.reviewer or "",
            "reason_digest": text_digest(row.reason), "created_at": iso_utc(row.created_at),
        }
    if entry_type == TOMBSTONE:
        return {
            "tombstone_id": row.tombstone_id, "run_id": row.run_id, "tenant_id": row.tenant_id or "",
            "purge_id": row.purge_id, "content_digest": row.content_digest, "record_counts": dict(row.record_counts or {}),
        }
    if entry_type == CHECKPOINT:
        return checkpoint_fields(row)
    raise ValueError(f"no payload for {entry_type}")


def payload_digest(entry_type: str, row: Any) -> str:
    return sha256_hex(payload_for(entry_type, row))


def compute_entry_hash(
    *, seq: int, entry_type: str, ref_table: str, ref_id: str, tenant_id: str, run_id: str, payload_digest: str,
    prev_hash: str, created_at: datetime | str | None, chain_version: str = CHAIN_VERSION,
) -> str:
    return sha256_hex(
        {
            "chain_version": chain_version, "seq": int(seq), "entry_type": entry_type, "ref_table": ref_table,
            "ref_id": ref_id, "tenant_id": tenant_id, "run_id": run_id, "payload_digest": payload_digest,
            "prev_hash": prev_hash, "created_at": created_at if isinstance(created_at, str) else iso_utc(created_at),
        }
    )


def entry_hash_of(entry: AuditChainEntry) -> str:
    return compute_entry_hash(
        seq=entry.seq, entry_type=entry.entry_type, ref_table=entry.ref_table or "", ref_id=entry.ref_id or "",
        tenant_id=entry.tenant_id or "", run_id=entry.run_id or "", payload_digest=entry.payload_digest,
        prev_hash=entry.prev_hash, created_at=entry.created_at, chain_version=entry.chain_version,
    )


def checkpoint_fields(checkpoint: Any) -> dict[str, Any]:
    """The canonical fields a checkpoint signature covers (D33)."""
    return {
        "checkpoint_id": checkpoint.checkpoint_id, "chain_version": CHAIN_VERSION, "through_seq": int(checkpoint.through_seq),
        "head_hash": checkpoint.head_hash, "entry_count": int(checkpoint.entry_count),
        "period_start": iso_utc(checkpoint.period_start), "period_end": iso_utc(checkpoint.period_end),
        "prev_checkpoint_hash": checkpoint.prev_checkpoint_hash or "", "algorithm": checkpoint.algorithm or "ed25519",
        "key_id": checkpoint.key_id or "",
    }


# ---------------------------------------------------------------------------
# same-transaction writing (session hooks)
# ---------------------------------------------------------------------------


def _info(session: Any) -> dict:
    return getattr(session, "sync_session", session).info


def track(session: Any, entry_type: str, row: Any) -> None:
    """Chain ``row`` when ``session`` commits (review decisions, approval decisions / expiries, checkpoints).

    New governance events and tombstones are collected automatically and need no call."""
    if entry_type not in REF_TABLES:
        raise ValueError(f"unknown audit chain entry type: {entry_type}")
    _info(session).setdefault(_PENDING, []).append((entry_type, row))


def _enabled() -> bool:
    try:
        return bool(get_settings().audit_chain_enabled)
    except Exception:  # pragma: no cover - settings errors surface elsewhere
        return True


@event.listens_for(Session, "before_flush")
def _stamp_created_at(session: Session, flush_context: Any, instances: Any) -> None:
    """Hashed timestamps are set in Python so they are known without a refresh (server defaults are not)."""
    for obj in session.new:
        if isinstance(obj, (GovernanceEvent, ApprovalEvent)) and getattr(obj, "created_at", None) is None:
            obj.created_at = utcnow()


@event.listens_for(Session, "after_flush")
def _collect_inserts(session: Session, flush_context: Any) -> None:
    if session.info.get(_BUSY):
        return
    found = [obj for obj in session.new if _auto_kind(obj)]
    if not found:
        return
    pending = session.info.setdefault(_PENDING, [])
    for obj in sorted(found, key=lambda item: (type(item).__name__, item.id or 0)):
        pending.append((_auto_kind(obj), obj))


def _auto_kind(obj: Any) -> str:
    """Rows chained on insert, whatever code path added them. A new approval row that is already a decision (not
    ``pending``) is chained too; pending rows are chained when they are decided or expire (``track()``)."""
    if isinstance(obj, GovernanceEvent):
        return GOVERNANCE_EVENT
    if isinstance(obj, RetentionTombstone):
        return TOMBSTONE
    if isinstance(obj, ApprovalEvent) and obj.status != "pending":
        return APPROVAL_DECISION
    return ""


@event.listens_for(Session, "before_commit")
def _append_before_commit(session: Session) -> None:
    if session.info.get(_BUSY):
        return
    if not session.info.get(_PENDING) and not any(_auto_kind(obj) for obj in session.new):
        return
    session.flush()
    pending = session.info.pop(_PENDING, [])
    if not pending or not _enabled():
        return
    session.info[_BUSY] = True
    try:
        _append_all(session, pending)
        session.flush()
    finally:
        session.info.pop(_BUSY, None)


@event.listens_for(Session, "after_rollback")
def _clear_on_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


@event.listens_for(Session, "after_soft_rollback")
def _clear_on_soft_rollback(session: Session, previous_transaction: Any) -> None:
    session.info.pop(_PENDING, None)


def _ref_of(entry_type: str, row: Any) -> tuple[str, str, str]:
    """``(ref_id, tenant_id, run_id)`` of a source row."""
    if entry_type == GOVERNANCE_EVENT:
        return str(row.id), row.tenant_id or "", row.run_id or ""
    if entry_type == REVIEW_DECISION:
        return row.review_id, row.tenant_id or "", row.run_id or ""
    if entry_type == APPROVAL_DECISION:
        return str(row.id), "", row.run_id or ""
    if entry_type == TOMBSTONE:
        return row.tombstone_id, row.tenant_id or "", row.run_id or ""
    if entry_type == CHECKPOINT:
        return row.checkpoint_id, "", ""
    raise ValueError(entry_type)


def _new_entry(seq: int, prev_hash: str, entry_type: str, ref_table: str, ref_id: str, tenant_id: str, run_id: str,
               digest: str, now: datetime) -> AuditChainEntry:
    entry = AuditChainEntry(
        seq=seq, entry_id=f"ace_{uuid.uuid4().hex}", entry_type=entry_type, ref_table=ref_table, ref_id=ref_id,
        tenant_id=tenant_id, run_id=run_id, payload_digest=digest, prev_hash=prev_hash, chain_version=CHAIN_VERSION,
        created_at=now,
    )
    entry.entry_hash = entry_hash_of(entry)
    return entry


def _ids(pending: list[tuple[str, Any]], entry_type: str) -> list[int]:
    return [row.id for kind, row in pending if kind == entry_type and getattr(row, "id", None) is not None]


def _genesis(session: Session, pending: list[tuple[str, Any]], now: datetime) -> AuditChainEntry:
    """D30: counts and maximum ids of the rows that existed before the chain (excluding this transaction's rows)."""

    def count_max(model: Any, *where: Any) -> tuple[int, int]:
        row = session.execute(select(func.count(), func.max(model.id)).where(*where)).one()
        return int(row[0] or 0), int(row[1] or 0)

    ge_ids, tb_ids, ap_ids = _ids(pending, GOVERNANCE_EVENT), _ids(pending, TOMBSTONE), _ids(pending, APPROVAL_DECISION)
    rv_ids = _ids(pending, REVIEW_DECISION)
    ge = count_max(GovernanceEvent, GovernanceEvent.id.notin_(ge_ids or [-1]))
    tb = count_max(RetentionTombstone, RetentionTombstone.id.notin_(tb_ids or [-1]))
    ap = count_max(ApprovalEvent, ApprovalEvent.status != "pending", ApprovalEvent.id.notin_(ap_ids or [-1]))
    rv = count_max(EvaluationReview, EvaluationReview.decided_at.is_not(None), EvaluationReview.id.notin_(rv_ids or [-1]))
    payload = {
        "chain_version": CHAIN_VERSION,
        "governance_events": {"count": ge[0], "max_id": ge[1]},
        "retention_tombstones": {"count": tb[0], "max_id": tb[1]},
        "evaluation_reviews_decided": {"count": rv[0], "max_id": rv[1]},
        "approval_decisions": {"count": ap[0], "max_id": ap[1]},
        "enabled_at": iso_utc(now),
    }
    ref_id = f"ge:{ge[1]};tb:{tb[1]};ap:{ap[1]}"
    return _new_entry(1, ZERO_HASH, GENESIS, "", ref_id, "", "", sha256_hex(payload), now)


def parse_genesis_ref(ref_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (ref_id or "").split(";"):
        key, _, value = part.partition(":")
        if key and value.isdigit():
            out[key] = int(value)
    return out


def _append_all(session: Session, pending: list[tuple[str, Any]]) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
    head = session.execute(select(AuditChainEntry).order_by(AuditChainEntry.seq.desc()).limit(1)).scalar_one_or_none()
    now = utcnow()
    if head is None:
        head = _genesis(session, pending, now)
        session.add(head)
        AUDIT_CHAIN_ENTRIES_TOTAL.labels(entry_type=GENESIS).inc()
    seen: set[tuple[str, int]] = set()
    for entry_type, row in pending:
        key = (entry_type, id(row))
        if key in seen or row in session.deleted:
            continue
        seen.add(key)
        ref_id, tenant_id, run_id = _ref_of(entry_type, row)
        entry = _new_entry(head.seq + 1, head.entry_hash, entry_type, REF_TABLES[entry_type], ref_id, tenant_id, run_id,
                           payload_digest(entry_type, row), now)
        session.add(entry)
        if entry_type == TOMBSTONE:
            row.chain_version, row.prev_hash, row.entry_hash = CHAIN_VERSION, entry.prev_hash, entry.entry_hash
        AUDIT_CHAIN_ENTRIES_TOTAL.labels(entry_type=entry_type).inc()
        head = entry


# ---------------------------------------------------------------------------
# signing keys (D33)
# ---------------------------------------------------------------------------


def key_id_for(public_key: ed25519.Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def load_signing_key(settings: Settings) -> ed25519.Ed25519PrivateKey | None:
    """The Ed25519 private key from ``AUDIT_CHAIN_SIGNING_KEY_FILE`` (preferred) or ``AUDIT_CHAIN_SIGNING_KEY``."""
    pem = b""
    if settings.audit_chain_signing_key_file:
        try:
            pem = Path(settings.audit_chain_signing_key_file).read_bytes()
        except OSError as exc:
            raise AuditChainError("audit_chain_signing_key_unreadable", f"Cannot read the signing key file: {exc.strerror}.", 500) from exc
    elif settings.audit_chain_signing_key.get_secret_value():
        pem = settings.audit_chain_signing_key.get_secret_value().encode("utf-8")
    if not pem:
        return None
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise AuditChainError("audit_chain_signing_key_invalid", "The signing key is not an unencrypted PEM private key.", 500) from exc
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise AuditChainError("audit_chain_signing_key_invalid", "The signing key must be an Ed25519 key.", 500)
    return key


def parse_public_keys(pem_text: str) -> dict[str, ed25519.Ed25519PublicKey]:
    keys: dict[str, ed25519.Ed25519PublicKey] = {}
    marker = "-----END PUBLIC KEY-----"
    for block in pem_text.split(marker):
        if "-----BEGIN PUBLIC KEY-----" not in block:
            continue
        try:
            key = serialization.load_pem_public_key((block.strip() + "\n" + marker + "\n").encode("utf-8"))
        except ValueError:
            continue
        if isinstance(key, ed25519.Ed25519PublicKey):
            keys[key_id_for(key)] = key
    return keys


def load_public_keys(settings: Settings, extra_file: str | None = None) -> dict[str, ed25519.Ed25519PublicKey]:
    """Keys accepted when verifying: ``AUDIT_CHAIN_PUBLIC_KEYS_FILE`` (or ``extra_file``) plus the current signing key."""
    keys: dict[str, ed25519.Ed25519PublicKey] = {}
    for path in (extra_file, settings.audit_chain_public_keys_file):
        if path:
            try:
                keys.update(parse_public_keys(Path(path).read_text(encoding="utf-8")))
            except OSError as exc:
                raise AuditChainError("audit_chain_public_keys_unreadable", f"Cannot read the public keys file: {exc.strerror}.", 500) from exc
    if extra_file:
        return keys  # an explicit key file is the verifier's own trust root
    try:
        private = load_signing_key(settings)
    except AuditChainError:
        private = None
    if private is not None:
        public = private.public_key()
        keys.setdefault(key_id_for(public), public)
    return keys


# ---------------------------------------------------------------------------
# checkpoints (D33, D34)
# ---------------------------------------------------------------------------


@dataclass
class CheckpointResult:
    checkpoint: AuditChainCheckpoint | None
    created: bool
    reason: str = ""


async def latest_checkpoint(session: AsyncSession) -> AuditChainCheckpoint | None:
    return await session.scalar(select(AuditChainCheckpoint).order_by(AuditChainCheckpoint.through_seq.desc(), AuditChainCheckpoint.id.desc()).limit(1))


async def head(session: AsyncSession) -> AuditChainEntry | None:
    return await session.scalar(select(AuditChainEntry).order_by(AuditChainEntry.seq.desc()).limit(1))


async def create_checkpoint(session: AsyncSession, settings: Settings, *, actor: str, now: datetime | None = None) -> CheckpointResult:
    """Sign the current head. No new entries since the last checkpoint -> nothing is written (idempotent)."""
    if not settings.audit_chain_enabled:
        raise AuditChainError("audit_chain_disabled", "The audit chain is disabled (AUDIT_CHAIN_ENABLED=false).", 409)
    current = await head(session)
    if current is None:
        return CheckpointResult(None, False, "empty_chain")
    previous = await latest_checkpoint(session)
    if previous is not None and current.entry_type == CHECKPOINT and current.ref_id == previous.checkpoint_id:
        return CheckpointResult(previous, False, "no_new_entries")
    key = load_signing_key(settings)
    now = now or utcnow()
    genesis = await session.scalar(select(AuditChainEntry).where(AuditChainEntry.seq == 1))
    checkpoint = AuditChainCheckpoint(
        checkpoint_id=f"acp_{uuid.uuid4().hex}", through_seq=current.seq, head_hash=current.entry_hash, entry_count=current.seq,
        period_start=previous.period_end if previous is not None else (genesis.created_at if genesis is not None else None),
        period_end=now, prev_checkpoint_hash=previous.checkpoint_hash if previous is not None else "", algorithm="ed25519",
        key_id=key_id_for(key.public_key()) if key is not None else "", status="signed" if key is not None else "unsigned",
        created_by=actor,
    )
    fields = checkpoint_fields(checkpoint)
    checkpoint.checkpoint_hash = sha256_hex(fields)
    checkpoint.signature = base64.b64encode(key.sign(canonical_json(fields).encode("utf-8"))).decode("ascii") if key is not None else ""
    session.add(checkpoint)
    await session.flush()
    track(session, CHECKPOINT, checkpoint)
    await session.commit()
    return CheckpointResult(checkpoint, True)


def verify_checkpoint_signature(checkpoint: AuditChainCheckpoint, keys: dict[str, ed25519.Ed25519PublicKey]) -> bool:
    key = keys.get(checkpoint.key_id or "")
    if key is None or not checkpoint.signature:
        return False
    try:
        key.verify(base64.b64decode(checkpoint.signature, validate=True), canonical_json(checkpoint_fields(checkpoint)).encode("utf-8"))
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


def export_checkpoint(checkpoint: AuditChainCheckpoint) -> dict[str, Any]:
    """A self-contained JSON statement to store outside the database (D34 ``--export-checkpoint``)."""
    return {
        "type": "rewardhacking.audit_chain.checkpoint",
        "fields": checkpoint_fields(checkpoint),
        "checkpoint_hash": checkpoint.checkpoint_hash,
        "status": checkpoint.status,
        "signature": checkpoint.signature,
        "created_by": checkpoint.created_by,
    }


def checkpoint_to_dict(checkpoint: AuditChainCheckpoint) -> dict[str, Any]:
    return {**checkpoint_fields(checkpoint), "checkpoint_hash": checkpoint.checkpoint_hash, "status": checkpoint.status,
            "signature": checkpoint.signature, "created_by": checkpoint.created_by, "created_at": iso_utc(checkpoint.created_at)}


def entry_to_dict(entry: AuditChainEntry) -> dict[str, Any]:
    return {
        "seq": entry.seq, "entry_id": entry.entry_id, "entry_type": entry.entry_type, "ref_table": entry.ref_table,
        "ref_id": entry.ref_id, "tenant_id": entry.tenant_id, "run_id": entry.run_id, "payload_digest": entry.payload_digest,
        "prev_hash": entry.prev_hash, "entry_hash": entry.entry_hash, "chain_version": entry.chain_version,
        "created_at": iso_utc(entry.created_at),
    }


# ---------------------------------------------------------------------------
# status and listing
# ---------------------------------------------------------------------------


async def status(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    current = await head(session)
    genesis = await session.scalar(select(AuditChainEntry).where(AuditChainEntry.seq == 1))
    counts = {row[0]: int(row[1]) for row in (await session.execute(
        select(AuditChainEntry.entry_type, func.count()).group_by(AuditChainEntry.entry_type))).all()}
    latest = await latest_checkpoint(session)
    try:
        key = load_signing_key(settings)
        signing = {"configured": key is not None, "key_id": key_id_for(key.public_key()) if key is not None else "", "error": ""}
    except AuditChainError as exc:
        signing = {"configured": False, "key_id": "", "error": exc.code}
    return {
        "enabled": bool(settings.audit_chain_enabled),
        "chain_version": CHAIN_VERSION,
        "entry_count": int(sum(counts.values())),
        "entries_by_type": counts,
        "head_seq": current.seq if current is not None else 0,
        "head_hash": current.entry_hash if current is not None else "",
        "head_created_at": iso_utc(current.created_at) if current is not None else None,
        "genesis": None if genesis is None else {"created_at": iso_utc(genesis.created_at), "legacy_max_ids": parse_genesis_ref(genesis.ref_id)},
        "latest_checkpoint": checkpoint_to_dict(latest) if latest is not None else None,
        "signing": signing,
        "verify_max_entries": int(settings.audit_chain_verify_max_entries),
    }


async def list_entries(session: AsyncSession, *, from_seq: int | None = None, entry_type: str | None = None, run_id: str | None = None,
                       limit: int = 100) -> list[AuditChainEntry]:
    stmt = select(AuditChainEntry)
    if from_seq is not None:
        stmt = stmt.where(AuditChainEntry.seq >= from_seq)
    if entry_type:
        stmt = stmt.where(AuditChainEntry.entry_type == entry_type)
    if run_id:
        stmt = stmt.where(AuditChainEntry.run_id == run_id)
    order = AuditChainEntry.seq.asc() if from_seq is not None else AuditChainEntry.seq.desc()
    return list((await session.scalars(stmt.order_by(order).limit(max(1, min(limit, 500))))).all())


async def list_checkpoints(session: AsyncSession, *, limit: int = 50) -> list[AuditChainCheckpoint]:
    stmt = select(AuditChainCheckpoint).order_by(AuditChainCheckpoint.through_seq.desc(), AuditChainCheckpoint.id.desc())
    return list((await session.scalars(stmt.limit(max(1, min(limit, 500))))).all())


# ---------------------------------------------------------------------------
# verification (D35)
# ---------------------------------------------------------------------------


class _Report:
    def __init__(self) -> None:
        self.issues: dict[str, int] = {}
        self.details: list[dict[str, Any]] = []
        self.info: dict[str, int] = {"explained_by_tombstone": 0, "superseded": 0, LEGACY_UNCHAINED: 0, "unsigned_checkpoints": 0}

    def add(self, kind: str, **detail: Any) -> None:
        self.issues[kind] = self.issues.get(kind, 0) + 1
        if len(self.details) < _DETAIL_LIMIT:
            self.details.append({"kind": kind, **detail})


async def _rows_by(session: AsyncSession, model: Any, column: Any, values: Iterable[Any]) -> dict[Any, Any]:
    values = list(set(values))
    out: dict[Any, Any] = {}
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        for row in (await session.scalars(select(model).where(column.in_(chunk)))).all():
            out[getattr(row, column.key)] = row
    return out


async def _latest_seq_by_ref(session: AsyncSession, entry_type: str, refs: Iterable[str]) -> dict[str, int]:
    refs = list(set(refs))
    out: dict[str, int] = {}
    for start in range(0, len(refs), 500):
        chunk = refs[start:start + 500]
        rows = await session.execute(
            select(AuditChainEntry.ref_id, func.max(AuditChainEntry.seq))
            .where(AuditChainEntry.entry_type == entry_type, AuditChainEntry.ref_id.in_(chunk))
            .group_by(AuditChainEntry.ref_id)
        )
        out.update({ref: int(seq) for ref, seq in rows.all()})
    return out


async def _check_sources(session: AsyncSession, entries: list[AuditChainEntry], report: _Report) -> None:
    by_type: dict[str, list[AuditChainEntry]] = {}
    for entry in entries:
        by_type.setdefault(entry.entry_type, []).append(entry)
    run_ids = {entry.run_id for entry in entries if entry.run_id}
    tombstones = await _rows_by(session, RetentionTombstone, RetentionTombstone.run_id, run_ids) if run_ids else {}

    async def check(entry_type: str, model: Any, column: Any, convert: Any, latest_only: bool) -> None:
        items = by_type.get(entry_type, [])
        if not items:
            return
        keys = [convert(entry.ref_id) for entry in items]
        rows = await _rows_by(session, model, column, [key for key in keys if key is not None])
        latest = await _latest_seq_by_ref(session, entry_type, [entry.ref_id for entry in items]) if latest_only else {}
        for entry, key in zip(items, keys):
            row = rows.get(key)
            if row is None:
                if entry.run_id and entry.run_id in tombstones:
                    report.info["explained_by_tombstone"] += 1
                else:
                    report.add(MISSING_SOURCE, seq=entry.seq, entry_type=entry_type, ref_id=entry.ref_id)
                continue
            if latest_only and latest.get(entry.ref_id, entry.seq) != entry.seq:
                report.info["superseded"] += 1  # a later decision of the same row is the one compared
                continue
            if payload_digest(entry_type, row) != entry.payload_digest:
                report.add(HASH_MISMATCH, seq=entry.seq, entry_type=entry_type, ref_id=entry.ref_id, field="payload_digest")
            if entry_type == TOMBSTONE and (row.chain_version, row.prev_hash, row.entry_hash) != (entry.chain_version, entry.prev_hash, entry.entry_hash):
                report.add(HASH_MISMATCH, seq=entry.seq, entry_type=entry_type, ref_id=entry.ref_id, field="tombstone_chain_fields")

    def as_int(value: str) -> int | None:
        return int(value) if value.isdigit() else None

    await check(GOVERNANCE_EVENT, GovernanceEvent, GovernanceEvent.id, as_int, False)
    await check(REVIEW_DECISION, EvaluationReview, EvaluationReview.review_id, str, True)
    await check(APPROVAL_DECISION, ApprovalEvent, ApprovalEvent.id, as_int, True)
    await check(TOMBSTONE, RetentionTombstone, RetentionTombstone.tombstone_id, str, False)
    await check(CHECKPOINT, AuditChainCheckpoint, AuditChainCheckpoint.checkpoint_id, str, False)


async def _check_checkpoints(session: AsyncSession, report: _Report, keys: dict[str, Any], *, from_seq: int, to_seq: int) -> int:
    checkpoints = list((await session.scalars(
        select(AuditChainCheckpoint).where(AuditChainCheckpoint.through_seq >= from_seq, AuditChainCheckpoint.through_seq <= to_seq)
        .order_by(AuditChainCheckpoint.through_seq.asc(), AuditChainCheckpoint.id.asc()))).all())
    if not checkpoints:
        return 0
    heads = await _rows_by(session, AuditChainEntry, AuditChainEntry.seq, [cp.through_seq for cp in checkpoints])
    all_hashes = {cp.checkpoint_hash for cp in (await session.scalars(select(AuditChainCheckpoint))).all()}
    for cp in checkpoints:
        entry = heads.get(cp.through_seq)
        if entry is None or entry.entry_hash != cp.head_hash or int(cp.entry_count) != int(cp.through_seq):
            report.add(HASH_MISMATCH, checkpoint_id=cp.checkpoint_id, field="head_hash")
        if sha256_hex(checkpoint_fields(cp)) != cp.checkpoint_hash:
            report.add(HASH_MISMATCH, checkpoint_id=cp.checkpoint_id, field="checkpoint_hash")
        if cp.prev_checkpoint_hash and cp.prev_checkpoint_hash not in all_hashes:
            report.add(CHAIN_BREAK, checkpoint_id=cp.checkpoint_id, field="prev_checkpoint_hash")
        if cp.status == "signed":
            if not verify_checkpoint_signature(cp, keys):
                report.add(CHECKPOINT_SIGNATURE_INVALID, checkpoint_id=cp.checkpoint_id, key_id=cp.key_id)
        else:
            report.info["unsigned_checkpoints"] += 1
    return len(checkpoints)


async def _reverse_check(session: AsyncSession, genesis: AuditChainEntry, report: _Report) -> None:
    """D35 (5): rows newer than the genesis entry must each have an entry; older rows are ``legacy_unchained``."""
    legacy = parse_genesis_ref(genesis.ref_id)

    async def refs(entry_type: str) -> set[str]:
        return set((await session.scalars(select(AuditChainEntry.ref_id).where(AuditChainEntry.entry_type == entry_type))).all())

    chained = await refs(GOVERNANCE_EVENT)
    for (event_id,) in (await session.execute(select(GovernanceEvent.id).where(GovernanceEvent.id > legacy.get("ge", 0)))).all():
        if str(event_id) not in chained:
            report.add(UNCHAINED_RECORD, ref_table="governance_events", ref_id=str(event_id))
    report.info[LEGACY_UNCHAINED] += int(await session.scalar(select(func.count()).select_from(GovernanceEvent).where(GovernanceEvent.id <= legacy.get("ge", 0))) or 0)

    chained = await refs(TOMBSTONE)
    for (tomb_id, row_id) in (await session.execute(select(RetentionTombstone.tombstone_id, RetentionTombstone.id))).all():
        if row_id > legacy.get("tb", 0) and tomb_id not in chained:
            report.add(UNCHAINED_RECORD, ref_table="retention_tombstones", ref_id=tomb_id)
        elif row_id <= legacy.get("tb", 0):
            report.info[LEGACY_UNCHAINED] += 1

    chained = await refs(APPROVAL_DECISION)
    stmt = select(ApprovalEvent.id).where(ApprovalEvent.status != "pending", ApprovalEvent.id > legacy.get("ap", 0))
    for (approval_id,) in (await session.execute(stmt)).all():
        if str(approval_id) not in chained:
            report.add(UNCHAINED_RECORD, ref_table="approval_events", ref_id=str(approval_id))

    chained = await refs(REVIEW_DECISION)
    since = _aware(genesis.created_at)
    rows = (await session.execute(select(EvaluationReview.review_id, EvaluationReview.decided_at).where(EvaluationReview.decided_at.is_not(None)))).all()
    for review_id, decided_at in rows:
        if review_id in chained:
            continue
        if _aware(decided_at) > since:
            report.add(UNCHAINED_RECORD, ref_table="evaluation_reviews", ref_id=review_id)
        else:
            report.info[LEGACY_UNCHAINED] += 1


async def verify(
    session: AsyncSession,
    settings: Settings,
    *,
    from_seq: int | None = None,
    to_seq: int | None = None,
    max_entries: int | None = None,
    public_keys_file: str | None = None,
) -> dict[str, Any]:
    """Re-compute the chain (D35). ``max_entries`` bounds online calls; the CLI passes ``None`` (everything).

    Read-only: nothing is written."""
    report = _Report()
    current = await head(session)
    head_seq = current.seq if current is not None else 0
    start = max(1, int(from_seq or 1))
    end = min(int(to_seq), head_seq) if to_seq else head_seq
    truncated = False
    if max_entries is not None and end - start + 1 > max_entries:
        end, truncated = start + max_entries - 1, True
    keys = load_public_keys(settings, public_keys_file)
    checked = 0
    previous: AuditChainEntry | None = None
    if start > 1 and head_seq:
        previous = await session.scalar(select(AuditChainEntry).where(AuditChainEntry.seq == start - 1))
        if previous is None:
            report.add(CHAIN_BREAK, seq=start - 1, field="missing_entry")
    cursor = start
    while head_seq and cursor <= end:
        batch = list((await session.scalars(
            select(AuditChainEntry).where(AuditChainEntry.seq >= cursor, AuditChainEntry.seq <= end)
            .order_by(AuditChainEntry.seq.asc()).limit(_BATCH))).all())
        if not batch:
            break
        for entry in batch:
            expected_seq = previous.seq + 1 if previous is not None else start
            if entry.seq != expected_seq:
                report.add(CHAIN_BREAK, seq=entry.seq, field="seq_gap", expected=expected_seq)
            expected_prev = previous.entry_hash if previous is not None else (ZERO_HASH if entry.seq == 1 else None)
            if expected_prev is not None and entry.prev_hash != expected_prev:
                report.add(CHAIN_BREAK, seq=entry.seq, field="prev_hash")
            if entry.seq == 1 and entry.entry_type != GENESIS:
                report.add(CHAIN_BREAK, seq=1, field="genesis")
            if entry_hash_of(entry) != entry.entry_hash:
                report.add(HASH_MISMATCH, seq=entry.seq, field="entry_hash")
            previous = entry
            checked += 1
        await _check_sources(session, batch, report)
        cursor = batch[-1].seq + 1
    checkpoints_checked = await _check_checkpoints(session, report, keys, from_seq=start, to_seq=end) if head_seq else 0
    full = start == 1 and end == head_seq and not truncated
    reverse = "skipped"
    if full:
        genesis = await session.scalar(select(AuditChainEntry).where(AuditChainEntry.seq == 1, AuditChainEntry.entry_type == GENESIS))
        if genesis is not None:
            await _reverse_check(session, genesis, report)
            reverse = "done"
        elif head_seq == 0:
            unchained = int(await session.scalar(select(func.count()).select_from(GovernanceEvent)) or 0)
            report.info[LEGACY_UNCHAINED] += unchained
            reverse = "done"
    for kind, count in report.issues.items():
        AUDIT_CHAIN_VERIFY_FAILURES_TOTAL.labels(kind=kind).inc(count)
    return {
        "status": OK if not report.issues else "failed",
        "chain_version": CHAIN_VERSION,
        "checked_entries": checked,
        "from_seq": start if head_seq else 0,
        "to_seq": end if head_seq else 0,
        "head_seq": head_seq,
        "head_hash": current.entry_hash if current is not None else "",
        "truncated": truncated,
        "next_from_seq": end + 1 if truncated else None,
        "reverse_check": reverse,
        "checkpoints_checked": checkpoints_checked,
        "issues": dict(sorted(report.issues.items())),
        "details": report.details,
        "info": report.info,
    }
