from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompt_config import build_voice_block
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.models.llmops import GenerationRun, PromptVersion
from app.db.session import get_session
from app.personalization.eligibility import (
    FACT_FIELDS,
    list_global_fact_eligibility,
    set_global_fact_eligibility,
)
from app.rules import versioning
from app.schemas.prompt_config import (
    AngleVersionMetricsOut,
    ComponentContentOut,
    DiffOut,
    DiscardRequest,
    DraftOut,
    DraftRequest,
    ExplainFactOut,
    ExplainOut,
    FactEligibilityReplaceRequest,
    FactEligibilityRowOut,
    PublishRequest,
    TestGenerateRequest,
    TestGenerateRunOut,
    VersionSummaryOut,
)
from app.services.prompt_testing import PromptTestingError, generate_test_draft
from app.services.review_metrics import angle_version_metrics

router = APIRouter(
    prefix="/prompt-config", tags=["prompt-config"], dependencies=[Depends(get_current_reviewer_id)]
)


def _with_rendered_voice_text(component_type: str, rows: list[dict]) -> list[dict]:
    if component_type != "voice_contract":
        return rows
    prepared = []
    for row in rows:
        row = dict(row)
        if not row.get("rendered_text"):
            row["rendered_text"] = build_voice_block(
                body_markdown=row.get("body_markdown"),
                persona=row.get("persona"),
                tone=row.get("tone"),
                writing_style=row.get("writing_style"),
                structure=row.get("structure"),
                subject_guidance=row.get("subject_guidance"),
                length_guidance=row.get("length_guidance"),
                readability_guidance=row.get("readability_guidance"),
            )
        prepared.append(row)
    return prepared


@router.post("/{component_type}/draft", response_model=DraftOut)
def draft_component(
    component_type: str, body: DraftRequest, session: Session = Depends(get_session)
) -> DraftOut:
    rows = _with_rendered_voice_text(component_type, body.rows)
    try:
        version = versioning.save_draft(
            session, component_type, body.component_key, rows, by=body.created_by
        )
    except versioning.VersioningError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    session.commit()
    return DraftOut(version=version)


@router.get("/{component_type}/{version}/content", response_model=ComponentContentOut)
def get_component_content(
    component_type: str,
    version: int,
    component_key: str = versioning.DEFAULT_COMPONENT_KEY,
    session: Session = Depends(get_session),
) -> ComponentContentOut:
    try:
        rows = versioning.get_version_rows(session, component_type, component_key, version)
    except versioning.VersioningError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return ComponentContentOut(
        component_type=component_type, component_key=component_key, version=version, rows=rows
    )


@router.get("/facts", response_model=list[str])
def get_fact_fields() -> list[str]:
    return list(FACT_FIELDS)


@router.get("/personalization/{version}/facts", response_model=list[FactEligibilityRowOut])
def get_personalization_facts(
    version: int, session: Session = Depends(get_session)
) -> list[FactEligibilityRowOut]:
    rows = list_global_fact_eligibility(session, version)
    return [
        FactEligibilityRowOut(
            fact_field=row.fact_field, exposure_mode=row.exposure_mode, condition=row.condition
        )
        for row in rows
    ]


@router.put("/personalization/{version}/facts", response_model=list[FactEligibilityRowOut])
def put_personalization_facts(
    version: int, body: FactEligibilityReplaceRequest, session: Session = Depends(get_session)
) -> list[FactEligibilityRowOut]:
    rows = set_global_fact_eligibility(session, version, [row.model_dump() for row in body.rows])
    session.commit()
    return [
        FactEligibilityRowOut(
            fact_field=row.fact_field, exposure_mode=row.exposure_mode, condition=row.condition
        )
        for row in rows
    ]


@router.post("/{component_type}/{version}/publish", status_code=204)
def publish_component(
    component_type: str, version: int, body: PublishRequest, session: Session = Depends(get_session)
) -> None:
    try:
        versioning.publish(
            session, component_type, body.component_key, version, by=body.by, at=body.at
        )
    except versioning.VersioningError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    session.commit()


@router.post("/{component_type}/{version}/discard", status_code=204)
def discard_component(
    component_type: str, version: int, body: DiscardRequest, session: Session = Depends(get_session)
) -> None:
    try:
        versioning.discard_draft(session, component_type, body.component_key, version)
    except versioning.VersioningError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    session.commit()


@router.get("/{component_type}/versions", response_model=list[VersionSummaryOut])
def list_component_versions(
    component_type: str,
    component_key: str = versioning.DEFAULT_COMPONENT_KEY,
    session: Session = Depends(get_session),
) -> list[VersionSummaryOut]:
    try:
        summaries = versioning.list_versions(session, component_type, component_key)
    except versioning.VersioningError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return [VersionSummaryOut(**summary) for summary in summaries]


@router.get("/{component_type}/diff", response_model=DiffOut)
def diff_component_versions(
    component_type: str,
    version_a: int,
    version_b: int,
    component_key: str = versioning.DEFAULT_COMPONENT_KEY,
    session: Session = Depends(get_session),
) -> DiffOut:
    try:
        diff = versioning.diff_versions(
            session, component_type, component_key, version_a, version_b
        )
    except versioning.VersioningError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return DiffOut(diff=diff)


@router.post("/test-generate", response_model=list[TestGenerateRunOut])
def test_generate(
    body: TestGenerateRequest, session: Session = Depends(get_session)
) -> list[TestGenerateRunOut]:
    try:
        runs = generate_test_draft(
            session,
            angle=body.angle,
            tier=body.tier,
            angle_version=body.angle_version,
            tier_version=body.tier_version,
            voice_version=body.voice_version,
            safety_version=body.safety_version,
            output_version=body.output_version,
            personalization_version=body.personalization_version,
            client_id=body.client_id,
            fact_profile=body.fact_profile,
            product=body.product,
            n=body.n,
            use_rag=body.use_rag,
            cta_override=body.cta_override,
        )
    except (PromptTestingError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    session.commit()
    return [
        TestGenerateRunOut(
            run_id=run.run_id,
            status=run.status,
            subject=(run.ai_draft_content or {}).get("subject"),
            body=(run.ai_draft_content or {}).get("body"),
            attempts=run.attempts,
            failed_guardrail=run.failed_guardrail,
            reason=run.reason,
        )
        for run in runs
    ]


@router.get("/angles/{angle}/metrics", response_model=list[AngleVersionMetricsOut])
def angle_metrics(
    angle: str, session: Session = Depends(get_session)
) -> list[AngleVersionMetricsOut]:
    return [
        AngleVersionMetricsOut(
            angle=metrics.angle,
            angle_catalog_version=metrics.angle_catalog_version,
            review_count=metrics.review_count,
            approval_rate=metrics.approval_rate,
            edit_rate=metrics.edit_rate,
            rejection_rate=metrics.rejection_rate,
            regeneration_rate=metrics.regeneration_rate,
        )
        for metrics in angle_version_metrics(session, angle)
    ]


@router.get("/generations/{run_id}/explain", response_model=ExplainOut)
def explain_generation(run_id: str, session: Session = Depends(get_session)) -> ExplainOut:
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no generation run {run_id!r}")

    angle = session.scalar(
        select(PromptVersion.angle).where(PromptVersion.prompt_version_id == run.prompt_version_id)
    )
    used_fields = set((run.context_payload or {}).keys())
    facts = [
        ExplainFactOut(
            field=field,
            used=field in used_fields,
            value=(run.context_payload or {}).get(field),
        )
        for field in FACT_FIELDS
    ]

    return ExplainOut(
        run_id=run.run_id,
        angle=angle,
        priority_tier=run.priority_tier,
        run_kind=run.run_kind,
        rule_version=run.rule_version,
        angle_catalog_version=run.angle_catalog_version,
        tier_contract_version=run.tier_contract_version,
        voice_contract_version=run.voice_contract_version,
        safety_policy_version=run.safety_policy_version,
        output_policy_version=run.output_policy_version,
        personalization_policy_version=run.personalization_policy_version,
        status=run.status,
        attempts=run.attempts,
        failed_guardrail=run.failed_guardrail,
        reason=run.reason,
        facts=facts,
    )
