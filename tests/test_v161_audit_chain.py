"""v1.6.1 P5: tamper-evident audit chain (plan D28-D36, feature eight).

The chain is written by session hooks in ``app.services.audit_chain`` inside the source transaction. Tampering is
simulated with Core ``UPDATE`` / ``DELETE`` statements, which bypass the ORM hooks just like a direct database edit.
Signing keys are generated per test; nothing here reads the local ``.env`` (settings come from ``monkeypatch``).
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import delete, func, select, update

from app.core.config import get_settings
from app.jobs import audit_chain as audit_chain_job
from app.models.approval_event import ApprovalEvent
from app.models.audit_chain import AuditChainCheckpoint, AuditChainEntry
from app.models.evaluation_review import EvaluationReview
from app.models.governance_event import GovernanceEvent
from app.models.retention import RetentionTombstone
from app.services import audit_chain
from app.services.governance_audit import add_governance_event, persist_governance_event
from app.services.review_queue import list_reviews
from app.workers.consumer import process_run_by_id
from tests.test_v160_retention import _apply, _plan, _seed, _settings

ZERO = "0" * 64


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _db(session_factory, fn):
    async def run():
        async with session_factory() as session:
            return await fn(session)

    return asyncio.run(run())


async def _all_entries(session) -> list[AuditChainEntry]:
    return list((await session.scalars(select(AuditChainEntry).order_by(AuditChainEntry.seq))).all())


def _entries(session_factory) -> list[AuditChainEntry]:
    return _db(session_factory, _all_entries)


def _verify(session_factory, **kwargs) -> dict:
    return _db(session_factory, lambda s: audit_chain.verify(s, get_settings(), **kwargs))


def _exec(session_factory, stmt) -> None:
    async def run(session):
        await session.execute(stmt)
        await session.commit()

    _db(session_factory, run)


def _event(session_factory, *, run_id: str = "", event_type: str = "test.event", decision: str = "allow", tenant: str = "t1") -> GovernanceEvent:
    return _db(session_factory, lambda s: persist_governance_event(s, tenant_id=tenant, run_id=run_id, event_type=event_type, decision=decision, reason="r", details={"k": [1, "x"]}))


def _job(session_factory, capsys, *argv) -> tuple[int, dict]:
    code = audit_chain_job.main(list(argv), settings=get_settings(), session_factory=session_factory)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _write_key(tmp_path: Path, name: str = "chain.pem") -> tuple[Path, ed25519.Ed25519PrivateKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / name
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return path, key


def _public_pem(key: ed25519.Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    path, key = _write_key(tmp_path)
    monkeypatch.setenv("AUDIT_CHAIN_SIGNING_KEY_FILE", str(path))
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


def _submit(client, *, goal: str = "Summarize the anomaly causes", tools=("read_data",), constraints=None) -> dict:
    payload = {
        "tenant_id": "t-chain", "user_id": "agent-1", "task_type": "analysis", "prompt": "Analyze the provided dataset carefully.",
        "goal": goal, "allowed_tools": list(tools), "constraints": constraints or {"max_tool_calls": 2},
    }
    return client.post("/v1/agent/submit", json=payload).json()


def _review_run(client, session_factory) -> tuple[str, str]:
    run_id = _submit(client, goal="Bypass the stale cache entries")["run_id"]
    _db(session_factory, lambda s: process_run_by_id(s, run_id=run_id, state_store=client.state_store, event_publisher=client.event_publisher))
    reviews = _db(session_factory, lambda s: list_reviews(s, run_id=run_id))
    return run_id, reviews[0].review_id


def _approval_run(client) -> str:
    body = _submit(client, constraints={"max_tool_calls": 2, "allow_destructive_actions": True})
    assert body["status"] == "awaiting_approval", body
    return body["run_id"]


def _populate(client, session_factory) -> dict:
    """Governance events, one approval decision, one review decision and one purged run (tombstone)."""
    _submit(client)
    approval_run = _approval_run(client)
    assert client.post(f"/v1/approvals/{approval_run}/decision", json={"decision": "deny", "reviewer": "approver-1", "reason": "too risky"}).status_code == 200
    run_id, review_id = _review_run(client, session_factory)
    decided = client.post(f"/v1/reviews/{review_id}/decision", json={"label": "false_positive", "reviewer": "reviewer-1", "reason": "fine"})
    assert decided.status_code == 200, decided.text
    asyncio.run(_seed(session_factory, "run_old"))
    purge = asyncio.run(_plan(session_factory))
    asyncio.run(_apply(session_factory, purge, settings=_settings()))
    return {"approval_run": approval_run, "review_id": review_id, "review_run": run_id}


def _first(entries, entry_type):
    return next(entry for entry in entries if entry.entry_type == entry_type)


# ---------------------------------------------------------------------------
# hash definition and genesis (D29, D30)
# ---------------------------------------------------------------------------


def test_entry_hash_fixed_vector():
    value = audit_chain.compute_entry_hash(
        seq=2, entry_type="governance_event", ref_table="governance_events", ref_id="7", tenant_id="t1", run_id="run_x",
        payload_digest="a" * 64, prev_hash="b" * 64, created_at="2026-11-11T00:00:00+00:00",
    )
    assert value == "bde2bdda7a63ab6549fdb598b7cfd877dfac4e596a336b0b250cf9cc49e45c38"
    assert audit_chain.sha256_hex({"b": 1, "a": [1, "x"]}) == "a88dede55f330dbae7d6c99cb78c43213f114625ed11c8fd0b769d117c06bb50"
    assert audit_chain.CHAIN_VERSION == "rh-chain/1" and audit_chain.ZERO_HASH == ZERO


def test_payload_fields_are_documented_and_complete():
    docs = Path("docs/security.md").read_text(encoding="utf-8")
    for entry_type, fields in audit_chain.PAYLOAD_FIELDS.items():
        assert f"`{entry_type}`" in docs, entry_type
        for field in fields:
            assert f"`{field}`" in docs, (entry_type, field)


def test_first_event_writes_genesis_then_the_event(session_factory):
    event = _event(session_factory, run_id="run_1")
    entries = _entries(session_factory)
    assert [e.entry_type for e in entries] == ["genesis", "governance_event"]
    genesis, entry = entries
    assert genesis.seq == 1 and genesis.prev_hash == ZERO and genesis.ref_id == "ge:0;tb:0;ap:0"
    assert entry.seq == 2 and entry.prev_hash == genesis.entry_hash and entry.ref_id == str(event.id)
    assert entry.run_id == "run_1" and entry.tenant_id == "t1" and entry.payload_digest == audit_chain.payload_digest("governance_event", event)
    assert all(entry.entry_hash == audit_chain.entry_hash_of(entry) for entry in entries)


def test_genesis_records_legacy_rows(session_factory, monkeypatch):
    monkeypatch.setenv("AUDIT_CHAIN_ENABLED", "false")
    get_settings.cache_clear()
    legacy = [_event(session_factory, event_type=f"legacy.{n}") for n in range(3)]
    assert _entries(session_factory) == []
    monkeypatch.setenv("AUDIT_CHAIN_ENABLED", "true")
    get_settings.cache_clear()
    _event(session_factory, event_type="after")
    genesis = _entries(session_factory)[0]
    assert audit_chain.parse_genesis_ref(genesis.ref_id) == {"ge": legacy[-1].id, "tb": 0, "ap": 0}
    result = _verify(session_factory)
    assert result["status"] == "ok" and result["info"]["legacy_unchained"] == 3 and result["reverse_check"] == "done"


# ---------------------------------------------------------------------------
# same-transaction writing (D31, D32)
# ---------------------------------------------------------------------------


def test_directly_constructed_events_are_chained_too(session_factory):
    async def run(session):
        session.add(GovernanceEvent(tenant_id="t1", event_type="direct", decision="ok", reason="", details={}))
        add_governance_event(session, tenant_id="t1", event_type="helper", decision="ok", reason="")
        await session.commit()

    _db(session_factory, run)
    kinds = [e.entry_type for e in _entries(session_factory)]
    assert kinds == ["genesis", "governance_event", "governance_event"]


def test_rolled_back_events_leave_no_entry(session_factory):
    async def run(session):
        add_governance_event(session, tenant_id="t1", event_type="discarded", decision="ok", reason="")
        await session.flush()
        await session.rollback()
        add_governance_event(session, tenant_id="t1", event_type="kept", decision="ok", reason="")
        await session.commit()

    _db(session_factory, run)
    entries = _entries(session_factory)
    assert [e.entry_type for e in entries] == ["genesis", "governance_event"]
    assert _verify(session_factory)["status"] == "ok"


def test_append_failure_fails_the_source_transaction(session_factory, monkeypatch):
    def broken(session, pending):
        raise RuntimeError("chain store unavailable")

    monkeypatch.setattr(audit_chain, "_append_all", broken)

    async def run(session):
        add_governance_event(session, tenant_id="t1", event_type="must.not.persist", decision="ok", reason="")
        with pytest.raises(RuntimeError):
            await session.commit()
        await session.rollback()
        return int(await session.scalar(select(func.count()).select_from(GovernanceEvent)) or 0)

    assert _db(session_factory, run) == 0


def test_sequential_writers_never_fork(session_factory):
    for n in range(6):
        _event(session_factory, event_type=f"writer.{n}")
    entries = _entries(session_factory)
    assert [e.seq for e in entries] == list(range(1, len(entries) + 1))
    assert len({e.prev_hash for e in entries}) == len(entries)
    assert all(entries[i].prev_hash == entries[i - 1].entry_hash for i in range(1, len(entries)))


def test_disabled_chain_writes_nothing_and_verify_reports_it(session_factory, monkeypatch):
    _event(session_factory, event_type="chained")
    monkeypatch.setenv("AUDIT_CHAIN_ENABLED", "false")
    get_settings.cache_clear()
    gap = _event(session_factory, event_type="while.disabled")
    monkeypatch.setenv("AUDIT_CHAIN_ENABLED", "true")
    get_settings.cache_clear()
    result = _verify(session_factory)
    assert result["status"] == "failed" and result["issues"] == {"unchained_record": 1}
    assert result["details"][0]["ref_id"] == str(gap.id)


# ---------------------------------------------------------------------------
# decisions and tombstones (D30)
# ---------------------------------------------------------------------------


def test_review_approval_and_tombstone_entries(client, session_factory):
    ids = _populate(client, session_factory)
    entries = _entries(session_factory)
    kinds = {e.entry_type for e in entries}
    assert {"genesis", "governance_event", "review_decision", "approval_decision", "tombstone"} <= kinds
    review_entry = _first(entries, "review_decision")
    review = _db(session_factory, lambda s: s.scalar(select(EvaluationReview).where(EvaluationReview.review_id == ids["review_id"])))
    assert review_entry.ref_id == ids["review_id"] and review_entry.payload_digest == audit_chain.payload_digest("review_decision", review)
    approval_entry = next(e for e in entries if e.entry_type == "approval_decision" and e.run_id == ids["approval_run"])
    approval = _db(session_factory, lambda s: s.scalar(select(ApprovalEvent).where(ApprovalEvent.id == int(approval_entry.ref_id))))
    assert approval.status == "deny" and approval_entry.payload_digest == audit_chain.payload_digest("approval_decision", approval)
    tombstone = _db(session_factory, lambda s: s.scalar(select(RetentionTombstone).where(RetentionTombstone.run_id == "run_old")))
    tomb_entry = _first(entries, "tombstone")
    assert (tombstone.chain_version, tombstone.prev_hash, tombstone.entry_hash) == ("rh-chain/1", tomb_entry.prev_hash, tomb_entry.entry_hash)
    # the review decision entry is written in the decision's transaction, right before its governance event
    decided_event = next(e for e in entries if e.entry_type == "governance_event" and e.seq == review_entry.seq + 1)
    assert decided_event.run_id == ids["review_run"]


def test_untouched_database_verifies_and_purged_sources_are_explained(client, session_factory, capsys):
    _populate(client, session_factory)
    result = _verify(session_factory)
    assert result["status"] == "ok", result
    assert result["reverse_check"] == "done" and result["info"]["explained_by_tombstone"] >= 1
    code, body = _job(session_factory, capsys, "--verify")
    assert code == 0 and body["status"] == "ok" and body["checked_entries"] == len(_entries(session_factory))


def test_expired_approval_is_chained(client, session_factory):
    run_id = _approval_run(client)

    async def expire(session):
        from app.services.audit_log import expire_stale_approvals

        await session.execute(update(ApprovalEvent).where(ApprovalEvent.run_id == run_id).values(expired=True))
        await session.commit()
        return await expire_stale_approvals(session)

    _db(session_factory, expire)
    entry = next(e for e in _entries(session_factory) if e.entry_type == "approval_decision" and e.run_id == run_id)
    approval = _db(session_factory, lambda s: s.scalar(select(ApprovalEvent).where(ApprovalEvent.id == int(entry.ref_id))))
    assert approval.status == "expired" and _verify(session_factory)["status"] == "ok"


def test_purged_run_410_carries_the_chain_fields(client, session_factory):
    asyncio.run(_seed(session_factory, "run_gone"))
    asyncio.run(_apply(session_factory, asyncio.run(_plan(session_factory)), settings=_settings()))
    response = client.get("/v1/runs/run_gone")
    assert response.status_code == 410
    tombstone = response.json()["tombstone"]
    assert tombstone["chain_version"] == "rh-chain/1" and len(tombstone["entry_hash"]) == 64 and len(tombstone["prev_hash"]) == 64


def test_review_redecision_compares_only_the_latest_entry(client, session_factory):
    _, review_id = _review_run(client, session_factory)
    assert client.post(f"/v1/reviews/{review_id}/decision", json={"label": "needs_info", "reviewer": "r1", "reason": "more"}).status_code == 200
    assert client.post(f"/v1/reviews/{review_id}/decision", json={"label": "false_positive", "reviewer": "r2", "reason": "ok"}).status_code == 200
    result = _verify(session_factory)
    assert result["status"] == "ok" and result["info"]["superseded"] == 1


# ---------------------------------------------------------------------------
# tampering (feature eight): each is caught and the CLI exits 1
# ---------------------------------------------------------------------------


def _tamper_case(client, session_factory, capsys, mutate, expected_kind: str) -> dict:
    ids = _populate(client, session_factory)
    assert _verify(session_factory)["status"] == "ok"
    mutate(session_factory, ids)
    result = _verify(session_factory)
    assert result["status"] == "failed" and expected_kind in result["issues"], result
    code, body = _job(session_factory, capsys, "--verify")
    assert code == 1 and body["status"] == "failed"
    return result


def test_tamper_governance_event_decision(client, session_factory, capsys):
    def mutate(factory, ids):
        target = _db(factory, lambda s: s.scalar(select(GovernanceEvent).where(GovernanceEvent.run_id == "").order_by(GovernanceEvent.id.desc())))
        _exec(factory, update(GovernanceEvent).where(GovernanceEvent.id == target.id).values(decision="allow-forged"))

    _tamper_case(client, session_factory, capsys, mutate, "hash_mismatch")


def test_tamper_review_label(client, session_factory, capsys):
    def mutate(factory, ids):
        _exec(factory, update(EvaluationReview).where(EvaluationReview.review_id == ids["review_id"]).values(label="accepted"))

    _tamper_case(client, session_factory, capsys, mutate, "hash_mismatch")


def test_tamper_delete_event_without_run(client, session_factory, capsys):
    def mutate(factory, ids):
        target = _db(factory, lambda s: s.scalar(select(GovernanceEvent).where(GovernanceEvent.run_id == "").order_by(GovernanceEvent.id.desc())))
        _exec(factory, delete(GovernanceEvent).where(GovernanceEvent.id == target.id))

    _tamper_case(client, session_factory, capsys, mutate, "missing_source")


def test_tamper_delete_an_entry(client, session_factory, capsys):
    def mutate(factory, ids):
        entries = _entries(factory)
        _exec(factory, delete(AuditChainEntry).where(AuditChainEntry.seq == entries[len(entries) // 2].seq))

    result = _tamper_case(client, session_factory, capsys, mutate, "chain_break")
    assert "unchained_record" in result["issues"] or "missing_source" not in result["issues"]


def test_tamper_entry_payload_digest(client, session_factory, capsys):
    def mutate(factory, ids):
        seq = _entries(factory)[3].seq
        _exec(factory, update(AuditChainEntry).where(AuditChainEntry.seq == seq).values(payload_digest="f" * 64))

    _tamper_case(client, session_factory, capsys, mutate, "hash_mismatch")


def test_tamper_insert_an_entry_in_the_middle(client, session_factory, capsys):
    def mutate(factory, ids):
        entries = _entries(factory)
        middle = entries[len(entries) // 2]

        async def run(session):
            for entry in sorted(entries, key=lambda e: -e.seq):
                if entry.seq > middle.seq:
                    await session.execute(update(AuditChainEntry).where(AuditChainEntry.seq == entry.seq).values(seq=entry.seq + 1))
            forged = AuditChainEntry(
                seq=middle.seq + 1, entry_id="ace_forged", entry_type="governance_event", ref_table="governance_events",
                ref_id="999999", tenant_id="t1", run_id="", payload_digest="e" * 64, prev_hash=middle.entry_hash,
                chain_version="rh-chain/1", created_at=middle.created_at,
            )
            forged.entry_hash = audit_chain.entry_hash_of(forged)
            session.add(forged)
            await session.commit()

        _db(factory, run)

    result = _tamper_case(client, session_factory, capsys, mutate, "chain_break")
    assert "missing_source" in result["issues"]


def test_tamper_tombstone_content_digest(client, session_factory, capsys):
    def mutate(factory, ids):
        _exec(factory, update(RetentionTombstone).where(RetentionTombstone.run_id == "run_old").values(content_digest="0" * 64))

    _tamper_case(client, session_factory, capsys, mutate, "hash_mismatch")


def test_tamper_checkpoint_head_hash(client, session_factory, capsys, signing_key):
    def mutate(factory, ids):
        result = _db(factory, lambda s: audit_chain.create_checkpoint(s, get_settings(), actor="test"))
        assert result.created and _verify(factory)["status"] == "ok"
        _exec(factory, update(AuditChainCheckpoint).where(AuditChainCheckpoint.checkpoint_id == result.checkpoint.checkpoint_id).values(head_hash="c" * 64))

    result = _tamper_case(client, session_factory, capsys, mutate, "checkpoint_signature_invalid")
    assert "hash_mismatch" in result["issues"]


def test_tamper_checkpoint_resigned_with_another_key(client, session_factory, capsys, signing_key):
    def mutate(factory, ids):
        result = _db(factory, lambda s: audit_chain.create_checkpoint(s, get_settings(), actor="test"))
        other = ed25519.Ed25519PrivateKey.generate()
        import base64

        forged = base64.b64encode(other.sign(audit_chain.canonical_json(audit_chain.checkpoint_fields(result.checkpoint)).encode("utf-8"))).decode("ascii")
        _exec(factory, update(AuditChainCheckpoint).where(AuditChainCheckpoint.checkpoint_id == result.checkpoint.checkpoint_id).values(signature=forged))

    _tamper_case(client, session_factory, capsys, mutate, "checkpoint_signature_invalid")


# ---------------------------------------------------------------------------
# checkpoints and keys (D33, D34)
# ---------------------------------------------------------------------------


def test_signed_checkpoint_verifies_with_the_public_key_and_is_idempotent(session_factory, signing_key, tmp_path, capsys):
    _event(session_factory)
    code, body = _job(session_factory, capsys, "--checkpoint")
    assert code == 0 and body["created"] is True
    checkpoint = body["checkpoint"]
    assert checkpoint["status"] == "signed" and checkpoint["key_id"] == audit_chain.key_id_for(signing_key.public_key())
    assert checkpoint["through_seq"] == 2 and checkpoint["prev_checkpoint_hash"] == ""
    code, again = _job(session_factory, capsys, "--checkpoint")
    assert code == 0 and again["created"] is False and again["reason"] == "no_new_entries"
    assert _entries(session_factory)[-1].entry_type == "checkpoint"
    public = tmp_path / "trusted.pem"
    public.write_text(_public_pem(signing_key), encoding="utf-8")
    code, verified = _job(session_factory, capsys, "--verify", "--public-keys", str(public))
    assert code == 0 and verified["checkpoints_checked"] == 1
    _event(session_factory, event_type="next")
    code, second = _job(session_factory, capsys, "--checkpoint")
    assert second["created"] and second["checkpoint"]["prev_checkpoint_hash"] == checkpoint["checkpoint_hash"]
    code, exported = _job(session_factory, capsys, "--export-checkpoint", checkpoint["checkpoint_id"])
    assert code == 0 and exported["fields"]["head_hash"] == checkpoint["head_hash"] and exported["signature"] == checkpoint["signature"]


def test_untrusted_public_key_fails_verification(session_factory, signing_key, tmp_path, capsys):
    _event(session_factory)
    _job(session_factory, capsys, "--checkpoint")
    wrong = tmp_path / "wrong.pem"
    wrong.write_text(_public_pem(ed25519.Ed25519PrivateKey.generate()), encoding="utf-8")
    code, body = _job(session_factory, capsys, "--verify", "--public-keys", str(wrong))
    assert code == 1 and body["issues"] == {"checkpoint_signature_invalid": 1}


def test_rotated_key_keeps_old_checkpoints_verifiable(session_factory, tmp_path, monkeypatch, capsys):
    old_path, old_key = _write_key(tmp_path, "old.pem")
    monkeypatch.setenv("AUDIT_CHAIN_SIGNING_KEY_FILE", str(old_path))
    get_settings.cache_clear()
    _event(session_factory)
    _job(session_factory, capsys, "--checkpoint")
    new_path, _ = _write_key(tmp_path, "new.pem")
    keys = tmp_path / "public_keys.pem"
    keys.write_text(_public_pem(old_key), encoding="utf-8")
    monkeypatch.setenv("AUDIT_CHAIN_SIGNING_KEY_FILE", str(new_path))
    monkeypatch.setenv("AUDIT_CHAIN_PUBLIC_KEYS_FILE", str(keys))
    get_settings.cache_clear()
    _event(session_factory, event_type="after.rotation")
    _job(session_factory, capsys, "--checkpoint")
    result = _verify(session_factory)
    assert result["status"] == "ok" and result["checkpoints_checked"] == 2
    get_settings.cache_clear()


def test_checkpoint_without_key_is_unsigned(session_factory, capsys):
    _event(session_factory)
    code, body = _job(session_factory, capsys, "--checkpoint")
    assert code == 0 and body["checkpoint"]["status"] == "unsigned" and body["checkpoint"]["signature"] == ""
    result = _verify(session_factory)
    assert result["status"] == "ok" and result["info"]["unsigned_checkpoints"] == 1


def test_empty_chain_checkpoint_and_bad_key(session_factory, tmp_path, monkeypatch, capsys):
    code, body = _job(session_factory, capsys, "--checkpoint")
    assert code == 0 and body["created"] is False and body["reason"] == "empty_chain"
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CHAIN_SIGNING_KEY_FILE", str(bad))
    get_settings.cache_clear()
    _event(session_factory)
    code, body = _job(session_factory, capsys, "--checkpoint")
    assert code == 2 and body["error"]["code"] == "audit_chain_signing_key_invalid"
    code, body = _job(session_factory, capsys, "--export-checkpoint", "acp_missing")
    assert code == 1 and body["error"]["code"] == "audit_chain_checkpoint_not_found"
    get_settings.cache_clear()


def test_operations_check_warns_in_dev_and_fails_in_prod_without_key(session_factory, monkeypatch):
    from app.services.operations import _audit_chain_integrity_check

    _event(session_factory)
    check = _db(session_factory, lambda s: _audit_chain_integrity_check(s, settings=get_settings()))
    assert check.status == "warn" and check.metadata["advisory"] is True and "no_signing_key" in check.metadata["problems"]
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    check = _db(session_factory, lambda s: _audit_chain_integrity_check(s, settings=get_settings()))
    assert check.status == "fail" and check.metadata["advisory"] is False
    get_settings.cache_clear()


def test_operations_check_passes_with_signed_recent_checkpoint_and_fails_on_tamper(session_factory, signing_key):
    from app.services.operations import _audit_chain_integrity_check

    _event(session_factory)
    _db(session_factory, lambda s: audit_chain.create_checkpoint(s, get_settings(), actor="test"))
    check = _db(session_factory, lambda s: _audit_chain_integrity_check(s, settings=get_settings()))
    assert check.status == "pass", check.detail
    _exec(session_factory, update(AuditChainEntry).where(AuditChainEntry.seq == 2).values(payload_digest="f" * 64))
    check = _db(session_factory, lambda s: _audit_chain_integrity_check(s, settings=get_settings()))
    assert check.status == "fail" and check.metadata["advisory"] is False


# ---------------------------------------------------------------------------
# API (feature eight)
# ---------------------------------------------------------------------------


def test_api_status_entries_checkpoints_and_bounded_verify(client, session_factory, monkeypatch):
    for n in range(4):
        _event(session_factory, event_type=f"api.{n}")
    status = client.get("/v1/audit-chain/status").json()
    assert status["entry_count"] == 5 and status["head_seq"] == 5 and status["signal_usage"] == "monitor_only"
    assert status["entries_by_type"] == {"genesis": 1, "governance_event": 4}
    entries = client.get("/v1/audit-chain/entries", params={"from_seq": 2, "limit": 2}).json()["entries"]
    assert [e["seq"] for e in entries] == [2, 3] and set(entries[0]) >= {"entry_hash", "prev_hash", "payload_digest"}
    assert "reason" not in entries[0] and "details" not in entries[0]
    assert client.get("/v1/audit-chain/checkpoints").json() == {"checkpoints": [], "count": 0}
    monkeypatch.setenv("AUDIT_CHAIN_VERIFY_MAX_ENTRIES", "2")
    get_settings.cache_clear()
    page = client.post("/v1/audit-chain/verify", json={}).json()
    assert page["status"] == "ok" and page["checked_entries"] == 2 and page["truncated"] is True and page["next_from_seq"] == 3
    assert page["reverse_check"] == "skipped"
    rest = client.post("/v1/audit-chain/verify", json={"from_seq": 3}).json()
    assert rest["checked_entries"] == 2 and rest["next_from_seq"] == 5
    assert client.post("/v1/audit-chain/verify", json={"from_seq": 0}).status_code == 422
    assert client.post("/v1/audit-chain/verify", json={"unknown": 1}).status_code == 422
    get_settings.cache_clear()


def test_api_roles_admin_and_auditor_only(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "admin-key:default:admin,auditor-key:default:auditor,viewer-key:default:viewer,operator-key:default:operator")
    get_settings.cache_clear()
    for key, allowed in (("admin-key", True), ("auditor-key", True), ("viewer-key", False), ("operator-key", False)):
        status = client.get("/v1/audit-chain/status", headers={"X-API-Key": key}).status_code
        verify = client.post("/v1/audit-chain/verify", headers={"X-API-Key": key}, json={}).status_code
        assert (status == 200, verify == 200) == (allowed, allowed), key
    assert client.get("/v1/audit-chain/entries").status_code == 401
    get_settings.cache_clear()


def test_chain_is_monitor_only(client, session_factory):
    ids = _populate(client, session_factory)
    run = client.get(f"/v1/runs/{ids['review_run']}").json()
    before = run["status"]
    _verify(session_factory)
    _db(session_factory, lambda s: audit_chain.create_checkpoint(s, get_settings(), actor="test"))
    assert client.get(f"/v1/runs/{ids['review_run']}").json()["status"] == before


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_page_for_admin_and_auditor(client, signed_in, session_factory):
    _event(session_factory)
    signed_in(role="auditor")
    page = client.get("/dashboard/audit-chain")
    assert page.status_code == 200 and 'data-testid="audit-chain-head-hash"' in page.text and "Audit Chain | 稽核鏈" in page.text
    assert 'id="audit-chain-verify-button"' in page.text and 'id="audit-chain-checkpoint-button"' not in page.text
    verified = client.post("/dashboard/audit-chain/verify", data={})
    assert verified.status_code == 200 and 'data-testid="audit-chain-verify-status">ok<' in verified.text
    refused = client.post("/dashboard/audit-chain/checkpoints", follow_redirects=False)
    assert refused.status_code == 403 and refused.json()["error"]["permission"] == "audit_chain.checkpoint"
    signed_in(role="admin")
    assert 'id="audit-chain-checkpoint-button"' in client.get("/dashboard/audit-chain").text


def test_dashboard_page_refuses_other_roles_and_tenant_users(client, signed_in):
    for role, tenant in (("operator", None), ("reviewer", None), ("tenant_admin", "t-1")):
        signed_in(role=role, tenant_id=tenant)
        assert client.get("/dashboard/audit-chain").status_code == 403, role
        assert "/dashboard/audit-chain" not in client.get("/dashboard").text


def test_dashboard_checkpoint_needs_reauth_then_works(client, signed_in, session_factory):
    from datetime import datetime, timedelta, timezone

    from app.models.access import DashboardSession

    _event(session_factory)
    who = signed_in(role="admin")

    async def age(session):
        await session.execute(update(DashboardSession).where(DashboardSession.session_id == who.session_id).values(reauth_at=datetime.now(timezone.utc) - timedelta(seconds=900)))
        await session.commit()

    _db(session_factory, age)
    stale = client.post("/dashboard/audit-chain/checkpoints", headers={"referer": "http://testserver/dashboard/audit-chain"}, follow_redirects=False)
    assert stale.status_code == 303 and stale.headers["location"].startswith("/dashboard/reauth?next=%2Fdashboard%2Faudit-chain")
    assert _db(session_factory, lambda s: s.scalar(select(func.count()).select_from(AuditChainCheckpoint))) == 0
    assert client.post("/dashboard/reauth", data={"password": who.password, "next": "/dashboard/audit-chain"}, follow_redirects=False).status_code == 303
    created = client.post("/dashboard/audit-chain/checkpoints", follow_redirects=False)
    assert created.status_code == 303 and "checkpoint_created" in created.headers["location"]
    checkpoint = _db(session_factory, lambda s: s.scalar(select(AuditChainCheckpoint)))
    assert checkpoint.created_by == f"user:{who.username}" and checkpoint.status == "unsigned"


def test_off_mode_dashboard_page_renders(client, session_factory):
    _event(session_factory)
    page = client.get("/dashboard/audit-chain")
    assert page.status_code == 200 and 'href="/dashboard/audit-chain"' in page.text


def test_access_events_are_chained(client, signed_in, session_factory):
    signed_in(role="auditor")
    client.post("/dashboard/approvals/expire-stale", follow_redirects=False)  # 403, writes access.denied
    denied = _db(session_factory, lambda s: s.scalar(select(GovernanceEvent).where(GovernanceEvent.event_type == "access.denied")))
    assert any(e.entry_type == "governance_event" and e.ref_id == str(denied.id) for e in _entries(session_factory))


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


def test_no_core_statement_rewrites_audited_tables_outside_retention():
    """Core UPDATE / INSERT on audited rows would bypass the session hooks (retention deletes whole runs and leaves a
    tombstone, which verification uses to explain the missing sources)."""
    offenders = []
    audited = {"GovernanceEvent", "AuditChainEntry", "AuditChainCheckpoint", "RetentionTombstone"}
    for path in Path("app").rglob("*.py"):
        if path.name in {"retention.py"} and path.parent.name == "services":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"update", "insert", "delete"}:
                if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in audited:
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_audit_chain_tables_are_never_purged():
    from app.services import retention

    assert "audit_chain_entries" not in retention.DELETED_TABLES and "audit_chain_checkpoints" not in retention.DELETED_TABLES
