"""Request / response schemas for the v1.6.0 retention and legal hold API (D11-D17)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetentionApplyRequest(BaseModel):
    plan_digest: str = Field(min_length=1, max_length=128)
    confirm: bool = False


class LegalHoldCreateRequest(BaseModel):
    scope_type: str = Field(min_length=1, max_length=16)
    scope_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class LegalHoldReleaseRequest(BaseModel):
    release_reason: str = Field(default="", max_length=2000)


class RetentionPolicyResponse(BaseModel):
    purge_mode: str
    deletes_enabled: bool
    retention_days: int
    batch_size: int
    plan_ttl_minutes: int
    finops_ledger_retention_days: int
    finops_ledger_policy: str
    policy_version: str
    exclusion_reasons: list[str]


class RetentionPurgeRecord(BaseModel):
    purge_id: str
    status: str
    plan_digest: str
    cutoff_at: str | None = None
    expires_at: str | None = None
    settings_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidate_run_ids: list[str] = Field(default_factory=list)
    excluded_summary: dict[str, Any] = Field(default_factory=dict)
    estimated_counts: dict[str, Any] = Field(default_factory=dict)
    scan_summary: dict[str, Any] = Field(default_factory=dict)
    created_by: str = ""
    created_at: str | None = None
    applied_by: str | None = None
    applied_at: str | None = None
    applied_counts: dict[str, Any] = Field(default_factory=dict)
    skipped_at_apply: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    receipt_digest: str | None = None


class RetentionTombstoneRecord(BaseModel):
    tombstone_id: str
    run_id: str
    tenant_id: str
    purge_id: str
    purged_at: str | None = None
    run_created_at: str | None = None
    final_status: str = ""
    record_counts: dict[str, Any] = Field(default_factory=dict)
    content_digest: str = ""
    policy_version: str = ""


class LegalHoldRecord(BaseModel):
    hold_id: str
    scope_type: str
    scope_id: str
    tenant_id: str
    reason: str
    status: str
    created_by: str
    created_at: str | None = None
    released_by: str | None = None
    released_at: str | None = None
    release_reason: str | None = None


class LedgerReportResponse(BaseModel):
    retention_days: int
    cutoff: str | None = None
    policy: str
    deletes: bool
    tables: dict[str, Any]
