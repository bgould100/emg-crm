import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database.auth import authenticate_contractor_session
from app.services.collections import (
    IdempotencyConflictError,
    JobNotFoundError,
    record_collection,
)
from app.services.payment_agreements import (
    AgreementJobNotFoundError,
    InvalidAgreementError,
    create_payment_agreement,
    get_payment_agreement,
)


router = APIRouter(prefix="/contractor", tags=["contractor-financials"])


class CollectionReport(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    collected_at: datetime


class PaymentAgreementRequest(BaseModel):
    arrangement_type: str
    invoice_total: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    schedule_type: str | None = None
    installment_amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    final_installment_amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    plan_start_date: date | None = None
    final_due_date: date | None = None
    custom_schedule: list[dict[str, Any]] | None = None


def _authenticated_contractor(request: Request):
    allowed_origin = os.getenv("EMG_ALLOWED_ORIGIN")
    origin = request.headers.get("origin")

    if not allowed_origin or origin != allowed_origin:
        raise HTTPException(status_code=403, detail="Origin not allowed.")

    token = request.cookies.get("emg_session")
    contractor = authenticate_contractor_session(token or "")

    if contractor is None:
        raise HTTPException(
            status_code=401,
            detail="A valid contractor session is required.",
        )

    return contractor


@router.post("/jobs/{job_id}/collections", status_code=201)
def report_job_collection(
    job_id: str,
    body: CollectionReport,
    request: Request,
):
    contractor = _authenticated_contractor(request)
    idempotency_key = request.headers.get("Idempotency-Key")

    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=422,
            detail="Provide an Idempotency-Key header.",
        )

    try:
        return record_collection(
            public_job_id=job_id,
            contractor_pk=contractor["contractor_pk"],
            account_id=contractor["account_id"],
            amount=body.amount,
            collected_at=body.collected_at,
            idempotency_key=idempotency_key,
        )
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@router.post("/jobs/{job_id}/payment-agreement", status_code=201)
def create_job_payment_agreement(
    job_id: str,
    body: PaymentAgreementRequest,
    request: Request,
):
    contractor = _authenticated_contractor(request)

    try:
        return create_payment_agreement(
            public_job_id=job_id,
            contractor_pk=contractor["contractor_pk"],
            account_id=contractor["account_id"],
            arrangement_type=body.arrangement_type,
            invoice_total=body.invoice_total,
            schedule_type=body.schedule_type,
            installment_amount=body.installment_amount,
            final_installment_amount=body.final_installment_amount,
            plan_start_date=body.plan_start_date,
            final_due_date=body.final_due_date,
            custom_schedule=body.custom_schedule,
        )
    except AgreementJobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except InvalidAgreementError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@router.get("/jobs/{job_id}/payment-agreement")
def read_job_payment_agreement(
    job_id: str,
    request: Request,
):
    contractor = _authenticated_contractor(request)

    try:
        return get_payment_agreement(
            public_job_id=job_id,
            contractor_pk=contractor["contractor_pk"],
        )
    except AgreementJobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
