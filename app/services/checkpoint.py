from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan, ExecutionPlanStep


def checkpoint_to_dict(checkpoint: RunCheckpoint) -> dict:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "run_id": checkpoint.run_id,
        "tenant_id": checkpoint.tenant_id,
        "trace_id": checkpoint.trace_id,
        "checkpoint_type": checkpoint.checkpoint_type,
        "status": checkpoint.status,
        "resume_cursor": checkpoint.resume_cursor,
        "state_payload": checkpoint.state_payload,
        "created_by": checkpoint.created_by,
        "created_at": checkpoint.created_at,
    }


def replay_attempt_to_dict(attempt: ReplayAttempt) -> dict:
    return {
        "replay_id": attempt.replay_id,
        "source_run_id": attempt.source_run_id,
        "tenant_id": attempt.tenant_id,
        "trace_id": attempt.trace_id,
        "mode": attempt.mode,
        "status": attempt.status,
        "result_payload": attempt.result_payload,
        "limitations": attempt.limitations,
        "created_by": attempt.created_by,
        "created_at": attempt.created_at,
    }


def _checkpoint_payload(
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    steps: list[ExecutionPlanStep],
    extra: dict | None = None,
) -> dict:
    payload = {
        "request_payload": run.request_payload,
        "policy_decision": dict(run.evaluation_summary.get("policy", {})),
        "execution_plan": {
            "plan_id": plan.plan_id,
            "workflow_id": plan.workflow_id,
            "workflow_version": plan.workflow_version,
            "status": plan.status,
            "selected_tools": plan.selected_tools,
        }
        if plan is not None
        else {},
        "step_count": len(steps),
        "replay_ready": plan is not None,
    }
    if extra:
        payload.update(extra)
    return payload


async def persist_run_checkpoint(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    steps: list[ExecutionPlanStep],
    checkpoint_type: str,
    resume_cursor: str,
    created_by: str,
    extra: dict | None = None,
) -> RunCheckpoint:
    checkpoint = RunCheckpoint(
        checkpoint_id=f"ckpt_{uuid4().hex}",
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        trace_id=(plan.trace_id if plan is not None else run.request_payload.get("metadata", {}).get("trace_id", "")),
        checkpoint_type=checkpoint_type,
        status=run.status,
        resume_cursor=resume_cursor,
        state_payload=_checkpoint_payload(run=run, plan=plan, steps=steps, extra=extra),
        created_by=created_by,
    )
    session.add(checkpoint)
    await session.commit()
    await session.refresh(checkpoint)
    return checkpoint


async def list_run_checkpoints(session: AsyncSession, *, run_id: str) -> list[RunCheckpoint]:
    stmt = select(RunCheckpoint).where(RunCheckpoint.run_id == run_id).order_by(RunCheckpoint.id.asc())
    return list((await session.scalars(stmt)).all())


async def resume_run_from_checkpoint(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan | None,
    steps: list[ExecutionPlanStep],
    resume_reason: str,
    resume_cursor: str,
    created_by: str,
) -> RunCheckpoint:
    summary = dict(run.evaluation_summary or {})
    events = list(summary.get("event_timeline", []))
    events.append(
        {
            "event_type": "run.resume_requested",
            "status": "queued",
            "reason": resume_reason,
            "resume_cursor": resume_cursor,
        }
    )
    summary["event_timeline"] = events
    summary["run_state"] = {"status": "queued", "resume_cursor": resume_cursor}
    summary["resume"] = {
        "status": "queued",
        "reason": resume_reason,
        "created_by": created_by,
        "resume_cursor": resume_cursor,
    }
    run.status = "queued"
    run.requires_human_review = False
    run.decision_reason = f"Resumed by {created_by}: {resume_reason}"
    run.evaluation_summary = summary
    run.result = {
        "response": {
            "run_id": run.run_id,
            "status": run.status,
            "decision_reason": run.decision_reason,
            "risk_score": run.risk_score,
            "policy_flags": run.policy_flags,
        },
        "execution_output": run.execution_output,
        "evaluation_summary": summary,
    }
    await session.flush()
    return await persist_run_checkpoint(
        session,
        run=run,
        plan=plan,
        steps=steps,
        checkpoint_type="resume",
        resume_cursor=resume_cursor,
        created_by=created_by,
        extra={"resume_reason": resume_reason},
    )


async def persist_replay_attempt(
    session: AsyncSession,
    *,
    run: AgentRun,
    plan: ExecutionPlan,
    steps: list[ExecutionPlanStep],
    mode: str,
    created_by: str,
) -> ReplayAttempt:
    attempt = ReplayAttempt(
        replay_id=f"replay_{uuid4().hex}",
        source_run_id=run.run_id,
        tenant_id=run.tenant_id,
        trace_id=plan.trace_id or run.request_payload.get("metadata", {}).get("trace_id", ""),
        mode=mode,
        status="completed",
        result_payload={
            "source_status": run.status,
            "replayed_policy": dict(run.evaluation_summary.get("policy", plan.policy_snapshot)),
            "workflow_id": plan.workflow_id,
            "step_count": len(steps),
            "automatic_external_execution": False,
        },
        limitations={
            "external_tool_calls": False,
            "state_mutation": False,
            "note": "V1.4 replay rebuilds auditable execution context without external side effects.",
        },
        created_by=created_by,
    )
    session.add(attempt)
    await session.commit()
    await session.refresh(attempt)
    return attempt


async def list_replay_attempts(session: AsyncSession, *, run_id: str) -> list[ReplayAttempt]:
    stmt = select(ReplayAttempt).where(ReplayAttempt.source_run_id == run_id).order_by(ReplayAttempt.id.asc())
    return list((await session.scalars(stmt)).all())
