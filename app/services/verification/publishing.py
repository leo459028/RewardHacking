"""Upload a suite snapshot and publish it, or ask a second person to (v1.6.2 D22, D23).

Replacing the answer sheet changes the grading standard, so ``verification_suite.publish`` can be put under dual control
with ``DUAL_CONTROL_ACTIONS``. The API and the Dashboard both come through here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services import change_requests
from app.services.verification import artifacts, suites


async def upload(
    session: AsyncSession, settings: Settings, *, data: bytes, tenant_id: str, name: str, base_ref: str = "", image_name: str = "python",
    test_command: Any = None, test_paths: Any = None, requester: change_requests.Requester,
) -> change_requests.Outcome:
    suites.validate_definition(settings, name=name, base_ref=base_ref, image_name=image_name)
    command, paths = suites.clean_test_command(test_command), suites.clean_test_paths(test_paths)
    normalized = artifacts.normalize_upload(data, max_bytes=int(settings.sandbox_artifact_max_bytes))
    suites.require_tests(normalized.paths, paths)
    await artifacts.store_artifact(session, normalized)
    await session.commit()
    params = {
        "tenant_id": tenant_id, "name": name, "artifact_digest": normalized.digest, "base_ref": base_ref, "image_name": image_name,
        "test_command": command, "test_paths": paths,
    }
    return await change_requests.execute_or_request(session, settings, change_requests.VERIFICATION_SUITE_PUBLISH, params, requester)
