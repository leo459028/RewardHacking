"""v1.5.9 benchmark, honeypot and attestation-key request schemas (responses are monitor-only dicts)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BenchmarkRunRequest(BaseModel):
    corpora: list[str] = Field(default_factory=lambda: ["seed-v1.5.9"], max_length=20)
    split: Literal["all", "dev", "holdout"] = "all"
    exclude_accepted_behavior: bool = False
    tenant_id: str | None = None
    created_by: str = Field(default="", max_length=128)
    # v1.5.10 judge triage measurement: "none" keeps the v1.5.9 run unchanged
    judge_provider: Literal["none", "deterministic", "openai_compatible"] = "none"
    max_judge_samples: int = Field(default=100, ge=1, le=5000)
    # v1.5.11: up to three named judge profiles measured side by side (takes precedence over judge_provider)
    judge_profiles: list[str] | None = Field(default=None, max_length=3)


class ReviewExportRequest(BaseModel):
    scope: Literal["tenant", "global"] = "tenant"
    label_categories: list[str] = Field(default_factory=list, max_length=20)
    notes: list[str] = Field(default_factory=list, max_length=20)
    exported_by: str = Field(default="", max_length=128)


class BulkReviewExportRequest(BaseModel):
    tenant_id: str | None = None
    label: Literal["confirmed_hacking", "false_positive", "accepted"] | None = None
    decided_after: datetime | None = None
    scope: Literal["tenant", "global"] = "tenant"
    exported_by: str = Field(default="", max_length=128)
    limit: int = Field(default=500, ge=1, le=2000)


class SampleRelabelRequest(BaseModel):
    label: Literal["hacking", "benign"]
    label_categories: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=500)
    relabeled_by: str = Field(default="", max_length=128)


class SampleRetireRequest(BaseModel):
    retired_by: str = Field(default="", max_length=128)


class HoneypotLaunchRequest(BaseModel):
    tenant_id: str | None = None
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(default="", max_length=128)
    agent_version: str = Field(default="", max_length=128)
    model_name: str = Field(default="", max_length=128)
    launched_by: str = Field(default="", max_length=128)


class HoneypotEnableRequest(BaseModel):
    enabled: bool


class InspectImportRequest(BaseModel):
    log: dict | None = None
    log_text: str | None = Field(default=None, max_length=10_000_000)
    tenant_id: str | None = None
    label_from: Literal["score", "metadata"] = "score"
    score_threshold: float = 1.0
    scorer: str | None = Field(default=None, max_length=128)
    metadata_key: str = Field(default="reward_hacking", max_length=128)
    hacking_category: str = Field(default="special_casing", max_length=64)
    dry_run: bool = True
    imported_by: str = Field(default="", max_length=128)


class AttestationKeyCreateRequest(BaseModel):
    name: str = Field(default="", max_length=128)
    algorithm: Literal["ed25519", "ecdsa-p256-sha256"] = "ed25519"
    public_key_pem: str = Field(min_length=1, max_length=8192)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=50)
    created_by: str = Field(default="", max_length=128)


class AttestationKeyRevokeRequest(BaseModel):
    revoked_by: str = Field(default="", max_length=128)
