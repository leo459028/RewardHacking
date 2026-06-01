from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finops import PricingCatalog, PricingChangeLog, PricingImportBatch, PricingSourceDocument
from app.schemas.finops import (
    BillingPlatform,
    Modality,
    PriceUnit,
    PricingMode,
    Provider,
    ResourceSubtype,
    ResourceType,
    ScopeType,
    TokenType,
)


REQUIRED_PRICING_FIELDS = {
    "provider",
    "billing_platform",
    "provider_region",
    "resource_type",
    "resource_subtype",
    "model_name",
    "model_family",
    "model_tier",
    "modality",
    "pricing_mode",
    "token_type",
    "price_unit",
    "unit_price",
    "currency",
    "source_url",
    "source_checked_at",
    "effective_from",
    "pricing_version",
    "is_system_default",
    "is_active",
}

OPTIONAL_PRICING_FIELDS = {
    "context_window_tokens",
    "max_output_tokens",
    "supports_cache",
    "supports_batch",
    "supports_tools",
    "supports_thinking",
    "discount_rate",
    "multiplier",
    "input_threshold_tokens_min",
    "input_threshold_tokens_max",
    "notes",
}

ENUM_FIELDS = {
    "provider": {item.value for item in Provider},
    "billing_platform": {item.value for item in BillingPlatform},
    "resource_type": {item.value for item in ResourceType},
    "resource_subtype": {item.value for item in ResourceSubtype},
    "modality": {item.value for item in Modality},
    "pricing_mode": {item.value for item in PricingMode},
    "token_type": {item.value for item in TokenType},
    "price_unit": {item.value for item in PriceUnit},
    "scope_type": {item.value for item in ScopeType},
}

DECIMAL_FIELDS = {
    "unit_price",
    "discount_rate",
    "multiplier",
    "minimum_billable_quantity",
    "free_quota_quantity",
    "cache_storage_unit_price",
}


@dataclass
class PricingSeed:
    schema_version: str
    checked_at: str
    base_currency: str
    import_policy: dict[str, Any]
    source_documents: list[dict[str, Any]]
    pricing_items: list[dict[str, Any]]
    source_file_name: str = ""
    source_file_hash: str = ""


@dataclass
class PricingImportReport:
    import_batch_id: str = ""
    import_mode: str = "dry_run"
    status: str = "validated"
    source_file_name: str = ""
    source_file_hash: str = ""
    valid_rows: int = 0
    new_rows: int = 0
    unchanged_rows: int = 0
    rows_to_supersede: int = 0
    invalid_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    missing_optional_fields: list[dict[str, Any]] = field(default_factory=list)
    duplicate_keys: list[dict[str, Any]] = field(default_factory=list)
    stale_sources: list[dict[str, Any]] = field(default_factory=list)
    unsupported_enum_values: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_batch_id": self.import_batch_id,
            "import_mode": self.import_mode,
            "status": self.status,
            "source_file_name": self.source_file_name,
            "source_file_hash": self.source_file_hash,
            "valid_rows": self.valid_rows,
            "new_rows": self.new_rows,
            "unchanged_rows": self.unchanged_rows,
            "rows_to_supersede": self.rows_to_supersede,
            "invalid_rows": self.invalid_rows,
            "warnings": self.warnings,
            "missing_optional_fields": self.missing_optional_fields,
            "duplicate_keys": self.duplicate_keys,
            "stale_sources": self.stale_sources,
            "unsupported_enum_values": self.unsupported_enum_values,
        }


def load_pricing_seed(path: str | Path) -> PricingSeed:
    source = Path(path)
    raw = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        data = _load_yaml(raw)
    return PricingSeed(
        schema_version=str(data.get("schema_version", "")),
        checked_at=str(data.get("checked_at", "")),
        base_currency=str(data.get("base_currency", "USD")),
        import_policy=dict(data.get("import_policy", {})),
        source_documents=list(data.get("source_documents", [])),
        pricing_items=list(data.get("pricing_items", [])),
        source_file_name=str(source),
        source_file_hash=sha256(raw.encode("utf-8")).hexdigest(),
    )


def _load_yaml(raw: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(raw) or {}
    except ModuleNotFoundError:
        return _load_simple_seed_yaml(raw)


def _load_simple_seed_yaml(raw: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key = ""
    current_item: dict[str, Any] | None = None

    for raw_line in raw.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if indent == 0 and line.startswith("- "):
            if not isinstance(data.get(current_key), list):
                data[current_key] = []
            current_item = {}
            data[current_key].append(current_item)
            rest = line[2:].strip()
            if rest:
                key, value = _split_yaml_pair(rest)
                current_item[key] = _parse_yaml_scalar(value)
            continue

        if indent == 0:
            current_item = None
            if line.endswith(":"):
                current_key = line[:-1]
                data[current_key] = {}
            else:
                key, value = _split_yaml_pair(line)
                current_key = key
                data[key] = _parse_yaml_scalar(value)
            continue

        if current_item is not None:
            key, value = _split_yaml_pair(line)
            current_item[key] = _parse_yaml_scalar(value)
            continue

        if isinstance(data.get(current_key), dict):
            key, value = _split_yaml_pair(line)
            data[current_key][key] = _parse_yaml_scalar(value)

    return data


def _split_yaml_pair(line: str) -> tuple[str, str]:
    if ":" not in line:
        return line, ""
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"", "''", '""'}:
        return ""
    if value == "null":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        return value[1:-1]
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def validate_pricing_seed(seed: PricingSeed) -> PricingImportReport:
    report = PricingImportReport(
        source_file_name=seed.source_file_name,
        source_file_hash=seed.source_file_hash,
    )
    active_keys: dict[tuple[Any, ...], int] = {}

    for index, item in enumerate(seed.pricing_items):
        missing = sorted(field for field in REQUIRED_PRICING_FIELDS if _missing(item.get(field)))
        if missing:
            report.invalid_rows.append({"row": index, "reason": "missing_required_fields", "fields": missing})

        for field_name, allowed_values in ENUM_FIELDS.items():
            if field_name in item and item[field_name] not in allowed_values:
                detail = {"row": index, "field": field_name, "value": item[field_name]}
                report.invalid_rows.append({"row": index, "reason": "unsupported_enum_value", **detail})
                report.unsupported_enum_values.append(detail)

        for field_name in DECIMAL_FIELDS:
            if field_name in item and item[field_name] is not None and not _is_decimal(item[field_name]):
                report.invalid_rows.append(
                    {"row": index, "reason": "invalid_decimal", "field": field_name, "value": item[field_name]}
                )

        for field_name in ("source_checked_at", "effective_from"):
            if field_name in item and item[field_name] and _parse_datetime(item[field_name]) is None:
                report.invalid_rows.append(
                    {"row": index, "reason": "invalid_datetime", "field": field_name, "value": item[field_name]}
                )

        missing_optional = sorted(field for field in OPTIONAL_PRICING_FIELDS if item.get(field) is None)
        if missing_optional:
            report.missing_optional_fields.append({"row": index, "fields": missing_optional})

        if not item.get("source_url"):
            report.invalid_rows.append({"row": index, "reason": "missing_source_url"})

        key = pricing_key_for_item(item)
        if item.get("is_active", True):
            if key in active_keys:
                duplicate = {"row": index, "first_row": active_keys[key], "key": list(key)}
                report.duplicate_keys.append(duplicate)
                report.invalid_rows.append({"row": index, "reason": "duplicate_active_pricing_key", "key": list(key)})
            active_keys[key] = index

    report.valid_rows = len(seed.pricing_items) - len({item["row"] for item in report.invalid_rows})
    return report


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _is_decimal(value: Any) -> bool:
    try:
        Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return True


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)


def pricing_key_for_item(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("provider", ""),
        item.get("billing_platform", "direct_api"),
        item.get("provider_region", "global"),
        item.get("billing_region", "global"),
        item.get("resource_type", ""),
        item.get("resource_subtype", ""),
        item.get("model_name", ""),
        item.get("model_version") or "",
        item.get("pricing_mode", "standard"),
        item.get("service_tier", "standard"),
        item.get("token_type", "not_applicable"),
        item.get("price_unit", ""),
        item.get("input_threshold_tokens_min"),
        item.get("input_threshold_tokens_max"),
        item.get("scope_type", "system_default"),
        item.get("scope_id") or "",
    )


def pricing_key_for_record(item: PricingCatalog) -> tuple[Any, ...]:
    return (
        item.provider,
        item.billing_platform,
        item.provider_region,
        item.billing_region,
        item.resource_type,
        item.resource_subtype,
        item.model_name,
        item.model_version,
        item.pricing_mode,
        item.service_tier,
        item.token_type,
        item.price_unit,
        item.input_threshold_tokens_min,
        item.input_threshold_tokens_max,
        item.scope_type,
        item.scope_id or "",
    )


async def dry_run_pricing_seed(
    session: AsyncSession,
    seed: PricingSeed,
    *,
    created_by: str = "system",
) -> PricingImportReport:
    validation = validate_pricing_seed(seed)
    validation.import_mode = "dry_run"
    if validation.invalid_rows:
        validation.status = "invalid"
        return validation
    await _compare_seed_with_database(session, seed, validation)
    return validation


async def apply_pricing_seed(
    session: AsyncSession,
    seed: PricingSeed,
    *,
    created_by: str = "system",
    base_currency: str = "USD",
) -> PricingImportReport:
    report = await dry_run_pricing_seed(session, seed, created_by=created_by)
    report.import_mode = "apply"
    if report.invalid_rows:
        report.status = "failed"
        return report

    batch_id = f"pib_{uuid4().hex}"
    report.import_batch_id = batch_id
    batch = PricingImportBatch(
        import_batch_id=batch_id,
        source_file_name=seed.source_file_name,
        source_file_hash=seed.source_file_hash,
        import_mode="apply",
        status="applied",
        created_by=created_by,
        applied_at=datetime.now(timezone.utc),
        summary=report.to_dict(),
        error_details={},
    )
    session.add(batch)

    for source in seed.source_documents:
        session.add(
            PricingSourceDocument(
                source_id=f"src_{uuid4().hex}",
                provider=str(source.get("provider", "")),
                source_url=str(source.get("source_url", "")),
                source_type=str(source.get("source_type", "")),
                checked_at=_parse_datetime(source.get("checked_at")) or datetime.now(timezone.utc),
                content_hash=sha256(json.dumps(source, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
                confidence=str(source.get("confidence", "")),
                notes=str(source.get("notes", "")),
            )
        )

    existing_by_key = await _active_pricing_by_key(session)
    now = datetime.now(timezone.utc)
    for item in seed.pricing_items:
        key = pricing_key_for_item(item)
        existing = existing_by_key.get(key)
        if existing is not None and _pricing_rows_equivalent(existing, item):
            continue
        new_row = _pricing_catalog_from_seed_item(
            item,
            import_batch_id=batch_id,
            created_by=created_by,
            supersedes_pricing_id=existing.pricing_id if existing is not None else "",
        )
        session.add(new_row)
        if existing is not None:
            existing.is_active = False
            existing.effective_to = _parse_datetime(item.get("effective_from")) or now
            existing.updated_by = created_by
        session.add(
            PricingChangeLog(
                change_id=f"pcl_{uuid4().hex}",
                pricing_id_old=existing.pricing_id if existing is not None else "",
                pricing_id_new=new_row.pricing_id,
                change_type="supersede" if existing is not None else "create",
                old_unit_price=existing.unit_price if existing is not None else None,
                new_unit_price=new_row.unit_price,
                currency=new_row.currency,
                change_reason=str(item.get("change_reason") or "seed_import"),
                source_url=new_row.source_url,
                changed_by=created_by,
                import_batch_id=batch_id,
            )
        )

    await session.commit()
    return report


async def _compare_seed_with_database(
    session: AsyncSession,
    seed: PricingSeed,
    report: PricingImportReport,
) -> None:
    existing_by_key = await _active_pricing_by_key(session)
    for item in seed.pricing_items:
        existing = existing_by_key.get(pricing_key_for_item(item))
        if existing is None:
            report.new_rows += 1
        elif _pricing_rows_equivalent(existing, item):
            report.unchanged_rows += 1
        else:
            report.rows_to_supersede += 1


async def _active_pricing_by_key(session: AsyncSession) -> dict[tuple[Any, ...], PricingCatalog]:
    rows = list(
        (
            await session.scalars(
                select(PricingCatalog).where(PricingCatalog.is_active.is_(True))
            )
        ).all()
    )
    return {pricing_key_for_record(row): row for row in rows}


def _pricing_rows_equivalent(row: PricingCatalog, item: dict[str, Any]) -> bool:
    return (
        _decimal_to_str(row.unit_price) == _decimal_to_str(item.get("unit_price"))
        and row.currency == item.get("currency", "USD")
        and _decimal_to_str(row.discount_rate) == _decimal_to_str(item.get("discount_rate") or "0")
        and _decimal_to_str(row.multiplier) == _decimal_to_str(item.get("multiplier") or "1")
        and row.source_url == item.get("source_url", "")
        and row.pricing_version == item.get("pricing_version", "")
    )


def _decimal_to_str(value: Any) -> str:
    return format(Decimal(str(value or "0")).normalize(), "f")


def _pricing_catalog_from_seed_item(
    item: dict[str, Any],
    *,
    import_batch_id: str,
    created_by: str,
    supersedes_pricing_id: str = "",
) -> PricingCatalog:
    return PricingCatalog(
        pricing_id=str(item.get("pricing_id") or f"price_{uuid4().hex}"),
        provider=str(item.get("provider", "")),
        provider_display_name=str(item.get("provider_display_name") or item.get("provider", "")),
        billing_platform=str(item.get("billing_platform", "direct_api")),
        provider_region=str(item.get("provider_region", "global")),
        billing_region=str(item.get("billing_region", "global")),
        resource_type=str(item.get("resource_type", "")),
        resource_subtype=str(item.get("resource_subtype", "other")),
        model_name=str(item.get("model_name", "")),
        model_alias=str(item.get("model_alias") or ""),
        model_version=str(item.get("model_version") or ""),
        model_family=str(item.get("model_family") or ""),
        model_tier=str(item.get("model_tier") or ""),
        modality=str(item.get("modality") or "text"),
        pricing_mode=str(item.get("pricing_mode") or "standard"),
        service_tier=str(item.get("service_tier") or "standard"),
        token_type=str(item.get("token_type") or "not_applicable"),
        price_unit=str(item.get("price_unit", "")),
        unit_price=Decimal(str(item.get("unit_price") or "0")),
        currency=str(item.get("currency", "USD")),
        tax_included=bool(item.get("tax_included") or False),
        discount_rate=Decimal(str(item.get("discount_rate") or "0")),
        multiplier=Decimal(str(item.get("multiplier") or "1")),
        minimum_billable_quantity=Decimal(str(item.get("minimum_billable_quantity") or "0")),
        free_quota_quantity=Decimal(str(item.get("free_quota_quantity") or "0")),
        free_quota_period=str(item.get("free_quota_period") or ""),
        input_threshold_tokens_min=item.get("input_threshold_tokens_min"),
        input_threshold_tokens_max=item.get("input_threshold_tokens_max"),
        context_window_tokens=item.get("context_window_tokens"),
        max_output_tokens=item.get("max_output_tokens"),
        cache_ttl_seconds=item.get("cache_ttl_seconds"),
        cache_storage_unit_price=Decimal(str(item.get("cache_storage_unit_price") or "0")),
        supports_cache=item.get("supports_cache"),
        supports_batch=item.get("supports_batch"),
        supports_tools=item.get("supports_tools"),
        supports_thinking=item.get("supports_thinking"),
        supports_streaming=item.get("supports_streaming"),
        supports_json_mode=item.get("supports_json_mode"),
        supports_multimodal=item.get("supports_multimodal"),
        source_url=str(item.get("source_url", "")),
        source_type=str(item.get("source_type") or "official_pricing"),
        source_confidence=str(item.get("source_confidence") or "official"),
        source_checked_at=_parse_datetime(item.get("source_checked_at")) or datetime.now(timezone.utc),
        pricing_version=str(item.get("pricing_version", "")),
        effective_from=_parse_datetime(item.get("effective_from")) or datetime.now(timezone.utc),
        effective_to=_parse_datetime(item.get("effective_to")),
        scope_type=str(item.get("scope_type") or "system_default"),
        scope_id=str(item.get("scope_id") or ""),
        is_system_default=bool(item.get("is_system_default", True)),
        is_tenant_override=bool(item.get("is_tenant_override", False)),
        supersedes_pricing_id=supersedes_pricing_id or str(item.get("supersedes_pricing_id") or ""),
        import_batch_id=import_batch_id,
        change_reason=str(item.get("change_reason") or "seed_import"),
        created_by=created_by,
        updated_by=created_by,
        is_active=bool(item.get("is_active", True)),
        metadata_payload={"notes": item.get("notes", ""), "base_currency": item.get("base_currency")},
    )
