from __future__ import annotations

# ruff: noqa: E402

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.services.pricing_import import apply_pricing_seed, dry_run_pricing_seed, load_pricing_seed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and import the V1.5.1 pricing catalog seed.")
    parser.add_argument("--file", default="app/seeds/model_pricing_seed.yaml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--created-by", default="system")
    parser.add_argument("--base-currency", default="USD")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--allow-warnings", action="store_true")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    seed = load_pricing_seed(args.file)
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            if args.dry_run:
                report = await dry_run_pricing_seed(session, seed, created_by=args.created_by)
            else:
                report = await apply_pricing_seed(
                    session,
                    seed,
                    created_by=args.created_by,
                    base_currency=args.base_currency,
                )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
        if report.invalid_rows:
            return 1
        if args.strict and report.warnings and not args.allow_warnings:
            return 2
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
