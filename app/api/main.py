from fastapi.staticfiles import StaticFiles
import os
import secrets
import uuid
from typing import Literal
import re

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field

from app.database.auth import (
    authenticate_contractor_session,
    login_contractor,
    logout_contractor,
    provision_contractor_account,
)
from app.database.admin_auth import (
    authenticate_admin_session,
    login_admin,
    logout_admin,
)
from app.database.connection import get_connection
from app.database.leads import create_lead, get_lead_by_lead_id
from app.database.contractors import (
    create_contractor,
    list_available_contractors,
    update_contractor_matching_profile,
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

from app.services.login_rate_limit import (
    is_login_rate_limited,
    record_login_failure,
)

from app.api.financial import router as financial_router
def normalize_us_phone(phone: str) -> str:
    """Validate a US phone number and normalize it to 10 digits."""

    phone = phone.strip()

    # Allow digits, spaces, parentheses, hyphens, periods, and
    # an optional leading + for an international-style country code.
    if not re.fullmatch(r"\+?[\d\s().-]+", phone):
        raise HTTPException(
            status_code=422,
            detail="Enter a valid US phone number.",
        )

    digits = re.sub(r"\D", "", phone)

    # Accept 10 digits, or 11 digits beginning with the US country code 1.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        raise HTTPException(
            status_code=422,
            detail="Enter a 10-digit US phone number, optionally with country code +1.",
        )

    # US area codes and exchange codes cannot begin with 0 or 1.
    if digits[0] not in "23456789" or digits[3] not in "23456789":
        raise HTTPException(
            status_code=422,
            detail="Enter a valid US phone number.",
        )

    return digits
app = FastAPI(
    title="EliteMatch Group API",
    description="Homeowner lead intake and contractor referral backend.",
    version="0.2.0",
)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"),
    name="static",
)
app.include_router(financial_router)
@app.get("/", include_in_schema=False)
def contractor_portal():
    frontend_path = Path(__file__).resolve().parent.parent / "static" / "index.html"
    return FileResponse(frontend_path)

@app.get("/homeowner", include_in_schema=False)
def homeowner_portal():
    frontend_path = Path(__file__).resolve().parent.parent / "static" / "homeowner.html"
    return FileResponse(frontend_path)


@app.get("/admin", include_in_schema=False)
def admin_portal():
    frontend_path = Path(__file__).resolve().parent.parent / "static" / "admin.html"
    return FileResponse(frontend_path)

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
    customer_phone: str = Field(min_length=1, max_length=40)


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

class ContractorMatchingProfileUpdate(BaseModel):
    service_area: list[str] = Field(min_length=1, max_length=100)
    specialties: list[str] = Field(min_length=1, max_length=50)

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

class ContractorInboxOpportunity(BaseModel):
    opportunity_id: str
    lead_id: str
    status: str
    match_score: int | None = None
    sent_at: str
    expires_at: str | None = None
    expiration_paused: bool
    zip_code: str
    service_type: str
    description: str
    requested_bid_count: int


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



class ContractorJobResponse(BaseModel):
    job_id: str
    status: str
    zip_code: str
    service_type: str
    description: str
    quoted_amount: float | None = None
    created_at: str
    updated_at: str


class ContractorJobDetailResponse(ContractorJobResponse):
    customer_name: str | None = None
    customer_phone: str | None = None
    customer_email: str | None = None


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

class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=1024)

class ContractorAccountCreate(BaseModel):
    contractor_id: str = Field(min_length=1, max_length=30)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=1024)


# -------------------------
# Admin authentication
# -------------------------

def require_admin(request: Request) -> dict:
    """Return the authenticated administrator or reject the request."""

    token = request.cookies.get("emg_admin_session")
    admin = authenticate_admin_session(token or "")

    if admin is None:
        raise HTTPException(
            status_code=401,
            detail="A valid administrator session is required.",
        )

    return admin


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
# Administrator authentication
# -------------------------

@app.post("/admin-auth/login")
def admin_login(
    request: Request,
    login_input: AdminLoginRequest,
    response: Response,
):
    require_allowed_origin(request)

    try:
        admin = login_admin(
            username=login_input.username,
            password=login_input.password,
        )
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password.",
        )

    response.set_cookie(
        key="emg_admin_session",
        value=admin["session_token"],
        max_age=admin["expires_in"],
        httponly=True,
        secure=contractor_cookie_secure(),
        samesite="lax",
        path="/",
    )

    return {
        "authenticated": True,
        "username": admin["username"],
        "expires_in": admin["expires_in"],
    }


@app.get("/admin-auth/me")
def admin_session_status(request: Request):
    admin = require_admin(request)
    return {
        "authenticated": True,
        "username": admin["username"],
    }


@app.post("/admin-auth/logout")
def admin_logout(request: Request, response: Response):
    require_allowed_origin(request)

    token = request.cookies.get("emg_admin_session")
    logout_admin(token or "")

    response.delete_cookie(
        key="emg_admin_session",
        path="/",
        httponly=True,
        secure=contractor_cookie_secure(),
        samesite="lax",
    )

    return {"authenticated": False}

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

    client_ip = request.client.host if request.client else "unknown"

    try:
        if is_login_rate_limited(
            email=login_input.email,
            client_ip=client_ip,
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Please try again in 15 minutes.",
                headers={"Retry-After": "900"},
            )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="Login is temporarily unavailable. Please try again later.",
        ) from error

    try:
        session = login_contractor(
            email=login_input.email,
            password=login_input.password,
        )
    except ValueError as error:
        try:
            record_login_failure(
                email=login_input.email,
                client_ip=client_ip,
            )
        except Exception as rate_limit_error:
            raise HTTPException(
                status_code=503,
                detail="Login is temporarily unavailable. Please try again later.",
            ) from rate_limit_error

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

@app.get("/contractor/profile")
def read_contractor_matching_profile(request: Request):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT contractor_id, company_name, service_area,
                       specialties, availability, status
                FROM contractors
                WHERE id = %s
                """,
                (contractor["contractor_pk"],),
            )
            profile = cursor.fetchone()

    if profile is None:
        raise HTTPException(status_code=404, detail="Contractor profile not found.")

    return {
        "contractor_id": profile["contractor_id"],
        "company_name": profile["company_name"],
        "service_area": profile["service_area"].split(","),
        "specialties": profile["specialties"].split(","),
        "availability": profile["availability"],
        "status": profile["status"],
    }


@app.put("/contractor/profile")
def update_contractor_profile(
    profile_input: ContractorMatchingProfileUpdate,
    request: Request,
):
    require_allowed_origin(request)
    contractor = require_contractor(request)

    try:
        profile = update_contractor_matching_profile(
            contractor_pk=contractor["contractor_pk"],
            service_area=profile_input.service_area,
            specialties=profile_input.specialties,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return {
        "message": "Matching profile updated.",
        "contractor_id": profile["contractor_id"],
        "company_name": profile["company_name"],
        "service_area": profile["service_area"].split(","),
        "specialties": profile["specialties"].split(","),
        "availability": profile["availability"],
        "status": profile["status"],
    }

@app.get(
    "/contractor/opportunities",
    response_model=list[ContractorInboxOpportunity],
)
def contractor_opportunity_inbox(request: Request):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    o.opportunity_id,
                    l.lead_id,
                    o.status,
                    o.match_score,
                    o.sent_at,
                    o.expires_at,
                    o.expiration_paused,
                    l.zip_code,
                    l.service_type,
                    l.description,
                    l.requested_bid_count
                FROM opportunities AS o
                JOIN leads AS l ON l.id = o.lead_id
                JOIN contractors AS c ON c.id = o.contractor_id
                WHERE c.contractor_id = %s
                    AND o.status = 'PENDING'
                ORDER BY o.sent_at DESC, o.id DESC
                """,
                (contractor["contractor_id"],),
            )
            rows = cursor.fetchall()

    return [
        {
            **row,
            "sent_at": row["sent_at"].isoformat(),
            "expires_at": (
                row["expires_at"].isoformat()
                if row["expires_at"] is not None
                else None
            ),
        }
        for row in rows
    ]

@app.get(
    "/contractor/opportunities/{opportunity_id}",
    response_model=ContractorInboxOpportunity,
)
def contractor_opportunity_detail(
    opportunity_id: str,
    request: Request,
):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    o.opportunity_id,
                    l.lead_id,
                    o.status,
                    o.match_score,
                    o.sent_at,
                    o.expires_at,
                    o.expiration_paused,
                    l.zip_code,
                    l.service_type,
                    l.description,
                    l.requested_bid_count
                FROM opportunities AS o
                JOIN leads AS l ON l.id = o.lead_id
                JOIN contractors AS c ON c.id = o.contractor_id
                WHERE o.opportunity_id = %s
                  AND c.contractor_id = %s
                """,
                (
                    opportunity_id,
                    contractor["contractor_id"],
                ),
            )
            row = cursor.fetchone()

    # Use the same response for nonexistent and unowned opportunities.
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Opportunity not found.",
        )

    return {
        **row,
        "sent_at": row["sent_at"].isoformat(),
        "expires_at": (
            row["expires_at"].isoformat()
            if row["expires_at"] is not None
            else None
        ),
    }

@app.get("/contractor/opportunities/{opportunity_id}/photos")
def contractor_opportunity_photos(
    opportunity_id: str,
    request: Request,
):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.photo_id,
                    p.original_filename,
                    p.content_type,
                    p.uploaded_at
                FROM photos AS p
                JOIN opportunities AS o ON o.lead_id = p.lead_id
                JOIN contractors AS c ON c.id = o.contractor_id
                WHERE o.opportunity_id = %s
                  AND c.contractor_id = %s
                  AND (
                      p.contractor_id IS NULL
                      OR p.contractor_id = c.id
                  )
                  AND (
                      p.job_id IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM jobs AS j
                          WHERE j.id = p.job_id
                            AND j.opportunity_id = o.id
                      )
                  )
                ORDER BY p.uploaded_at, p.id
                """,
                (opportunity_id, contractor["contractor_id"]),
            )
            rows = cursor.fetchall()

    return [
        {
            "photo_id": row["photo_id"],
            "filename": row["original_filename"],
            "content_type": row["content_type"],
            "uploaded_at": row["uploaded_at"].isoformat(),
            "url": f"/contractor/photos/{row['photo_id']}",
        }
        for row in rows
    ]


@app.get("/contractor/jobs/{job_id}/photos")
def contractor_job_photos(
    job_id: str,
    request: Request,
):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.photo_id,
                    p.original_filename,
                    p.content_type,
                    p.uploaded_at
                FROM photos AS p
                JOIN jobs AS j ON j.lead_id = p.lead_id
                WHERE j.job_id = %s
                  AND j.contractor_id = %s
                  AND (
                      p.contractor_id IS NULL
                      OR p.contractor_id = j.contractor_id
                  )
                  AND (
                      p.job_id IS NULL
                      OR p.job_id = j.id
                  )
                ORDER BY p.uploaded_at, p.id
                """,
                (job_id, contractor["contractor_pk"]),
            )
            rows = cursor.fetchall()

    return [
        {
            "photo_id": row["photo_id"],
            "filename": row["original_filename"],
            "content_type": row["content_type"],
            "uploaded_at": row["uploaded_at"].isoformat(),
            "url": f"/contractor/photos/{row['photo_id']}",
        }
        for row in rows
    ]


@app.get("/contractor/photos/{photo_id}")
def contractor_photo(
    photo_id: str,
    request: Request,
):
    contractor = require_contractor(request)
    contractor_pk = contractor["contractor_pk"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.s3_key,
                    p.content_type
                FROM photos AS p
                WHERE p.photo_id = %s
                  AND (
                      p.contractor_id IS NULL
                      OR p.contractor_id = %s
                  )
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM opportunities AS o
                          WHERE o.lead_id = p.lead_id
                            AND o.contractor_id = %s
                            AND (
                                p.job_id IS NULL
                                OR EXISTS (
                                    SELECT 1
                                    FROM jobs AS j
                                    WHERE j.id = p.job_id
                                      AND j.opportunity_id = o.id
                                )
                            )
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM jobs AS j
                          WHERE j.lead_id = p.lead_id
                            AND j.contractor_id = %s
                            AND (
                                p.job_id IS NULL
                                OR p.job_id = j.id
                            )
                      )
                  )
                """,
                (
                    photo_id,
                    contractor_pk,
                    contractor_pk,
                    contractor_pk,
                ),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    s3_key = row["s3_key"]

    # This endpoint serves local development files only.
    # Never interpret a database value as an arbitrary filesystem path.
    if not s3_key.startswith("local/"):
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    stored_name = s3_key.removeprefix("local/")

    if (
        not stored_name
        or Path(stored_name).name != stored_name
        or "/" in stored_name
        or "\\" in stored_name
    ):
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    upload_dir = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "uploads"
    )
    file_path = upload_dir / stored_name

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Photo file not found.",
        )

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
    content_type = row["content_type"]

    if content_type not in allowed_types:
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    return FileResponse(
        path=file_path,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/admin/overview")
def admin_overview(request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM leads) AS total_leads,
                    (SELECT COUNT(*) FROM contractors) AS total_contractors,
                    (SELECT COUNT(*) FROM opportunities
                     WHERE status = 'PENDING') AS pending_opportunities,
                    (SELECT COUNT(*) FROM jobs) AS total_jobs
            """)
            counts = cur.fetchone()

            cur.execute("""
                SELECT COALESCE(SUM(a.fee_amount), 0) AS accrued_referral_fees
                FROM job_fee_accruals a
                JOIN job_collections c ON c.id = a.collection_id
                WHERE a.accrual_status IN ('ACCRUED', 'INVOICED')
                  AND c.report_status = 'ACTIVE'
            """)
            financials = cur.fetchone()

    return {
        **dict(counts),
        "accrued_referral_fees": financials["accrued_referral_fees"],
        "financial_note": (
            "Accrued referral fees are not confirmation that EMG has received payment."
        ),
    }


@app.get("/admin/leads")
def admin_list_leads(request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lead_id, zip_code, service_type, description, budget,
                       urgency, requested_bid_count, status, customer_name,
                       customer_email, customer_phone, created_at, updated_at
                FROM leads
                ORDER BY created_at DESC
                LIMIT 200
            """)
            return [dict(row) for row in cur.fetchall()]


@app.get("/admin/contractors")
def admin_list_contractors(request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.contractor_id, c.company_name, c.contact_name,
                       c.email, c.phone, c.service_area, c.specialties,
                       c.availability, c.status, c.created_at,
                       (a.id IS NOT NULL) AS has_login
                FROM contractors c
                LEFT JOIN contractor_accounts a
                  ON a.contractor_id = c.id
                ORDER BY c.created_at DESC
                LIMIT 200
            """)
            rows = [dict(row) for row in cur.fetchall()]

    for row in rows:
        row["service_area"] = [
            value.strip() for value in (row["service_area"] or "").split(",")
            if value.strip()
        ]
        row["specialties"] = [
            value.strip() for value in (row["specialties"] or "").split(",")
            if value.strip()
        ]

    return rows


@app.post("/admin/contractors", response_model=ContractorResponse, status_code=201)
def admin_register_contractor(
    contractor_input: ContractorCreate,
    request: Request,
):
    require_admin(request)

    try:
        contractor = create_contractor(**contractor_input.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return {
        "contractor_id": contractor["contractor_id"],
        "company_name": contractor["company_name"],
        "service_area": contractor["service_area"].split(","),
        "specialties": contractor["specialties"].split(","),
        "availability": contractor["availability"],
    }


@app.post("/admin/contractor-accounts", status_code=201)
def admin_create_contractor_account(
    account_input: ContractorAccountCreate,
    request: Request,
):
    require_admin(request)

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
        lead_data = lead_input.model_dump()
        lead_data["customer_phone"] = normalize_us_phone(
            lead_data["customer_phone"]
        )

        lead = create_lead(**lead_data)

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

# -------------------------
# Homeowner Intake
# -------------------------

@app.post("/homeowner/submit", status_code=201)
async def submit_homeowner_request(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    zip_code: str = Form(...),
    service_type: str = Form(...),
    description: str = Form(...),
    customer_email: str | None = Form(default=None),
    urgency: str | None = Form(default=None),
    budget_range: str | None = Form(default=None),
    requested_bid_count: int = Form(default=3),
    additional_info: str | None = Form(default=None),
    photos: list[UploadFile] | None = File(default=None),
):
    require_allowed_origin(request)

    customer_name = customer_name.strip()
    customer_phone = normalize_us_phone(customer_phone)
    zip_code = zip_code.strip()
    service_type = service_type.strip()
    description = description.strip()
    customer_email = (customer_email or "").strip() or None
    additional_info = (additional_info or "").strip() or None

    if not customer_name or not customer_phone:
        raise HTTPException(
            status_code=422,
            detail="Customer name and phone are required.",
        )

    if len(zip_code) != 5 or not zip_code.isdigit():
        raise HTTPException(
            status_code=422,
            detail="Enter a valid five-digit ZIP code.",
        )

    if not service_type or not description:
        raise HTTPException(
            status_code=422,
            detail="Service type and job description are required.",
        )

    if requested_bid_count < 1 or requested_bid_count > 5:
        raise HTTPException(
            status_code=422,
            detail="Requested bids must be between 1 and 5.",
        )

    allowed_urgencies = {
        "1-2 days",
        "3-7 days",
        "1-2 weeks",
        "3+ weeks",
    }

    if urgency and urgency not in allowed_urgencies:
        raise HTTPException(
            status_code=422,
            detail="Invalid requested timeline.",
        )

    allowed_budgets = {
        "prefer_not_to_say",
        "under_1000",
        "1000_2500",
        "2500_5000",
        "5000_7500",
        "7500_10000",
        "10000_plus",
    }

    if budget_range and budget_range not in allowed_budgets:
        raise HTTPException(
            status_code=422,
            detail="Invalid budget range.",
        )

    # Validate every photo before creating the lead.
    uploaded_photos = []
    photos = photos or []

    if len(photos) > 5:
        raise HTTPException(
            status_code=422,
            detail="You can upload a maximum of five photos.",
        )

    max_photo_size = 5 * 1024 * 1024

    for photo in photos:
    # Browsers may submit an empty file entry when no photo is selected.
        if not photo.filename:
            continue

        content = await photo.read(max_photo_size + 1)

        if len(content) > max_photo_size:
            raise HTTPException(
                status_code=422,
                detail="Each photo must be 5 MB or smaller.",
            )

        if content.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            content_type = "image/png"
        elif (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
        ):
            content_type = "image/webp"
        else:
            raise HTTPException(
                status_code=422,
                detail="Photos must be valid JPEG, PNG, or WebP images.",
            )

        original_filename = Path(
            photo.filename or "homeowner-photo"
        ).name[:255]

        uploaded_photos.append(
            (uuid.uuid4().hex, content, content_type, original_filename)
        )

    # Save the lead and the additional homeowner information.
    try:
        lead = create_lead(
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            zip_code=zip_code,
            service_type=service_type,
            description=description,
            urgency=urgency,
            budget_range=budget_range,
            requested_bid_count=requested_bid_count,
            additional_info=additional_info,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from error

    # Store images privately on the local development machine.
    upload_dir = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "uploads"
    )
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                for stored_name, content, content_type, original_filename in uploaded_photos:
                    file_path = upload_dir / stored_name
                    file_path.write_bytes(content)
                    saved_files.append(file_path)

                    cursor.execute(
                        """
                        INSERT INTO photos (
                            photo_id,
                            lead_id,
                            s3_key,
                            original_filename,
                            content_type
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            f"EMGP-{stored_name[:16].upper()}",
                            lead["id"],
                            f"local/{stored_name}",
                            original_filename,
                            content_type,
                        ),
                    )
    except Exception:
        for file_path in saved_files:
            file_path.unlink(missing_ok=True)

        print(
            f"Homeowner photo storage failed for lead "
            f"{lead['lead_id']}."
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Your lead was created, but photo storage failed. "
                "Please contact EMG before resubmitting."
            ),
        )

    # Use the existing matching engine; never call it with the
    # internal numeric database ID.
    try:
        opportunities = route_opportunities_to_fill_slots(
            lead["lead_id"]
        )
        routing_status = "completed"
    except Exception:
        opportunities = []
        routing_status = "requires_review"
        print(
            f"Automatic matching failed for lead "
            f"{lead['lead_id']}."
        )

    return {
        "message": (
            "Your request has been received."
            if routing_status == "completed"
            else "Your request has been received and will be reviewed."
        ),
        "lead_id": lead["lead_id"],
        "routing_status": routing_status,
        "opportunities_created": len(opportunities),
    }

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
def register_contractor(
    contractor_input: ContractorCreate,
    request: Request,
):
    require_admin(request)
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
    request: Request,
):
    # Routing creates contractor invitations, so it is admin-protected.
    require_admin(request)

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

# -------------------------
# Contractor job list
# -------------------------

@app.get(
    "/contractor/jobs",
    response_model=list[ContractorJobResponse],
)
def list_contractor_jobs(request: Request):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    j.job_id,
                    j.status,
                    l.zip_code,
                    l.service_type,
                    l.description,
                    j.quoted_amount::float AS quoted_amount,
                    j.created_at::text AS created_at,
                    j.updated_at::text AS updated_at
                FROM jobs AS j
                JOIN leads AS l ON l.id = j.lead_id
                WHERE j.contractor_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM opportunities AS o
                      WHERE o.id = j.opportunity_id
                        AND o.status = 'ACCEPTED'
                        AND o.contractor_id = j.contractor_id
                  )
                ORDER BY j.updated_at DESC
                """,
                (contractor["contractor_pk"],),
            )
            rows = cursor.fetchall()

    return rows

@app.get(
    "/contractor/jobs/{job_id}",
    response_model=ContractorJobDetailResponse,
)
def contractor_job_detail(
    job_id: str,
    request: Request,
):
    contractor = require_contractor(request)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    j.job_id,
                    j.status,
                    l.zip_code,
                    l.service_type,
                    l.description,
                    l.customer_name,
                    l.customer_phone,
                    l.customer_email,
                    j.quoted_amount::float AS quoted_amount,
                    j.created_at::text AS created_at,
                    j.updated_at::text AS updated_at
                FROM jobs AS j
                JOIN leads AS l ON l.id = j.lead_id
                WHERE j.job_id = %s
                  AND j.contractor_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM opportunities AS o
                      WHERE o.id = j.opportunity_id
                        AND o.status = 'ACCEPTED'
                        AND o.contractor_id = j.contractor_id
                  )
                """,
                (job_id, contractor["contractor_pk"]),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )

    return row

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
    http_request: Request,
):
    admin_identity = require_admin(http_request)["username"]

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
    http_request: Request,
):
    admin_identity = require_admin(http_request)["username"]

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
    http_request: Request,
):
    admin_identity = require_admin(http_request)["username"]

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
    http_request: Request,
):
    admin_identity = require_admin(http_request)["username"]

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
    http_request: Request,
):
    admin_identity = require_admin(http_request)["username"]

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
