"""改进实验室：运行证据、最小修改提案、评估门禁与人工发布。"""
import datetime
import hashlib
import hmac
import json
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import harness as harness_registry
from .. import jobs
from ..config import settings
from ..database import get_db
from ..models import Agent, HarnessVersion, ImprovementProposal, Item, Job, User, iso_utc
from ..security import require_module, require_owner

router = APIRouter(prefix="/api/v1/improvement", tags=["改进实验室"])


def _loads(value: str, default):
    try:
        parsed = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _proposal_out(row: ImprovementProposal) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "base_version": row.base_version,
        "proposed_version_id": row.proposed_version_id,
        "hypothesis": row.hypothesis,
        "evidence": _loads(row.evidence, []),
        "expected_metrics": _loads(row.expected_metrics, {}),
        "evaluation": _loads(row.evaluation, {}),
        "status": row.status,
        "created_by": row.created_by,
        "approved_by": row.approved_by,
        "created_at": iso_utc(row.created_at),
    }


@router.get("/turns")
def list_runs(
    limit: int = 50,
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    rows = db.query(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 200))).all()
    out = []
    for row in rows:
        agent = db.get(Agent, row.agent_id) if row.agent_id else None
        if agent is not None:
            try:
                require_owner(admin, agent)
            except HTTPException:
                continue
        payload = _loads(row.payload, {})
        out.append(
            {
                "id": row.id,
                "agent_id": row.agent_id,
                "agent_name": agent.name if agent else "",
                "harness_version": payload.get("harness_version"),
                "status": row.status,
                "progress": row.progress,
                "error": row.error,
                "created_at": iso_utc(row.created_at),
                "updated_at": iso_utc(row.updated_at),
            }
        )
    return out


@router.get("/turns/{run_id}/items")
def list_run_events(
    run_id: str,
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    job = db.get(Job, run_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turn 不存在")
    agent = db.get(Agent, job.agent_id) if job.agent_id else None
    if agent is not None:
        require_owner(admin, agent)
    rows = (
        db.query(Item)
        .filter(Item.turn_id == run_id)
        .order_by(Item.sequence, Item.id)
        .all()
    )
    return [
        {
            "sequence": row.sequence,
            "type": row.name,
            "payload": _loads(row.payload, {}),
            "created_at": iso_utc(row.created_at),
        }
        for row in rows
    ]


@router.get("/proposals")
def list_proposals(
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    rows = db.query(ImprovementProposal).order_by(ImprovementProposal.id.desc()).all()
    return [
        _proposal_out(row)
        for row in rows
        if (agent := db.get(Agent, row.agent_id)) is not None
        and _can_manage(admin, agent)
    ]


def _can_manage(user: User, agent: Agent) -> bool:
    try:
        require_owner(user, agent)
        return True
    except HTTPException:
        return False


@router.post("/proposals", status_code=status.HTTP_201_CREATED)
def create_proposal(
    body: dict = Body(...),
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    agent_id = int(body.get("agent_id") or 0)
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "智能体不存在")
    require_owner(admin, agent)
    hypothesis = str(body.get("hypothesis") or "").strip()
    system_prompt = str(body.get("system_prompt") or "").strip()
    if not hypothesis or not system_prompt:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "hypothesis 与 system_prompt 必填")
    active = harness_registry.active_version(db, agent)
    evidence = list(dict.fromkeys(
        str(value).strip() for value in (body.get("evidence") or [])
        if str(value).strip()
    ))
    if not evidence:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少提供一个已完成的基线 Turn")
    _validated_baseline_jobs(db, agent, agent.active_version, evidence)
    proposed = harness_registry.create_version(
        db,
        agent,
        system_prompt=system_prompt,
        tool_policy=body.get("tool_policy") or (_loads(active.tool_policy, {}) if active else {}),
        memory_policy=body.get("memory_policy") or (_loads(active.memory_policy, {}) if active else {}),
        verification_policy=body.get("verification_policy") or (
            _loads(active.verification_policy, {}) if active else {}
        ),
        output_policy=body.get("output_policy") or (_loads(active.output_policy, {}) if active else {}),
        change_summary=hypothesis,
        created_by=admin.id,
        publish=False,
    )
    row = ImprovementProposal(
        agent_id=agent.id,
        base_version=agent.active_version,
        proposed_version_id=proposed.id,
        hypothesis=hypothesis,
        evidence=json.dumps(evidence, ensure_ascii=False),
        expected_metrics=json.dumps(body.get("expected_metrics") or {}, ensure_ascii=False),
        status="proposed",
        created_by=admin.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _proposal_out(row)


def _validated_baseline_jobs(
    db: Session, agent: Agent, base_version: int, run_ids: list[str]
) -> list[Job]:
    rows = []
    for run_id in run_ids:
        job = db.get(Job, run_id)
        payload = _loads(job.payload, {}) if job is not None else {}
        snapshot_version = (
            ((payload.get("execution_snapshot") or {}).get("harness") or {}).get("version")
            or payload.get("harness_version")
        )
        if (
            job is None
            or job.agent_id != agent.id
            or job.status != jobs.DONE
            or int(snapshot_version or 0) != int(base_version)
            or not (_loads(job.result, {}).get("answer") or "").strip()
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"基线 Turn {run_id} 必须属于当前智能体/版本且成功完成",
            )
        rows.append(job)
    return rows


def _verification_passed(db: Session, run_id: str) -> bool:
    event = (
        db.query(Item)
        .filter(
            Item.turn_id == run_id,
            Item.name == "verification.completed",
        )
        .order_by(Item.sequence.desc(), Item.id.desc())
        .first()
    )
    return bool(event and _loads(event.payload, {}).get("passed"))


def _metrics(db: Session, rows: list[Job]) -> dict:
    count = len(rows)
    successes = sum(
        row.status == jobs.DONE
        and bool((_loads(row.result, {}).get("answer") or "").strip())
        for row in rows
    )
    verified = sum(_verification_passed(db, row.id) for row in rows)
    return {
        "runs": count,
        "success_rate": successes / count if count else 0.0,
        "verification_rate": verified / count if count else 0.0,
    }


def _signed_evaluation(value: dict) -> dict:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    key = (settings.SECRET_MASTER_KEY or settings.JWT_SECRET).encode("utf-8")
    return {
        **unsigned,
        "signature": hmac.new(key, canonical, hashlib.sha256).hexdigest(),
    }


def _signature_valid(value: dict) -> bool:
    signature = str(value.get("signature") or "")
    return bool(signature) and hmac.compare_digest(
        signature, _signed_evaluation(value)["signature"]
    )


@router.post("/proposals/{proposal_id}/evaluation")
def record_evaluation(
    proposal_id: int,
    body: dict = Body(...),
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    row = db.get(ImprovementProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提案不存在")
    agent = db.get(Agent, row.agent_id)
    require_owner(admin, agent)
    if agent.active_version != row.base_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "基线版本已过期，请重新创建提案")
    baseline = _validated_baseline_jobs(
        db, agent, row.base_version, _loads(row.evidence, [])
    )
    candidate_version = db.get(HarnessVersion, row.proposed_version_id)
    if candidate_version is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "候选 Harness 版本不存在")
    evaluation = _loads(row.evaluation, {})
    if row.status == "proposed":
        from .chat import build_execution_snapshot

        candidate_ids = []
        for index, baseline_job in enumerate(baseline):
            baseline_payload = _loads(baseline_job.payload, {})
            inputs = baseline_payload.get("inputs") or {}
            query = str(inputs.get("query") or "").strip()
            if not query:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"基线 Turn {baseline_job.id} 缺少可回放问题",
                )
            snapshot = build_execution_snapshot(db, agent, query)
            snapshot["harness"] = harness_registry.as_dict(candidate_version)
            payload = {
                "user_id": admin.id,
                "agent_id": agent.id,
                "harness_version": candidate_version.version,
                "inputs": inputs,
                "template_ids": [],
                "dataset_ids": [],
                "skill_ids": [],
                "mcp_ids": [],
                "invoked_agent_ids": [],
                "provider_id": baseline_payload.get("provider_id"),
                "attachment_images": [],
                "attachment_docs": [],
                "session_id": f"eval-{row.id}-{index}-{uuid.uuid4().hex[:8]}",
                "project_id": None,
                "source": "improvement-evaluation",
                "approval_tokens": [],
                "execution_snapshot": snapshot,
            }
            candidate_ids.append(jobs.enqueue_in_session(
                db,
                admin.id,
                agent.id,
                "chat",
                payload,
                idempotency_key=f"improvement:{row.id}:candidate:{baseline_job.id}",
            ))
        evaluation = {
            "algorithm": "paired-run-v1",
            "baseline_run_ids": [item.id for item in baseline],
            "candidate_run_ids": candidate_ids,
            "started_by": admin.id,
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        row.evaluation = json.dumps(evaluation, ensure_ascii=False)
        row.status = "evaluating"
        db.commit()
        db.refresh(row)
        return _proposal_out(row)
    if row.status != "evaluating":
        return _proposal_out(row)
    candidate_ids = evaluation.get("candidate_run_ids") or []
    candidates = [db.get(Job, run_id) for run_id in candidate_ids]
    if not candidates or any(item is None for item in candidates):
        raise HTTPException(status.HTTP_409_CONFLICT, "候选回归任务记录不完整")
    if any(item.status not in jobs._TERMINAL for item in candidates):
        return _proposal_out(row)
    baseline_metrics = _metrics(db, baseline)
    candidate_metrics = _metrics(db, candidates)
    expected = _loads(row.expected_metrics, {})
    min_success = float(expected.get(
        "min_success_rate", baseline_metrics["success_rate"]
    ))
    min_verification = float(expected.get(
        "min_verification_rate", baseline_metrics["verification_rate"]
    ))
    passed = (
        candidate_metrics["success_rate"] >= min_success
        and candidate_metrics["verification_rate"] >= min_verification
    )
    evaluation = _signed_evaluation({
        **evaluation,
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
        "thresholds": {
            "min_success_rate": min_success,
            "min_verification_rate": min_verification,
        },
        "outcome": "passed" if passed else "failed",
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    row.evaluation = json.dumps(evaluation, ensure_ascii=False)
    row.status = "evaluated" if passed else "rejected"
    db.commit()
    db.refresh(row)
    return _proposal_out(row)


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int,
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    row = db.get(ImprovementProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提案不存在")
    agent = db.get(Agent, row.agent_id)
    require_owner(admin, agent)
    evaluation = _loads(row.evaluation, {})
    if (
        row.status != "evaluated"
        or evaluation.get("outcome") != "passed"
        or not _signature_valid(evaluation)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "只有通过评估的提案才能批准")
    if admin.id == row.created_by:
        raise HTTPException(status.HTTP_409_CONFLICT, "提案创建人与批准人必须分离")
    if agent.active_version != row.base_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "基线版本已过期，请重新评估")
    version = db.get(HarnessVersion, row.proposed_version_id)
    if version is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "提案版本不存在")
    harness_registry.publish_version(db, agent, version.version)
    row.status = "approved"
    row.approved_by = admin.id
    db.commit()
    db.refresh(row)
    return _proposal_out(row)


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    admin: User = Depends(require_module("improvement")),
    db: Session = Depends(get_db),
):
    row = db.get(ImprovementProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提案不存在")
    require_owner(admin, db.get(Agent, row.agent_id))
    row.status = "rejected"
    db.commit()
    db.refresh(row)
    return _proposal_out(row)
