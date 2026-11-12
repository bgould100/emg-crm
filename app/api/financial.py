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
    AgreementConflictError,
    AgreementJobNotFoundError,
    InvalidAgreementError,
    accept_payment_agreement,
    create_payment_agreement,
    get_payment_agreement,
    request_emg_payment_agreement_review,
)


router = APIRouter(prefix="/contractor", tags=["contractor-financials"])


class CollectionReport(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    collected_at: datetime

class EMGPaymentAgreementReviewRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)

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
    down_payment_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2
    )
    down_payment_due_date: date | None = None
    installment_count: int | None = Field(default=None, gt=0)

class ContractorPaymentAgreementRequest(BaseModel):
    arrangement_type: str
    invoice_total: Decimal = Field(
        gt=0, max_digits=12, decimal_places=2
    )
    schedule_type: str | None = None
    term_months: int | None = Field(default=None, gt=0)
    installment_amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    final_installment_amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    plan_start_date: date | None = None
    final_due_date: date | None = None
    custom_schedule: list[dict[str, Any]] | None = None
    down_payment_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2
    )
    down_payment_due_date: date | None = None
    installment_count: int | None = Field(default=None, gt=0)

def _authenticated_contractor(request: Request):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        allowed_origin = os.getenv("EMG_ALLOWED_ORIGIN")
        origin = request.headers.get("origin")

        if (
            not allowed_origin
            or not origin
            or origin.rstrip("/") != allowed_origin.rstrip("/")
        ):
            raise HTTPException(
                status_code=403,
                detail="Origin not allowed.",
            )

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
    body: ContractorPaymentAgreementRequest,
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
            down_payment_amount=body.down_payment_amount,
            down_payment_due_date=body.down_payment_due_date,
            installment_count=body.installment_count,
            schedule_type=body.schedule_type,
            term_months=body.term_months,
            installment_amount=body.installment_amount,
            final_installment_amount=body.final_installment_amount,
            plan_start_date=body.plan_start_date,
            final_due_date=body.final_due_date,
            custom_schedule=body.custom_schedule,
        )
    except AgreementJobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except AgreementConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except InvalidAgreementError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))

@router.post(
    "/jobs/{job_id}/payment-agreement/{agreement_id}/confirm-paid-in-full"
)
def confirm_job_paid_in_full(
    job_id: str,
    agreement_id: int,
    request: Request,
):
    contractor = _authenticated_contractor(request)

    try:
        return record_collection(
            public_job_id=job_id,
            contractor_pk=contractor["contractor_pk"],
            account_id=contractor["account_id"],
            paid_in_full_agreement_id=agreement_id,
        )
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))

@router.post("/jobs/{job_id}/payment-agreement/accept")
def accept_job_payment_agreement(
    job_id: str,
    request: Request,
):
    contractor = _authenticated_contractor(request)

    try:
        return accept_payment_agreement(
            public_job_id=job_id,
            contractor_pk=contractor["contractor_pk"],
            account_id=contractor["account_id"],
        )
    except AgreementJobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except AgreementConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except InvalidAgreementError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))

@router.post("/jobs/{job_id}/payment-agreement/request-emg-review")
def request_job_payment_agreement_review(
    job_id: str,
    body: EMGPaymentAgreementReviewRequest,
    request: Request,
):
    contractor = _authenticated_contractor(request)

    try:
        return request_emg_payment_agreement_review(
            public_job_id=job_id,
            contractor_pk=contractor["contractor_pk"],
            account_id=contractor["account_id"],
            note=body.note,
        )
    except AgreementJobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found.")
    except AgreementConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except InvalidAgreementError as error:
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
