from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.deps import get_event_publisher, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.main import app
from app.models.finops import (
    CostEvent,
    PricingCatalog,
    PricingChangeLog,
    PricingImportBatch,
    PricingSourceDocument,
)
from app.schemas.finops import (
    BillingPlatform,
    PriceUnit,
    PricingCatalogCreateRequest,
    Provider,
    ResourceSubtype,
    ResourceType,
    TokenType,
    UsageEventCreateRequest,
)
from app.services.events import InMemoryEventPublisher
from app.services.finops import record_usage_event
from app.services.pricing_import import (
    apply_pricing_seed,
    dry_run_pricing_seed,
    load_pricing_seed,
    validate_pricing_seed,
)
from app.services.run_state import InMemoryRunStateStore


SEED_PATH = Path("app/seeds/model_pricing_seed.yaml")


def reset_v151_settings(monkeypatch, **env):
    keys = [
        "AUTH_ENABLED",
        "API_KEYS",
        "QUOTA_ENABLED",
        "DASHBOARD_AUTH_ENABLED",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


@pytest.fixture
def v151_client(session_factory):
    state_store = InMemoryRunStateStore()
    event_publisher = InMemoryEventPublisher()

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_state_store] = lambda: state_store
    app.dependency_overrides[get_event_publisher] = lambda: event_publisher
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_v151_schema_uses_decimal_and_controlled_enums():
    payload = PricingCatalogCreateRequest(
        provider=Provider.openai,
        billing_platform=BillingPlatform.direct_api,
        provider_region="global",
        resource_type=ResourceType.model,
        resource_subtype=ResourceSubtype.chat_completion,
        model_name="gpt-5.5",
        model_family="GPT",
        model_tier="premium_reasoning",
        token_type=TokenType.input,
        price_unit=PriceUnit.per_1m_tokens,
        unit_price="5.12345678",
        source_url="https://openai.com/api/pricing/",
        source_checked_at="2026-06-01T00:00:00Z",
        effective_from="2026-06-01T00:00:00Z",
        pricing_version="official-2026-06-01",
    )

    assert payload.unit_price == Decimal("5.12345678")
    assert payload.discount_rate == Decimal("0")
    assert PriceUnit.per_1m_tokens_hour.value == "per_1m_tokens_hour"

    with pytest.raises(ValidationError):
        PricingCatalogCreateRequest(
            provider="unknown_vendor",
            resource_type="model",
            resource_subtype="chat_completion",
            model_name="bad-model",
            token_type="input",
            price_unit="per_1m_tokens",
            unit_price="1.00",
            source_url="https://example.test/pricing",
            source_checked_at="2026-06-01T00:00:00Z",
            effective_from="2026-06-01T00:00:00Z",
            pricing_version="bad",
        )


def test_v151_existing_pricing_seed_validates_against_expanded_schema():
    seed = load_pricing_seed(SEED_PATH)
    report = validate_pricing_seed(seed)

    assert report.valid_rows == 149
    assert report.invalid_rows == []
    assert report.duplicate_keys == []
    assert report.unsupported_enum_values == []
    assert {source["provider"] for source in seed.source_documents} >= {
        "openai",
        "google",
        "anthropic",
        "deepseek",
        "kimi",
        "minimax",
        "zai",
    }


async def test_v151_pricing_dry_run_does_not_write_database(session_factory):
    seed = load_pricing_seed(SEED_PATH)
    async with session_factory() as session:
        report = await dry_run_pricing_seed(session, seed, created_by="qa")
        pricing_count = await session.scalar(select(func.count()).select_from(PricingCatalog))
        batch_count = await session.scalar(select(func.count()).select_from(PricingImportBatch))

    assert report.new_rows == 149
    assert report.unchanged_rows == 0
    assert report.rows_to_supersede == 0
    assert report.invalid_rows == []
    assert pricing_count == 0
    assert batch_count == 0


async def test_v151_pricing_apply_writes_versioned_audit_tables(session_factory):
    seed = load_pricing_seed(SEED_PATH)
    async with session_factory() as session:
        report = await apply_pricing_seed(session, seed, created_by="system")
        pricing_count = await session.scalar(select(func.count()).select_from(PricingCatalog))
        batch_count = await session.scalar(select(func.count()).select_from(PricingImportBatch))
        source_count = await session.scalar(select(func.count()).select_from(PricingSourceDocument))
        change_count = await session.scalar(select(func.count()).select_from(PricingChangeLog))

    assert report.new_rows == 149
    assert report.rows_to_supersede == 0
    assert pricing_count == 149
    assert batch_count == 1
    assert source_count == 9
    assert change_count == 149


async def test_v151_pricing_supersede_preserves_old_row_and_cost_snapshot(session_factory):
    seed = load_pricing_seed(SEED_PATH)
    seed.pricing_items = [item for item in seed.pricing_items if item["provider"] == "openai"][:1]
    seed.source_documents = [source for source in seed.source_documents if source["provider"] == "openai"][:1]

    async with session_factory() as session:
        first_report = await apply_pricing_seed(session, seed, created_by="system")
        active_price = await session.scalar(select(PricingCatalog).where(PricingCatalog.is_active.is_(True)))
        usage = await record_usage_event(
            session,
            UsageEventCreateRequest(
                tenant_id="default",
                run_id="run-v151-snapshot",
                provider="openai",
                billing_platform="direct_api",
                model_name=active_price.model_name,
                resource_type="model",
                resource_subtype="chat_completion",
                usage_unit="per_1m_tokens",
                quantity="1",
                input_tokens=1_000_000,
                phase="estimated",
            ),
        )
        cost = await session.scalar(select(CostEvent).where(CostEvent.usage_event_id == usage.usage_id))
        snapshot_before = cost.pricing_snapshot

        updated_seed = deepcopy(seed)
        updated_seed.pricing_items[0]["unit_price"] = "6.00"
        updated_seed.pricing_items[0]["pricing_version"] = "official-2026-06-02"
        second_report = await apply_pricing_seed(session, updated_seed, created_by="system")
        rows = list((await session.scalars(select(PricingCatalog).order_by(PricingCatalog.created_at.asc()))).all())
        cost_after = await session.scalar(select(CostEvent).where(CostEvent.cost_id == cost.cost_id))

    assert first_report.new_rows == 1
    assert second_report.rows_to_supersede == 1
    assert len(rows) == 2
    assert rows[0].is_active is False
    assert rows[0].effective_to is not None
    assert rows[1].supersedes_pricing_id == rows[0].pricing_id
    assert Decimal(snapshot_before["unit_price"]) == Decimal("5.00")
    assert Decimal(cost_after.pricing_snapshot["unit_price"]) == Decimal("5.00")


def test_v151_admin_import_permissions(monkeypatch, v151_client):
    reset_v151_settings(
        monkeypatch,
        AUTH_ENABLED="true",
        API_KEYS="super-key:default:super_admin,viewer-key:default:viewer",
    )

    viewer_denied = v151_client.post(
        "/v1/admin/finops/pricing/import/apply",
        headers={"X-API-Key": "viewer-key"},
        json={"file_path": str(SEED_PATH), "created_by": "viewer"},
    )
    assert viewer_denied.status_code == 403

    dry_run = v151_client.post(
        "/v1/admin/finops/pricing/import/dry-run",
        headers={"X-API-Key": "super-key"},
        json={"file_path": str(SEED_PATH), "created_by": "super-admin"},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["new_rows"] == 149


def test_v151_admin_finops_dashboard_import_and_audit_pages(monkeypatch, v151_client):
    reset_v151_settings(monkeypatch, DASHBOARD_AUTH_ENABLED="false")

    import_page = v151_client.get("/dashboard/admin/finops/pricing/import")
    assert import_page.status_code == 200
    assert "Pricing Import" in import_page.text

    dry_run = v151_client.post(
        "/dashboard/admin/finops/pricing/import",
        data={"file_path": str(SEED_PATH), "mode": "dry_run"},
    )
    assert dry_run.status_code == 200
    assert "Valid Rows" in dry_run.text
    assert "149" in dry_run.text

    apply = v151_client.post(
        "/dashboard/admin/finops/pricing/import",
        data={"file_path": str(SEED_PATH), "mode": "apply"},
    )
    assert apply.status_code == 200
    assert "Import History" in apply.text

    catalog = v151_client.get("/dashboard/admin/finops/pricing?active_only=true")
    assert catalog.status_code == 200
    assert "Pricing Catalog" in catalog.text
    assert "openai" in catalog.text

    sources = v151_client.get("/dashboard/admin/finops/pricing/sources")
    assert sources.status_code == 200
    assert "Source Documents" in sources.text
    assert "Pricing Change Log" in sources.text

    items = v151_client.get("/v1/admin/finops/pricing?active_only=true")
    assert items.status_code == 200
    pricing_id = items.json()[0]["pricing_id"]
    detail = v151_client.get(f"/dashboard/admin/finops/pricing/{pricing_id}")
    assert detail.status_code == 200
    assert "Basic Info" in detail.text
    assert "Source And Effective Dates" in detail.text
