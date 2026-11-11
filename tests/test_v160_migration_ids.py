"""v1.6.0: Alembic revision ID rule (``docs/migrations.md`` § Revision ID Rule).

Revision IDs look like dates (``YYYYMMDD`` + 4-digit serial). From ``202611120002`` on, an ID uses its real
creation date, or the previous head's date with the next serial when the real date is not later than the head.
Older revisions (``v1.5.9``-``v1.5.12``) predate the rule and are never renamed.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import date
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
RULE_START = "202611120002"
ID_PATTERN = re.compile(r"^(\d{8})(\d{4})$")
CREATE_DATE = re.compile(r"^Create Date: (\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}$", re.MULTILINE)


def _migrations() -> dict[str, tuple[str | None, str, str]]:
    found = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found[module.revision] = (module.down_revision, path.name, module.__doc__ or "")
    return found


def _date(revision: str) -> date:
    day = ID_PATTERN.match(revision).group(1)
    return date(int(day[:4]), int(day[4:6]), int(day[6:]))


def test_v160_revision_chain_is_linear_and_increasing():
    migrations = _migrations()
    heads = set(migrations) - {down for down, _, _ in migrations.values() if down}
    assert len(heads) == 1
    for revision, (down, filename, _) in migrations.items():
        assert ID_PATTERN.match(revision), revision
        assert filename.startswith(f"{revision}_"), filename
        if down is not None:
            assert down in migrations and int(revision) > int(down), (revision, down)


def test_v160_revision_ids_never_invent_dates_from_rule_start():
    migrations = _migrations()
    assert RULE_START in migrations
    for revision, (down, filename, doc) in migrations.items():
        if int(revision) < int(RULE_START):
            continue
        match = CREATE_DATE.search(doc)
        assert match, f"{filename}: docstring needs 'Create Date: YYYY-MM-DD HH:MM:SS'"
        created = date.fromisoformat(match.group(1))
        allowed = max(created, _date(down)) if down else created
        assert _date(revision) <= allowed, f"{filename}: ID date {_date(revision)} is later than {allowed}"
        if down and _date(revision) == _date(down):
            assert int(revision) == int(down) + 1, f"{filename}: same-day ID must use the next serial"
