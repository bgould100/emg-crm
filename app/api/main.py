import os
import secrets
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.database.auth import (
    authenticate_contractor_session,
    login_contractor,
    logout_contractor,
    provision_contractor_account,
)
from app.database.connection import get_connection
from app.database.leads import create_lead, get_lead_by_lead_id
from app.database.contractors import (
    create_contractor,
    list_available_contractors,
)
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity
from app.services.job_status import update_job_status
from app.services.expiration import (
    hold_opportunity,
    extend_opportunity_deadline,
    release_opportunity_hold,
)
from app.services.slot_release import (
    release_accepted_bid_slot,
    retry_replacement_routing,
)


app = FastAPI(
    title="EliteMatch Group API",
    description="Homeowner lead intake and contractor referral backend.",
    version="0.2.0",
)


# -------------------------
# Request and response models
# -------------------------

class LeadCreate(BaseModel):
    zip_code: str = Field(min_length=5, max_length=10)
    service_type: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=5000)
    requested_bid_count: int = Field(default=1, ge=1, le=10)
    budget: float | None = Field(default=None, gt=0)
    urgency: str | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None


class LeadResponse(BaseModel):
    lead_id: str
    zip_code: str
    service_type: str
    description: str
    requested_bid_count: int
    status: str


class ContractorCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=5, max_length=255)
    contact_name: str | None = None
    phone: str | None = None
    service_area: list[str] = Field(min_length=1)
    specialties: list[str] = Field(min_length=1)


class ContractorResponse(BaseModel):
    contractor_id: str
    company_name: str
    service_area: list[str]
    specialties: list[str]
    availability: str


class OpportunityResponse(BaseModel):
    opportunity_id: str
    lead_id: str
    contractor_id: str
    company_name: str
    match_score: int
    status: str


class OpportunityDecision(BaseModel):
    decision: Literal["accept", "decline"]


class JobStatusUpdate(BaseModel):
    status: Literal[
        "QUOTE_IN_PROGRESS",
        "BID_WON",
        "BID_LOST",
        "JOB_IN_PROGRESS",
        "JOB_COMPLETED",
        "PAYMENT_RECEIVED",
    ]


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    changed: bool
    winning_contractors: int | None = None
    alert_queued: bool = False


class OpportunityDecisionResponse(BaseModel):
    opportunity_id: str
    decision: str
    status: str
    job_id: str | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    next_opportunities: list[OpportunityResponse] = Field(
        default_factory=list
    )
    message: str | None = None


class AdminReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class AdminExtendRequest(AdminReasonRequest):
    days: int = Field(ge=1, le=30)


class AdminActionResponse(BaseModel):
    opportunity_id: str
    action: str
    message: str


class AdminSlotReleaseResponse(BaseModel):
    opportunity_id: str
    action: str
    message: str
    replacement_count: int


class ContractorLoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class ContractorAccountCreate(BaseModel):
    contractor_id: str = Field(min_length=1, max_length=30)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=1024)


# -------------------------
# Admin authentication
# -------------------------

def require_admin(x_admin_token: str | None) -> str:
    """Fail closed unless the configured admin token is valid."""

    configured_token = os.getenv("EMG_ADMIN_TOKEN")

    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="Admin controls are disabled: EMG_ADMIN_TOKEN is not set.",
        )

    if (
        not x_admin_token
        or not secrets.compare_digest(x_admin_token, configured_token)
    ):
        raise HTTPException(
            status_code=401,
            detail="A valid admin token is required.",
        )

    return os.getenv("EMG_ADMIN_IDENTITY", "configured-admin")


# -------------------------
# Browser request protection
# -------------------------

def require_allowed_origin(request: Request) -> None:
    """Require the configured origin for browser state-changing requests."""

    allowed_origin = os.getenv("EMG_ALLOWED_ORIGIN")
    request_origin = request.headers.get("origin")

    if not allowed_origin:
        raise HTTPException(
            status_code=503,
            detail="Browser origin protection is not configured.",
        )

    if (
        not request_origin
        or not secrets.compare_digest(
            request_origin.rstrip("/"),
            allowed_origin.rstrip("/"),
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="Request origin is not allowed.",
        )


def contractor_cookie_secure() -> bool:
    """Use Secure cookies when the deployment runs over HTTPS."""

    return (
        os.getenv("EMG_COOKIE_SECURE", "false").strip().lower() == "true"
    )


# -------------------------
# Contractor authentication
# -------------------------

def require_contractor(request: Request) -> dict:
    """Return the authenticated contractor or reject the request."""

    token = request.cookies.get("emg_session")
    contractor = authenticate_contractor_session(token or "")

    if contractor is None:
        raise HTTPException(
            status_code=401,
            detail="A valid contractor session is required.",
        )

    return contractor


@app.post("/contractor-auth/login")
def contractor_login(
    request: Request,
    login_input: ContractorLoginRequest,
    response: Response,
):
    require_allowed_origin(request)

    try:
        session = login_contractor(
            email=login_input.email,
            password=login_input.password,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        ) from error

    response.set_cookie(
        key="emg_session",
        value=session["session_token"],
        max_age=session["expires_in"],
        httponly=True,
        secure=contractor_cookie_secure(),
        samesite="lax",
        path="/",
    )

    return {
        "contractor_id": session["contractor_id"],
        "company_name": session["company_name"],
        "email": session["email"],
        "expires_in": session["expires_in"],
    }


@app.post("/contractor-auth/logout")
def contractor_logout(
    request: Request,
    response: Response,
):
    require_allowed_origin(request)

    token = request.cookies.get("emg_session")
    logout_contractor(token or "")

    response.delete_cookie(
        key="emg_session",
        path="/",
        httponly=True,
        secure=contractor_cookie_secure(),
        samesite="lax",
    )

    return {"message": "Logged out."}


@app.get("/contractor-auth/me")
def contractor_me(request: Request):
    contractor = require_contractor(request)

    return {
        "contractor_id": contractor["contractor_id"],
        "company_name": contractor["company_name"],
        "email": contractor["email"],
    }


@app.post("/admin/contractor-accounts", status_code=201)
def admin_create_contractor_account(
    account_input: ContractorAccountCreate,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    require_admin(x_admin_token)

    try:
        account = provision_contractor_account(
            contractor_id=account_input.contractor_id,
            email=account_input.email,
            password=account_input.password,
        )
    except ValueError as error:
        message = str(error)
        status_code = 409 if "already exists" in message else 422

        raise HTTPException(
            status_code=status_code,
            detail=message,
        ) from error

    return {
        "account_id": account["id"],
        "email": account["email"],
        "status": account["status"],
    }


# -------------------------
# Health
# -------------------------

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "emg-api"}


# -------------------------
# Leads
# -------------------------

@app.post("/leads", response_model=LeadResponse, status_code=201)
def submit_lead(lead_input: LeadCreate):
    try:
        lead = create_lead(**lead_input.model_dump())

        return {
            "lead_id": lead["lead_id"],
            "zip_code": lead["zip_code"],
            "service_type": lead["service_type"],
            "description": lead["description"],
            "requested_bid_count": lead["requested_bid_count"],
            "status": lead["status"],
        }
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from error


@app.get("/leads/{lead_id}", response_model=LeadResponse)
def read_lead(lead_id: str):
    lead = get_lead_by_lead_id(lead_id)

    if lead is None:
        raise HTTPException(
            status_code=404,
            detail="Lead not found.",
        )

    return {
        "lead_id": lead["lead_id"],
        "zip_code": lead["zip_code"],
        "service_type": lead["service_type"],
        "description": lead["description"],
        "requested_bid_count": lead["requested_bid_count"],
        "status": lead["status"],
    }


# -------------------------
# Contractors
# -------------------------

@app.post(
    "/contractors",
    response_model=ContractorResponse,
    status_code=201,
)
def register_contractor(contractor_input: ContractorCreate):
    try:
        contractor = create_contractor(**contractor_input.model_dump())

        return {
            "contractor_id": contractor["contractor_id"],
            "company_name": contractor["company_name"],
            "service_area": contractor["service_area"].split(","),
            "specialties": contractor["specialties"].split(","),
            "availability": contractor["availability"],
        }
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from error


@app.get("/contractors", response_model=list[ContractorResponse])
def read_available_contractors():
    contractors = list_available_contractors()

    return [
        {
            "contractor_id": contractor["contractor_id"],
            "company_name": contractor["company_name"],
            "service_area": contractor["service_area"].split(","),
            "specialties": contractor["specialties"].split(","),
            "availability": contractor["availability"],
        }
        for contractor in contractors
    ]


# -------------------------
# Opportunity routing and responses
# -------------------------

@app.post(
    "/leads/{lead_id}/route",
    response_model=list[OpportunityResponse],
    status_code=201,
)
def route_lead(
    lead_id: str,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    # Routing creates contractor invitations, so it is admin-protected.
    require_admin(x_admin_token)

    try:
        opportunities = route_opportunities_to_fill_slots(lead_id)
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error

    if not opportunities:
        raise HTTPException(
            status_code=409,
            detail=(
                "No open bid slots can be filled. The requested bid count "
                "may already be covered, invitations may be pending, or "
                "no eligible contractors remain."
            ),
        )

    return opportunities


@app.post(
    "/opportunities/{opportunity_id}/respond",
    response_model=OpportunityDecisionResponse,
)
def decide_opportunity(
    opportunity_id: str,
    decision_input: OpportunityDecision,
    request: Request,
):
    require_allowed_origin(request)
    contractor = require_contractor(request)

    # Verify ownership before changing state or returning homeowner details.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM opportunities
                WHERE opportunity_id = %s
                  AND contractor_id = %s
                """,
                (
                    opportunity_id,
                    contractor["contractor_pk"],
                ),
            )
            owns_opportunity = cursor.fetchone() is not None

    if not owns_opportunity:
        raise HTTPException(
            status_code=404,
            detail="Opportunity not found.",
        )

    try:
        result = respond_to_opportunity(
            opportunity_id,
            decision_input.decision,
            contractor["contractor_pk"],
        )
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error

    # Do not reveal other contractors' invitations to this contractor.
    result["next_opportunities"] = []

    return result


# -------------------------
# Contractor job status
# -------------------------

@app.patch(
    "/jobs/{job_id}/status",
    response_model=JobStatusResponse,
)
def change_job_status(
    job_id: str,
    status_input: JobStatusUpdate,
    request: Request,
):
    require_allowed_origin(request)
    contractor = require_contractor(request)

    # Verify ownership before invoking the status service.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM jobs
                WHERE job_id = %s
                  AND contractor_id = %s
                """,
                (
                    job_id,
                    contractor["contractor_pk"],
                ),
            )
            owns_job = cursor.fetchone() is not None

    if not owns_job:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )

    try:
        result = update_job_status(
            job_id,
            status_input.status,
            contractor["contractor_pk"],
        )
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error

    return result


# -------------------------
# Admin-only expiration controls
# -------------------------

@app.post(
    "/admin/opportunities/{opportunity_id}/hold",
    response_model=AdminActionResponse,
)
def admin_hold_opportunity(
    opportunity_id: str,
    request: AdminReasonRequest,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    admin_identity = require_admin(x_admin_token)

    try:
        hold_opportunity(
            opportunity_id,
            reason=request.reason,
            performed_by=admin_identity,
        )
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error

    return {
        "opportunity_id": opportunity_id,
        "action": "HOLD",
        "message": (
            "Expiration monitoring has been paused for this opportunity."
        ),
    }


@app.post(
    "/admin/opportunities/{opportunity_id}/extend",
    response_model=AdminActionResponse,
)
def admin_extend_opportunity(
    opportunity_id: str,
    request: AdminExtendRequest,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    admin_identity = require_admin(x_admin_token)

    try:
        extend_opportunity_deadline(
            opportunity_id,
            days=request.days,
            reason=request.reason,
            performed_by=admin_identity,
        )
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error

    return {
        "opportunity_id": opportunity_id,
        "action": "EXTEND",
        "message": (
            f"The expiration deadline was extended by {request.days} day(s)."
        ),
    }


@app.post(
    "/admin/opportunities/{opportunity_id}/release-hold",
    response_model=AdminActionResponse,
)
def admin_release_opportunity_hold(
    opportunity_id: str,
    request: AdminReasonRequest,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    admin_identity = require_admin(x_admin_token)

    try:
        release_opportunity_hold(
            opportunity_id,
            reason=request.reason,
            performed_by=admin_identity,
        )
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error

    return {
        "opportunity_id": opportunity_id,
        "action": "RELEASE_HOLD",
        "message": "The expiration hold has been released.",
    }



@app.post(
    "/admin/opportunities/{opportunity_id}/release-slot",
    response_model=AdminSlotReleaseResponse,
)
def admin_release_accepted_slot(
    opportunity_id: str,
    request: AdminReasonRequest,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    admin_identity = require_admin(x_admin_token)

    try:
        result = release_accepted_bid_slot(
            opportunity_id,
            reason=request.reason,
            performed_by=admin_identity,
        )
    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error

    replacement_count = result["replacement_count"]

    return {
        "opportunity_id": opportunity_id,
        "action": result["action"],
        "message": (
            "The accepted bid slot was released. "
            f"{replacement_count} replacement invitation(s) created."
        ),
        "replacement_count": replacement_count,
    }



@app.post(
    "/admin/opportunities/{opportunity_id}/retry-slot-routing",
    response_model=AdminSlotReleaseResponse,
)
def admin_retry_slot_routing(
    opportunity_id: str,
    request: AdminReasonRequest,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    admin_identity = require_admin(x_admin_token)

    try:
        result = retry_replacement_routing(
            opportunity_id,
            reason=request.reason,
            performed_by=admin_identity,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    replacement_count = result["replacement_count"]
    return {
        "opportunity_id": opportunity_id,
        "action": result["action"],
        "message": (
            "Replacement routing retry completed. "
            f"{replacement_count} replacement invitation(s) created."
        ),
        "replacement_count": replacement_count,
    }
