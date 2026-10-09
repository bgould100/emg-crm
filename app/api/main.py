
import os
import secrets
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.database.leads import create_lead, get_lead_by_lead_id
from app.database.contractors import (
    create_contractor,
    list_available_contractors,
)
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity
from app.services.expiration import (
    hold_opportunity,
    extend_opportunity_deadline,
    release_opportunity_hold,
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


# -------------------------
# Admin authentication
# -------------------------

def require_admin(x_admin_token: str | None) -> str:
    """Validate the configured development admin token."""

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

    # This identifies the configured admin credential, not an individual
    # authenticated user. Replace with real user identity when proper
    # account-based authentication is implemented.
    return os.getenv("EMG_ADMIN_IDENTITY", "configured-admin")


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
        raise HTTPException(status_code=404, detail="Lead not found.")

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
def route_lead(lead_id: str):
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
    request: OpportunityDecision,
):
    try:
        return respond_to_opportunity(
            opportunity_id,
            request.decision,
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
        "message": "Expiration monitoring has been paused for this opportunity.",
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
        "message": f"The expiration deadline was extended by {request.days} day(s).",
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