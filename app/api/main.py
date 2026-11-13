from fastapi.staticfiles import StaticFiles
import os
import secrets
import uuid
from typing import Literal
import re
import io
import traceback
from decimal import Decimal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)
from PIL import Image, UnidentifiedImageError

from app.database.auth import (
    authenticate_contractor_session,
    change_contractor_password,
    login_contractor,
    logout_contractor,
    provision_contractor_account,
    complete_required_password_change,
    reset_contractor_password,
)
from app.database.admin_auth import (
    authenticate_admin_session,
    login_admin,
    logout_admin,
)
from app.database.connection import get_connection
from app.services.photo_storage import (
    save_photo,
    read_photo,
    delete_photo,
)
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

from app.api.financial import (
    PaymentAgreementRequest,
    router as financial_router,
)
from app.services.payment_agreements import (
    AgreementConflictError,
    AgreementJobNotFoundError,
    InvalidAgreementError,
    get_admin_payment_agreement,
    revise_payment_agreement,
)

from app.services.homeowner_reviews import (
    InvalidHomeownerReviewError,
    InvalidReviewTokenError,
    ReviewAlreadySubmittedError,
    ReviewExpiredError,
    get_homeowner_review_detail,
    list_homeowner_reviews,
    submit_homeowner_review,
    validate_homeowner_review_invitation,
)

from app.services.homeowner_review_delivery import (
    ReviewInvitationConflictError,
    ReviewInvitationNotFoundError,
    list_homeowner_review_invitations,
    resolve_homeowner_review_invitation,
    resume_homeowner_review_invitation,
    stop_homeowner_review_invitation,
)

from app.services.review_tokens import (
    generate_review_token,
    review_token_matches,
)

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

class HomeownerRequestBodyLimitMiddleware:
    """Limit the total body size of homeowner form submissions."""
    def __init__(self, app):
        self.app = app
    MAX_BODY_SIZE = 30 * 1024 * 1024

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != "/homeowner/submit"
        ):
            await self.app(scope, receive, send)
            return

        content_length = next(
            (
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ),
            None,
        )

        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self._reject(send, 400, "Invalid Content-Length header.")
                return

            if declared_size < 0:
                await self._reject(send, 400, "Invalid Content-Length header.")
                return

            if declared_size > self.MAX_BODY_SIZE:
                await self._reject(send, 413, "Submission is too large. Reduce the total upload size to 30 MiB or less.")
                return

        chunks = []
        total_size = 0

        while True:
            message = await receive()

            if message["type"] == "http.disconnect":
                return

            if message["type"] != "http.request":
                continue

            chunk = message.get("body", b"")
            total_size += len(chunk)

            if total_size > self.MAX_BODY_SIZE:
                await self._reject(
                    send,
                    413,
                    "Submission is too large. Reduce the total upload size to 30 MiB or less.",
                )
                return

            chunks.append(message)

            if not message.get("more_body", False):
                break

        index = 0

        async def replay_body():
            nonlocal index

            if index < len(chunks):
                message = chunks[index]
                index += 1
                return message

            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_body, send)

    async def _reject(self, send, status_code, detail):
        import json

        body = json.dumps({"detail": detail}).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

app = FastAPI(
    title="EliteMatch Group API",
    description="Homeowner lead intake and contractor referral backend.",
    version="0.2.0",
)
app.add_middleware(HomeownerRequestBodyLimitMiddleware)

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

@app.get("/homeowner/review", include_in_schema=False)
def homeowner_review_portal():
    frontend_path = (
        Path(__file__).resolve().parent.parent
        / "static"
        / "homeowner_review.html"
    )
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

class ContractorReferralFeeRateUpdate(BaseModel):
    referral_fee_rate: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("0.10"),
        multiple_of=Decimal("0.05"),
    )

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
    budget_range: str | None = None
    urgency: str | None = None


class OpportunityDecision(BaseModel):
    decision: Literal["accept", "decline"]

class ContractorJobNoteCreate(BaseModel):
    note_text: str = Field(min_length=1, max_length=600)

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
    budget_range: str | None = None
    urgency: str | None = None
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

class AdminReviewInvitationResolutionRequest(BaseModel):
    action: Literal["RESEND", "DO_NOT_RESEND"]
    reason: str = Field(min_length=3, max_length=1000)

class HomeownerReviewSubmission(BaseModel):
    token: str = Field(min_length=1, max_length=256)

    rating: StrictInt = Field(ge=1, le=5)

    # False = Paid In Full
    # True = Put on a payment plan
    payment_plan_agreed: StrictBool

    written_feedback: str | None = Field(
        default=None,
        max_length=5000,
    )

    total_cost_submitted: StrictBool = False

    total_cost: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )

    cost_comments: str | None = Field(
        default=None,
        max_length=5000,
    )

    @model_validator(mode="after")
    def validate_cost_submission(self):
        if self.total_cost_submitted and self.total_cost is None:
            raise ValueError(
                "Enter the final total cost or indicate that you are not providing it."
            )

        if not self.total_cost_submitted and self.total_cost is not None:
            raise ValueError(
                "Total cost must be omitted when it is not being submitted."
            )

        return self

class AdminExtendRequest(AdminReasonRequest):
    days: int = Field(ge=1, le=30)

class AdminPaymentAgreementRevisionRequest(PaymentAgreementRequest):
    term_months: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=1000)

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

class ContractorPasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)

class RequiredPasswordChangeRequest(BaseModel):
    new_password: str = Field(min_length=12, max_length=1024)
    confirm_password: str = Field(min_length=12, max_length=1024)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self

class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=1024)

class ContractorAccountCreate(BaseModel):
    contractor_id: str = Field(min_length=1, max_length=30)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=1024)
    confirm_password: str = Field(min_length=12, max_length=1024)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self

class ContractorPasswordResetRequest(BaseModel):
    temporary_password: str = Field(min_length=12, max_length=1024)
    confirm_password: str = Field(min_length=12, max_length=1024)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.temporary_password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self



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

def require_contractor(
    request: Request,
    *,
    allow_password_change_required: bool = False,
) -> dict:
    """Return the authenticated contractor or reject the request."""

    token = request.cookies.get("emg_session")
    contractor = authenticate_contractor_session(token or "")

    if contractor is None:
        raise HTTPException(
            status_code=401,
            detail="A valid contractor session is required.",
        )

    if (
        contractor["must_change_password"]
        and not allow_password_change_required
    ):
        raise HTTPException(
            status_code=403,
            detail="You must change your temporary password before continuing.",
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
        "must_change_password": session["must_change_password"],
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

@app.post("/contractor-auth/change-password")
def contractor_change_password(
    request: Request,
    password_input: ContractorPasswordChangeRequest,
    response: Response,
):
    require_allowed_origin(request)
    contractor = require_contractor(request)

    try:
        change_contractor_password(
            account_id=contractor["account_id"],
            current_password=password_input.current_password,
            new_password=password_input.new_password,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    response.delete_cookie(
        key="emg_session",
        path="/",
        httponly=True,
        secure=contractor_cookie_secure(),
        samesite="lax",
    )

    return {
        "message": "Password changed successfully. Please log in again."
    }

@app.post("/contractor-auth/required-password-change")
def contractor_required_password_change(
    request: Request,
    password_input: RequiredPasswordChangeRequest,
    response: Response,
):
    require_allowed_origin(request)

    contractor = require_contractor(
        request,
        allow_password_change_required=True,
    )

    session_token = request.cookies.get("emg_session") or ""

    try:
        result = complete_required_password_change(
            account_id=contractor["account_id"],
            session_token=session_token,
            new_password=password_input.new_password,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    response.set_cookie(
        key="emg_session",
        value=result["session_token"],
        max_age=result["expires_in"],
        httponly=True,
        secure=contractor_cookie_secure(),
        samesite="lax",
        path="/",
    )

    return {"message": "Password changed successfully."}

@app.get("/contractor-auth/me")
def contractor_me(request: Request):
    contractor = require_contractor(
        request,
        allow_password_change_required=True,
    )

    return {
        "contractor_id": contractor["contractor_id"],
        "company_name": contractor["company_name"],
        "email": contractor["email"],
        "must_change_password": contractor["must_change_password"],
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
                    l.budget_range,
                    l.urgency
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
                    l.budget_range,
                    l.urgency
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
    contractor_pk = contractor["contractor_pk"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    j.status,
                    j.photos_locked_at,
                    p.photo_id,
                    p.original_filename,
                    p.content_type,
                    p.uploaded_at,
                    p.contractor_id AS photo_contractor_id
                FROM jobs AS j
                LEFT JOIN photos AS p
                  ON p.lead_id = j.lead_id
                 AND (
                     p.contractor_id IS NULL
                     OR p.contractor_id = j.contractor_id
                 )
                 AND (
                     p.job_id IS NULL
                     OR p.job_id = j.id
                 )
                WHERE j.job_id = %s
                  AND j.contractor_id = %s
                ORDER BY p.uploaded_at, p.id
                """,
                (job_id, contractor_pk),
            )
            rows = cursor.fetchall()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )

    photos_locked = (
        rows[0]["status"] != "JOB_IN_PROGRESS"
        or rows[0]["photos_locked_at"] is not None
    )

    photos = [
        {
            "photo_id": row["photo_id"],
            "filename": row["original_filename"],
            "content_type": row["content_type"],
            "uploaded_at": row["uploaded_at"].isoformat(),
            "url": f"/contractor/photos/{row['photo_id']}",
            "source": (
                "homeowner"
                if row["photo_contractor_id"] is None
                else "contractor"
            ),
            "can_delete": (
                row["photo_contractor_id"] == contractor_pk
                and not photos_locked
            ),
        }
        for row in rows
        if row["photo_id"] is not None
    ]

    return {
        "photos_locked": photos_locked,
        "photos": photos,
    }

@app.get("/contractor/jobs/{job_id}/notes")
def contractor_job_notes(
    job_id: str,
    request: Request,
):
    contractor = require_contractor(request)
    contractor_pk = contractor["contractor_pk"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    j.status,
                    j.notes_locked_at,
                    n.note_id,
                    n.note_text,
                    n.created_at,
                    c.company_name,
                    c.contact_name
                FROM jobs AS j
                LEFT JOIN job_notes AS n
                    ON n.job_id = j.id
                LEFT JOIN contractors AS c
                    ON c.id = n.contractor_id
                WHERE j.job_id = %s
                  AND j.contractor_id = %s
                ORDER BY n.created_at, n.id
                """,
                (job_id, contractor_pk),
            )
            rows = cursor.fetchall()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )

    notes_locked = (
        rows[0]["status"] != "JOB_IN_PROGRESS"
        or rows[0]["notes_locked_at"] is not None
    )

    notes = [
        {
            "note_id": row["note_id"],
            "note_text": row["note_text"],
            "created_at": row["created_at"].isoformat(),
            "author_name": (
                row["contact_name"] or row["company_name"]
            ),
        }
        for row in rows
        if row["note_id"] is not None
    ]

    return {
        "notes_locked": notes_locked,
        "notes": notes,
    }

@app.post("/contractor/jobs/{job_id}/notes", status_code=201)
def add_contractor_job_note(
    job_id: str,
    note: ContractorJobNoteCreate,
    request: Request,
):
    require_allowed_origin(request)
    contractor = require_contractor(request)
    contractor_pk = contractor["contractor_pk"]

    note_text = note.note_text.strip()

    if not note_text:
        raise HTTPException(
            status_code=422,
            detail="Note cannot be empty.",
        )

    if len(note_text) > 600:
        raise HTTPException(
            status_code=422,
            detail="Note cannot exceed 600 characters.",
        )

    note_id = f"EMGN-{uuid.uuid4().hex[:16].upper()}"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status, notes_locked_at
                FROM jobs
                WHERE job_id = %s
                  AND contractor_id = %s
                FOR UPDATE
                """,
                (job_id, contractor_pk),
            )
            locked_job = cursor.fetchone()

            if locked_job is None:
                raise HTTPException(
                    status_code=404,
                    detail="Job not found.",
                )

            if locked_job["status"] != "JOB_IN_PROGRESS":
                raise HTTPException(
                    status_code=409,
                    detail="Notes can only be added while the job is in progress.",
                )

            if locked_job["notes_locked_at"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Notes are locked for this job.",
                )

            cursor.execute(
                """
                INSERT INTO job_notes (
                    note_id,
                    job_id,
                    contractor_id,
                    note_text
                )
                VALUES (%s, %s, %s, %s)
                RETURNING note_id, note_text, created_at
                """,
                (
                    note_id,
                    locked_job["id"],
                    contractor_pk,
                    note_text,
                ),
            )
            saved_note = cursor.fetchone()

    return {
        "note_id": saved_note["note_id"],
        "note_text": saved_note["note_text"],
        "created_at": saved_note["created_at"].isoformat(),
    }

@app.post("/contractor/jobs/{job_id}/photos", status_code=201)
async def upload_contractor_job_photos(
    job_id: str,
    request: Request,
    photos: list[UploadFile] = File(...),
):
    require_allowed_origin(request)
    contractor = require_contractor(request)
    contractor_pk = contractor["contractor_pk"]

    # Verify ownership and current status using the public job ID.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT j.id AS job_pk, j.lead_id AS lead_pk, j.status, j.photos_locked_at
                FROM jobs AS j
                WHERE j.job_id = %s
                  AND j.contractor_id = %s
                """,
                (job_id, contractor_pk),
            )
            job = cursor.fetchone()

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )

    if job["status"] != "JOB_IN_PROGRESS":
        raise HTTPException(
            status_code=409,
            detail="Photo uploads are only allowed while the job is in progress.",
        )

    if job["photos_locked_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail="Photo uploads are locked for this job.",
        )

    if len(photos) > 15:
        raise HTTPException(
            status_code=422,
            detail="You can upload a maximum of fifteen photos.",
        )

    # Validate every file before saving any files or database records.
    max_photo_size = 5 * 1024 * 1024
    uploaded_photos = []

    for photo in photos:
        if not photo.filename:
            continue

        content = await photo.read(max_photo_size + 1)

        if len(content) > max_photo_size:
            raise HTTPException(
                status_code=422,
                detail="Each photo must be 5 MB or smaller.",
            )

        try:
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format
                image.verify()

            # Normalize MPO images to a standard JPEG using the
            # full-resolution first frame.
            if image_format == "MPO":
                with Image.open(io.BytesIO(content)) as image:
                    image.seek(0)
                    converted = io.BytesIO()
                    image.convert("RGB").save(
                        converted,
                        format="JPEG",
                        quality=95,
                    )
                    content = converted.getvalue()

                if len(content) > max_photo_size:
                    raise HTTPException(
                        status_code=422,
                        detail="Each photo must be 5 MB or smaller after conversion.",
                    )

                image_format = "JPEG"
            else:
                # Reopen and fully decode standard image formats.
                with Image.open(io.BytesIO(content)) as image:
                    image.load()

            content_types = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }

            content_type = content_types.get(image_format)

            if content_type is None:
                raise HTTPException(
                    status_code=422,
                    detail="Photos must be valid JPEG, PNG, or WebP images.",
                )

        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="Photos must be valid JPEG, PNG, or WebP images.",
            )

        original_filename = Path(photo.filename).name[:255]
        stored_name = uuid.uuid4().hex

        uploaded_photos.append(
            (stored_name, content, content_type, original_filename)
        )

    if not uploaded_photos:
        raise HTTPException(
            status_code=422,
            detail="Select at least one valid photo to upload.",
        )

    saved_storage_keys = []
    saved_photos = []

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, photos_locked_at
                    FROM jobs
                    WHERE id = %s
                    AND contractor_id = %s
                    FOR UPDATE
                    """,
                    (job["job_pk"], contractor_pk),
                )
                locked_job = cursor.fetchone()

                if locked_job is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Job not found.",
                    )

                if locked_job["status"] != "JOB_IN_PROGRESS":
                    raise HTTPException(
                        status_code=409,
                        detail="Photo uploads are only allowed while the job is in progress.",
                    )

                if locked_job["photos_locked_at"] is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Photo uploads are locked for this job.",
                    )
                cursor.execute(
                    """
                    SELECT COUNT(*) AS photo_count
                    FROM photos
                    WHERE job_id = %s
                      AND contractor_id = %s
                    """,
                    (job["job_pk"], contractor_pk),
                )
                existing_photo_count = cursor.fetchone()["photo_count"]

                if existing_photo_count + len(uploaded_photos) > 15:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "A maximum of fifteen photos is allowed per job. "
                            f"This job already has {existing_photo_count} photos."
                        ),
                    )
                for (
                    stored_name,
                    content,
                    content_type,
                    original_filename,
                ) in uploaded_photos:
                    storage_key = save_photo(
                        stored_name,
                        content,
                        content_type,
                    )
                    saved_storage_keys.append(storage_key)

                    photo_id = f"EMGP-{stored_name[:16].upper()}"

                    cursor.execute(
                        """
                        INSERT INTO photos (
                            photo_id,
                            lead_id,
                            contractor_id,
                            job_id,
                            s3_key,
                            original_filename,
                            content_type
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING uploaded_at
                        """,
                        (
                            photo_id,
                            job["lead_pk"],
                            contractor_pk,
                            job["job_pk"],
                            storage_key,
                            original_filename,
                            content_type,
                        ),
                    )
                    record = cursor.fetchone()

                    saved_photos.append(
                        {
                            "photo_id": photo_id,
                            "filename": original_filename,
                            "content_type": content_type,
                            "uploaded_at": record["uploaded_at"].isoformat(),
                            "url": f"/contractor/photos/{photo_id}",
                        }
                    )
    except HTTPException:
        for storage_key in saved_storage_keys:
            try:
                delete_photo(storage_key)
            except Exception as cleanup_error:
                print(
                    f"Photo cleanup failed for {storage_key}: "
                    f"{type(cleanup_error).__name__}"
                )
        raise
    except Exception as error:
        for storage_key in saved_storage_keys:
            try:
                delete_photo(storage_key)
            except Exception as cleanup_error:
                print(
                    f"Photo cleanup failed for {storage_key}: "
                    f"{type(cleanup_error).__name__}"
                )

        print(f"Contractor photo storage failed for job {job_id}.")
        raise HTTPException(
            status_code=500,
            detail="Photo storage failed. Please try again later.",
        ) from error

    return saved_photos


@app.delete("/contractor/jobs/{job_id}/photos/{photo_id}")
def delete_contractor_job_photo(
    job_id: str,
    photo_id: str,
    request: Request,
):
    require_allowed_origin(request)
    contractor = require_contractor(request)
    contractor_pk = contractor["contractor_pk"]

    storage_key = None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    j.id AS job_pk,
                    j.status,
                    j.photos_locked_at,
                    p.s3_key
                FROM jobs AS j
                JOIN photos AS p
                  ON p.job_id = j.id
                WHERE j.job_id = %s
                  AND j.contractor_id = %s
                  AND p.photo_id = %s
                  AND p.contractor_id = %s
                FOR UPDATE OF j, p
                """,
                (
                    job_id,
                    contractor_pk,
                    photo_id,
                    contractor_pk,
                ),
            )
            photo = cursor.fetchone()

            if photo is None:
                raise HTTPException(
                    status_code=404,
                    detail="Contractor photo not found.",
                )

            if (
                photo["status"] != "JOB_IN_PROGRESS"
                or photo["photos_locked_at"] is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Photo deletion is locked for this job.",
                )

            storage_key = photo["s3_key"]

            cursor.execute(
                """
                DELETE FROM photos
                WHERE photo_id = %s
                  AND job_id = %s
                  AND contractor_id = %s
                """,
                (
                    photo_id,
                    photo["job_pk"],
                    contractor_pk,
                ),
            )

            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=404,
                    detail="Contractor photo not found.",
                )

    try:
        delete_photo(storage_key)
    except Exception as error:
        print(
            f"Photo deletion failed for {photo_id}: "
            f"{type(error).__name__}"
        )
        raise HTTPException(
            status_code=500,
            detail="Photo record deleted, but photo storage cleanup failed.",
        ) from error

    return {"message": "Photo deleted successfully."}


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

    try:
        photo_content = read_photo(s3_key)
    except (FileNotFoundError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    return Response(
        content=photo_content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

@app.get("/admin/photos/{photo_id}")
def admin_photo(photo_id: str, request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s3_key, content_type
                FROM photos
                WHERE photo_id = %s
            """, (photo_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    s3_key = row["s3_key"] or ""

    content_type = row["content_type"]
    if content_type not in {
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    try:
        photo_content = read_photo(s3_key)
    except (FileNotFoundError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail="Photo not found.",
        )

    return Response(
        content=photo_content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

@app.get("/admin/jobs/{job_id}/photos")
def admin_job_photos(job_id: str, request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.photo_id,
                    p.original_filename,
                    p.content_type,
                    p.uploaded_at::text AS uploaded_at
                FROM photos AS p
                JOIN jobs AS j
                ON j.id = p.job_id
                AND j.contractor_id = p.contractor_id
                WHERE j.job_id = %s
                ORDER BY p.uploaded_at, p.id
            """, (job_id,))
            rows = cur.fetchall()

    return [
        {
            "photo_id": row["photo_id"],
            "filename": row["original_filename"] or "Job photo",
            "content_type": row["content_type"],
            "uploaded_at": row["uploaded_at"],
            "url": f"/admin/photos/{row['photo_id']}",
        }
        for row in rows
    ]

@app.get("/admin/jobs/{job_id}/notes")
def admin_job_notes(job_id: str, request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    j.job_id,
                    n.note_id,
                    n.note_text,
                    n.created_at::text AS created_at,
                    c.company_name,
                    c.contact_name
                FROM jobs AS j
                LEFT JOIN job_notes AS n
                    ON n.job_id = j.id
                LEFT JOIN contractors AS c
                    ON c.id = n.contractor_id
                WHERE j.job_id = %s
                ORDER BY n.created_at, n.id
            """, (job_id,))
            rows = cur.fetchall()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )

    return [
        {
            "note_id": row["note_id"],
            "note_text": row["note_text"],
            "created_at": row["created_at"],
            "company_name": row["company_name"],
            "contact_name": row["contact_name"],
            "author_name": (
                row["contact_name"] or row["company_name"]
            ),
        }
        for row in rows
        if row["note_id"] is not None
    ]

@app.get("/admin/leads/{lead_id}/photos")
def admin_lead_photos(lead_id: str, request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.photo_id,
                    p.original_filename,
                    p.content_type,
                    p.uploaded_at::text AS uploaded_at
                FROM photos AS p
                JOIN leads AS l ON l.id = p.lead_id
                WHERE l.lead_id = %s
                  AND p.job_id IS NULL
                  AND p.contractor_id IS NULL
                ORDER BY p.uploaded_at, p.id
            """, (lead_id,))
            rows = cur.fetchall()

    return [
        {
            "photo_id": row["photo_id"],
            "filename": row["original_filename"] or "Homeowner photo",
            "content_type": row["content_type"],
            "uploaded_at": row["uploaded_at"],
            "url": f"/admin/photos/{row['photo_id']}",
        }
        for row in rows
    ]

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
                       customer_email, customer_phone, created_at, updated_at,
                       routing_error, routing_failed_at
                FROM leads
                ORDER BY created_at DESC
                LIMIT 200
            """)
            return [dict(row) for row in cur.fetchall()]

@app.get("/admin/homeowner-reviews")
def admin_list_homeowner_reviews(request: Request):
    require_admin(request)
    return list_homeowner_reviews()

@app.get("/admin/review-invitations")
def admin_list_homeowner_review_invitations(request: Request):
    require_admin(request)
    return list_homeowner_review_invitations()

@app.get("/admin/homeowner-reviews/{review_id}")
def admin_get_homeowner_review(review_id: int, request: Request):
    require_admin(request)

    result = get_homeowner_review_detail(review_id=review_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Homeowner review not found.",
        )

    return result

@app.get("/admin/jobs/{job_id}/activity")
def admin_job_activity(job_id: str, request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    sh.event_type,
                    sh.previous_status,
                    sh.new_status,
                    sh.metadata,
                    sh.created_at::text AS created_at
                FROM status_history AS sh
                JOIN jobs AS j
                  ON j.id = sh.job_id
                WHERE j.job_id = %s
                  AND (
                      sh.job_id = j.id
                      OR sh.opportunity_id = j.opportunity_id
                  )
                ORDER BY sh.created_at ASC, sh.id ASC
                """,
                (job_id,),
            )
            return [dict(row) for row in cur.fetchall()]



@app.get("/admin/contractors/{contractor_id}/activity")
def admin_contractor_activity(
    contractor_id: str,
    request: Request,
):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Jobs belonging to this contractor.
            cur.execute(
                """
                SELECT
                    j.job_id,
                    l.customer_name,
                    j.status,
                    j.created_at::text AS created_at
                FROM jobs AS j
                JOIN contractors AS c
                  ON c.id = j.contractor_id
                JOIN leads AS l
                  ON l.id = j.lead_id
                WHERE c.contractor_id = %s
                ORDER BY j.created_at DESC, j.id DESC
                """,
                (contractor_id,),
            )
            jobs = [dict(row) for row in cur.fetchall()]

            # Opportunities that have not become jobs.
            cur.execute(
                """
                SELECT
                    o.opportunity_id,
                    l.customer_name,
                    o.status,
                    o.sent_at::text AS sent_at
                FROM opportunities AS o
                JOIN contractors AS c
                  ON c.id = o.contractor_id
                JOIN leads AS l
                  ON l.id = o.lead_id
                LEFT JOIN jobs AS j
                  ON j.opportunity_id = o.id
                WHERE c.contractor_id = %s
                  AND j.id IS NULL
                ORDER BY o.sent_at DESC, o.id DESC
                """,
                (contractor_id,),
            )
            unassigned_opportunities = [
                dict(row) for row in cur.fetchall()
            ]

            # Keep contractor profile events separate from job events.
            cur.execute(
                """
                SELECT
                    sh.event_type,
                    sh.previous_status,
                    sh.new_status,
                    sh.metadata,
                    sh.created_at::text AS created_at
                FROM status_history AS sh
                JOIN contractors AS c
                  ON c.id = sh.contractor_id
                WHERE c.contractor_id = %s
                  AND sh.event_type IN (
                      'ContractorCreated',
                      'ContractorMatchingProfileUpdated',
                      'ContractorReferralFeeRateUpdated'
                  )
                ORDER BY sh.created_at ASC, sh.id ASC
                """,
                (contractor_id,),
            )
            profile_events = [
                dict(row) for row in cur.fetchall()
            ]

            return {
                "jobs": jobs,
                "unassigned_opportunities": unassigned_opportunities,
                "profile_events": profile_events,
            }

@app.get("/admin/jobs/{job_id}/payment-agreement")
def admin_get_payment_agreement(job_id: str, request: Request):
    require_admin(request)

    try:
        return get_admin_payment_agreement(
            public_job_id=job_id,
        )
    except AgreementJobNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )
    except InvalidAgreementError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

@app.post(
    "/admin/jobs/{job_id}/payment-agreement/revise",
    status_code=201,
)
def admin_revise_payment_agreement(
    job_id: str,
    body: AdminPaymentAgreementRevisionRequest,
    request: Request,
):
    admin = require_admin(request)
    require_allowed_origin(request)

    try:
        result = revise_payment_agreement(
            public_job_id=job_id,
            admin_id=admin["admin_id"],
            admin_username=admin["username"],
            reason=body.reason,
            arrangement_type=body.arrangement_type,
            invoice_total=body.invoice_total,
            term_months=body.term_months,
            down_payment_amount=body.down_payment_amount,
            down_payment_due_date=body.down_payment_due_date,
            installment_count=body.installment_count,
            schedule_type=body.schedule_type,
            installment_amount=body.installment_amount,
            final_installment_amount=body.final_installment_amount,
            plan_start_date=body.plan_start_date,
            final_due_date=body.final_due_date,
            custom_schedule=body.custom_schedule,
        )
    except AgreementJobNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        )
    except AgreementConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )
    except (InvalidAgreementError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

    return result

@app.get("/homeowner/review-invitations/validate")
def validate_homeowner_review_link(token: str):
    """Validate a homeowner review link without exposing personal information."""

    try:
        return validate_homeowner_review_invitation(token=token)

    except InvalidReviewTokenError:
        raise HTTPException(
            status_code=404,
            detail="This review link is invalid.",
        )

    except ReviewAlreadySubmittedError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    except ReviewExpiredError as exc:
        raise HTTPException(
            status_code=410,
            detail=str(exc),
        )


@app.post("/homeowner/reviews", status_code=201)
def submit_homeowner_review_endpoint(
    body: HomeownerReviewSubmission,
    request: Request,
):
    """Accept a homeowner review using its invitation token."""

    require_allowed_origin(request)

    try:
        return submit_homeowner_review(
            token=body.token,
            rating=body.rating,
            payment_plan_agreed=body.payment_plan_agreed,
            written_feedback=body.written_feedback,
            total_cost_submitted=body.total_cost_submitted,
            total_cost=body.total_cost,
            cost_comments=body.cost_comments,
        )

    except InvalidReviewTokenError:
        raise HTTPException(
            status_code=404,
            detail="This review link is invalid.",
        )

    except ReviewAlreadySubmittedError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    except ReviewExpiredError as exc:
        raise HTTPException(
            status_code=410,
            detail=str(exc),
        )

    except InvalidHomeownerReviewError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

@app.post("/admin/review-invitations/{invitation_id}/resume")
def admin_resume_homeowner_review_invitation(
    invitation_id: int,
    body: AdminReasonRequest,
    request: Request,
):
    admin = require_admin(request)
    require_allowed_origin(request)

    try:
        return resume_homeowner_review_invitation(
            invitation_id=invitation_id,
            admin_id=admin["admin_id"],
            reason=body.reason,
        )
    except ReviewInvitationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )
    except ReviewInvitationConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )


@app.post("/admin/review-invitations/{invitation_id}/stop")
def admin_stop_homeowner_review_invitation(
    invitation_id: int,
    body: AdminReasonRequest,
    request: Request,
):
    admin = require_admin(request)
    require_allowed_origin(request)

    try:
        return stop_homeowner_review_invitation(
            invitation_id=invitation_id,
            admin_id=admin["admin_id"],
            reason=body.reason,
        )
    except ReviewInvitationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )
    except ReviewInvitationConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

@app.post("/admin/review-invitations/{invitation_id}/resolve")
def admin_resolve_homeowner_review_invitation(
    invitation_id: int,
    body: AdminReviewInvitationResolutionRequest,
    request: Request,
):
    admin = require_admin(request)
    require_allowed_origin(request)

    try:
        return resolve_homeowner_review_invitation(
            invitation_id=invitation_id,
            admin_id=admin["admin_id"],
            action=body.action,
            reason=body.reason,
        )
    except ReviewInvitationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )
    except ReviewInvitationConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

@app.get("/admin/jobs/{job_id}/review-link")
def admin_get_homeowner_review_link(
    job_id: str,
    request: Request,
    response: Response,
):
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    i.id,
                    i.token_hash,
                    i.expires_at,
                    i.submitted_at,
                    i.expires_at > CURRENT_TIMESTAMP AS is_valid
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            invitation = cur.fetchone()

    if invitation is None:
        raise HTTPException(
            status_code=404,
            detail="No homeowner review invitation exists for this job.",
        )

    if invitation["submitted_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail="The homeowner has already submitted a review.",
        )

    if not invitation["is_valid"]:
        raise HTTPException(
            status_code=410,
            detail="The homeowner review invitation has expired.",
        )

    stored_hash = invitation["token_hash"]

    if not stored_hash:
        raise HTTPException(
            status_code=409,
            detail="The review token has not been initialized.",
        )

    token = generate_review_token(invitation["id"])

    if not review_token_matches(token, stored_hash):
        raise HTTPException(
            status_code=500,
            detail="The review invitation token could not be verified.",
        )

    return {
        "job_id": job_id,
        "review_path": f"/homeowner/review?token={token}",
        "expires_at": invitation["expires_at"].isoformat(),
    }

@app.get("/admin/jobs")
def admin_list_jobs(request: Request):
    require_admin(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    j.job_id,
                    j.status,
                    j.referral_fee_rate::float AS referral_fee_rate,
                    j.quoted_amount::float AS quoted_amount,
                    j.created_at::text AS created_at,
                    j.updated_at::text AS updated_at,
                    l.lead_id,
                    l.zip_code,
                    l.service_type,
                    l.description,
                    l.customer_name,
                    l.customer_email,
                    l.customer_phone,
                    c.contractor_id,
                    c.company_name,
                    c.contact_name AS contractor_contact_name
                FROM jobs AS j
                JOIN leads AS l
                  ON l.id = j.lead_id
                JOIN contractors AS c
                  ON c.id = j.contractor_id
                ORDER BY j.updated_at DESC
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
                       c.availability, c.default_referral_fee_rate::float AS default_referral_fee_rate, c.status, c.created_at,
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

@app.patch("/admin/contractors/{contractor_id}/referral-fee-rate")
def admin_update_contractor_referral_fee_rate(
    contractor_id: str,
    rate_input: ContractorReferralFeeRateUpdate,
    request: Request,
):
    admin = require_admin(request)
    require_allowed_origin(request)

    new_rate = rate_input.referral_fee_rate

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Serialize rate changes against opportunity acceptance.
            cur.execute(
                """
                SELECT id, default_referral_fee_rate
                FROM contractors
                WHERE contractor_id = %s
                FOR UPDATE
                """,
                (contractor_id,),
            )
            contractor = cur.fetchone()

            if contractor is None:
                raise HTTPException(
                    status_code=404,
                    detail="Contractor not found.",
                )

            contractor_pk = contractor["id"]
            previous_rate = contractor["default_referral_fee_rate"]

            if previous_rate == new_rate:
                return {
                    "contractor_id": contractor_id,
                    "previous_rate": float(previous_rate),
                    "new_rate": float(new_rate),
                    "adjusted_jobs": 0,
                    "message": "The contractor already has this referral fee rate.",
                }

            cur.execute(
                """
                UPDATE contractors
                SET default_referral_fee_rate = %s
                WHERE id = %s
                """,
                (new_rate, contractor_pk),
            )

            adjusted_jobs = []

            # A decrease affects only active jobs without any submitted
            # payment-agreement version. A previous submission protects
            # the job even if that agreement was later superseded.
            if new_rate < previous_rate:
                cur.execute(
                    """
                    SELECT
                        j.id,
                        j.job_id,
                        j.referral_fee_rate
                    FROM jobs AS j
                    WHERE j.contractor_id = %s
                      AND j.status IN (
                          'QUOTE_IN_PROGRESS',
                          'BID_WON',
                          'JOB_IN_PROGRESS'
                      )
                      AND j.referral_fee_rate > %s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM job_payment_agreements AS jpa
                          WHERE jpa.job_id = j.id
                            AND jpa.emg_review_requested_at IS NOT NULL
                      )
                    ORDER BY j.id
                    FOR UPDATE OF j
                    """,
                    (contractor_pk, new_rate),
                )
                eligible_jobs = cur.fetchall()

                for job in eligible_jobs:
                    old_job_rate = job["referral_fee_rate"]
                    effective_rate = min(old_job_rate, new_rate)

                    cur.execute(
                        """
                        UPDATE jobs
                        SET referral_fee_rate = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (effective_rate, job["id"]),
                    )

                    cur.execute(
                        """
                        INSERT INTO job_financial_audit_events (
                            job_id,
                            contractor_id,
                            actor_type,
                            event_type,
                            related_record_type,
                            related_record_id,
                            details
                        )
                        VALUES (
                            %s,
                            %s,
                            'EMG',
                            'REFERRAL_FEE_RATE_ADJUSTED',
                            'contractors',
                            %s,
                            jsonb_build_object(
                                'previous_rate', %s::numeric,
                                'new_rate', %s::numeric,
                                'reason', 'CONTRACTOR_DEFAULT_RATE_DECREASE'
                            )
                        )
                        """,
                        (
                            job["id"],
                            contractor_pk,
                            contractor_pk,
                            old_job_rate,
                            effective_rate,
                        ),
                    )

                    adjusted_jobs.append(job["job_id"])

            cur.execute(
                """
                INSERT INTO status_history (
                    contractor_id,
                    event_type,
                    previous_status,
                    new_status,
                    metadata
                )
                VALUES (
                    %s,
                    'ContractorReferralFeeRateUpdated',
                    %s,
                    %s,
                    jsonb_build_object(
                        'previous_rate', %s::numeric,
                        'new_rate', %s::numeric,
                        'adjusted_jobs', %s,
                        'admin_username', %s::text
                    )
                )
                """,
                (
                    contractor_pk,
                    format(previous_rate, ".4f"),
                    format(new_rate, ".4f"),
                    previous_rate,
                    new_rate,
                    len(adjusted_jobs),
                    admin["username"],
                ),
            )

    return {
        "contractor_id": contractor_id,
        "previous_rate": float(previous_rate),
        "new_rate": float(new_rate),
        "adjusted_jobs": len(adjusted_jobs),
        "adjusted_job_ids": adjusted_jobs,
    }

@app.patch("/admin/jobs/{job_id}/referral-fee-rate")
def admin_update_job_referral_fee_rate(
    job_id: str,
    rate_input: ContractorReferralFeeRateUpdate,
    request: Request,
):
    admin = require_admin(request)
    require_allowed_origin(request)

    requested_rate = rate_input.referral_fee_rate

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Read the contractor association first so we can preserve
            # the same contractor-then-job lock order used by rate changes.
            cur.execute(
                """
                SELECT id, contractor_id
                FROM jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            initial_job = cur.fetchone()

            if initial_job is None:
                raise HTTPException(
                    status_code=404,
                    detail="Job not found.",
                )

            contractor_pk = initial_job["contractor_id"]

            if contractor_pk is None:
                raise HTTPException(
                    status_code=409,
                    detail="A contractor must be assigned before changing the job referral fee rate.",
                )

            # Lock the contractor first to serialize this operation against
            # contractor default-rate changes and opportunity acceptance.
            cur.execute(
                """
                SELECT id, default_referral_fee_rate
                FROM contractors
                WHERE id = %s
                FOR UPDATE
                """,
                (contractor_pk,),
            )
            contractor = cur.fetchone()

            if contractor is None:
                raise HTTPException(
                    status_code=409,
                    detail="The job's assigned contractor could not be found.",
                )

            # Lock and re-read the job after acquiring the contractor lock.
            cur.execute(
                """
                SELECT id, contractor_id, referral_fee_rate
                FROM jobs
                WHERE id = %s
                FOR UPDATE
                """,
                (initial_job["id"],),
            )
            job = cur.fetchone()

            if job is None:
                raise HTTPException(
                    status_code=404,
                    detail="Job not found.",
                )

            if job["contractor_id"] != contractor_pk:
                raise HTTPException(
                    status_code=409,
                    detail="The job's assigned contractor changed. Please refresh and try again.",
                )

            previous_rate = job["referral_fee_rate"]
            contractor_default_rate = contractor["default_referral_fee_rate"]

            # The effective job rate cannot exceed the contractor's default.
            effective_rate = min(
                requested_rate,
                contractor_default_rate,
            )

            if previous_rate == effective_rate:
                return {
                    "job_id": job_id,
                    "previous_rate": float(previous_rate),
                    "requested_rate": float(requested_rate),
                    "new_rate": float(effective_rate),
                    "message": "The job already has this effective referral fee rate.",
                }

            cur.execute(
                """
                UPDATE jobs
                SET referral_fee_rate = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (effective_rate, job["id"]),
            )

            cur.execute(
                """
                INSERT INTO job_financial_audit_events (
                    job_id,
                    contractor_id,
                    actor_type,
                    event_type,
                    related_record_type,
                    related_record_id,
                    details
                )
                VALUES (
                    %s,
                    %s,
                    'EMG',
                    'REFERRAL_FEE_RATE_ADJUSTED',
                    'jobs',
                    %s,
                    jsonb_build_object(
                        'previous_rate', %s::numeric,
                        'requested_rate', %s::numeric,
                        'new_rate', %s::numeric,
                        'contractor_default_rate', %s::numeric,
                        'reason', 'MANUAL_JOB_RATE_UPDATE',
                        'admin_username', %s::text
                    )
                )
                """,
                (
                    job["id"],
                    contractor_pk,
                    job["id"],
                    previous_rate,
                    requested_rate,
                    effective_rate,
                    contractor_default_rate,
                    admin["username"],
                ),
            )

    return {
        "job_id": job_id,
        "previous_rate": float(previous_rate),
        "requested_rate": float(requested_rate),
        "new_rate": float(effective_rate),
        "contractor_default_rate": float(contractor_default_rate),
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


@app.post("/admin/contractor-accounts/{contractor_id}/reset-password")
def admin_reset_contractor_password(
    contractor_id: str,
    password_input: ContractorPasswordResetRequest,
    request: Request,
):
    require_admin(request)

    try:
        reset_contractor_password(
            contractor_id=contractor_id,
            temporary_password=password_input.temporary_password,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from error

    return {
        "message": (
            "Temporary password set. Existing sessions were revoked, "
            "and the contractor must change the password at next login."
        )
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
    customer_phone = customer_phone.strip()
    zip_code = zip_code.strip()
    service_type = service_type.strip()
    description = description.strip()
    customer_email = (customer_email or "").strip() or None
    additional_info = (additional_info or "").strip() or None

    if len(customer_name) > 150:
        raise HTTPException(status_code=422, detail="Customer name must be 150 characters or fewer.")

    if len(customer_phone) > 30:
        raise HTTPException(status_code=422, detail="Phone number must be 30 characters or fewer.")

    if customer_email and len(customer_email) > 255:
        raise HTTPException(status_code=422, detail="Email must be 255 characters or fewer.")

    if len(service_type) > 100:
        raise HTTPException(status_code=422, detail="Service type must be 100 characters or fewer.")

    if len(description) > 5000:
        raise HTTPException(status_code=422, detail="Job description must be 5000 characters or fewer.")

    if additional_info and len(additional_info) > 5000:
        raise HTTPException(status_code=422, detail="Additional information must be 5000 characters or fewer.")

    customer_phone = normalize_us_phone(customer_phone)

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

    saved_storage_keys = []

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                for stored_name, content, content_type, original_filename in uploaded_photos:
                    storage_key = save_photo(
                        stored_name,
                        content,
                        content_type,
                    )
                    saved_storage_keys.append(storage_key)

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
                            storage_key,
                            original_filename,
                            content_type,
                        ),
                    )
    except Exception:
        for storage_key in saved_storage_keys:
            try:
                delete_photo(storage_key)
            except Exception as cleanup_error:
                print(
                    f"Photo cleanup failed for {storage_key}: "
                    f"{type(cleanup_error).__name__}"
                )

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
    # internal numeric database ID.
    try:
        opportunities = route_opportunities_to_fill_slots(
            lead["lead_id"]
        )
        routing_status = "completed"

        # Clear any previous routing failure after a successful run.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE leads
                    SET routing_error = NULL,
                        routing_failed_at = NULL
                    WHERE lead_id = %s
                    """,
                    (lead["lead_id"],),
                )

    except Exception as error:
        opportunities = []
        routing_status = "requires_review"

        error_message = f"{type(error).__name__}: {error}"

        # Persist the failure so administrators can recover this lead.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE leads
                    SET routing_error = %s,
                        routing_failed_at = CURRENT_TIMESTAMP
                    WHERE lead_id = %s
                    """,
                    (error_message, lead["lead_id"]),
                )

        print(
            f"Automatic matching failed for lead "
            f"{lead['lead_id']}: {error_message}",
            flush=True,
        )
        traceback.print_exc()

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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE leads
                SET routing_error = NULL,
                    routing_failed_at = NULL
                WHERE lead_id = %s
                """,
                (lead_id,),
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
                    l.budget_range,
                    l.urgency,
                    l.customer_name,
                    l.customer_phone,
                    l.customer_email,
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
                    l.budget_range,
                    l.urgency,
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
