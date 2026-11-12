"""Submission and agreement comparison for homeowner reviews."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from psycopg.types.json import Jsonb

from app.database.connection import get_connection
from app.services.review_tokens import hash_review_token, review_token_matches


CENT = Decimal("0.01")
MAX_TOTAL_COST = Decimal("9999999999.99")


class InvalidReviewTokenError(Exception):
    """The review link is invalid."""


class ReviewExpiredError(Exception):
    """The review invitation has expired."""


class ReviewAlreadySubmittedError(Exception):
    """A review has already been submitted for this invitation."""


class InvalidHomeownerReviewError(ValueError):
    """The submitted review contains invalid data."""


def _money(value):
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise InvalidHomeownerReviewError("Enter a valid total cost.") from None

    if (
        not amount.is_finite()
        or amount < 0
        or amount > MAX_TOTAL_COST
    ):
        raise InvalidHomeownerReviewError(
            "Total cost must be between $0.00 and $9,999,999,999.99."
        )

    try:
        rounded = amount.quantize(CENT)
    except InvalidOperation:
        raise InvalidHomeownerReviewError(
            "Enter a valid total cost with no more than two decimal places."
        ) from None

    if amount != rounded:
        raise InvalidHomeownerReviewError(
            "Total cost must have no more than two decimal places."
        )

    return rounded


def _optional_text(value, field_name, max_length=5000):
    if value is None:
        return None

    if not isinstance(value, str):
        raise InvalidHomeownerReviewError(f"{field_name} must be text.")

    value = value.strip()

    if len(value) > max_length:
        raise InvalidHomeownerReviewError(
            f"{field_name} must be {max_length} characters or fewer."
        )

    return value or None


def submit_homeowner_review(
    *,
    token,
    rating,
    payment_plan_agreed,
    written_feedback=None,
    total_cost_submitted=False,
    total_cost=None,
    cost_comments=None,
):
    """Save a homeowner review and flag differences from the operative agreement.

    payment_plan_agreed:
        False means the homeowner reports paying in full.
        True means the homeowner reports using a payment plan.

    The review and any resulting discrepancies are committed atomically.
    """
    if not isinstance(token, str) or not token.strip():
        raise InvalidReviewTokenError("This review link is invalid.")

    token = token.strip()

    if len(token) > 256:
        raise InvalidReviewTokenError("This review link is invalid.")

    if isinstance(rating, bool) or not isinstance(rating, int) or rating not in range(1, 6):
        raise InvalidHomeownerReviewError("Rating must be between 1 and 5.")

    if not isinstance(payment_plan_agreed, bool):
        raise InvalidHomeownerReviewError(
            "Select whether the job was paid in full or put on a payment plan."
        )

    if not isinstance(total_cost_submitted, bool):
        raise InvalidHomeownerReviewError("Invalid total-cost response.")

    written_feedback = _optional_text(written_feedback, "Written feedback")
    cost_comments = _optional_text(cost_comments, "Cost comments")

    if total_cost_submitted:
        if total_cost is None:
            raise InvalidHomeownerReviewError(
                "Enter the final total cost or indicate that you are not providing it."
            )
        reported_total = _money(total_cost)
    else:
        reported_total = Decimal("0.00")

    token_hash = hash_review_token(token)

    with get_connection() as conn:
        with conn.transaction():
            invitation = conn.execute(
                """
                SELECT
                    i.id AS invitation_id,
                    i.job_id,
                    i.token_hash,
                    i.expires_at,
                    i.submitted_at,
                    (i.expires_at <= clock_timestamp()) AS is_expired
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                WHERE i.token_hash = %s
                FOR UPDATE OF i
                """,
                (token_hash,),
            ).fetchone()

            if invitation is None or not review_token_matches(
                token, invitation["token_hash"]
            ):
                raise InvalidReviewTokenError("This review link is invalid.")

            if invitation["submitted_at"] is not None:
                raise ReviewAlreadySubmittedError(
                    "A review has already been submitted for this job."
                )

            if invitation["is_expired"]:
                raise ReviewExpiredError("This review link has expired.")

            review = conn.execute(
                """
                INSERT INTO homeowner_reviews (
                    invitation_id,
                    job_id,
                    rating,
                    written_feedback,
                    total_cost,
                    total_cost_submitted,
                    payment_plan_agreed,
                    cost_comments
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    invitation["invitation_id"],
                    invitation["job_id"],
                    rating,
                    written_feedback,
                    reported_total,
                    total_cost_submitted,
                    payment_plan_agreed,
                    cost_comments,
                ),
            ).fetchone()

            review_id = review["id"]

            agreement = conn.execute(
                """
                SELECT id, arrangement_type, invoice_total
                FROM job_payment_agreements
                WHERE job_id = %s
                  AND status IN ('ACCEPTED', 'ADMIN_REVISED')
                FOR SHARE
                """,
                (invitation["job_id"],),
            ).fetchone()

            discrepancies = []

            if agreement is not None:
                agreement_id = agreement["id"]
                expected_payment_plan = (
                    agreement["arrangement_type"] == "PAYMENT_PLAN"
                )

                if payment_plan_agreed != expected_payment_plan:
                    expected_label = (
                        "Put on a payment plan"
                        if expected_payment_plan
                        else "Paid In Full"
                    )
                    reported_label = (
                        "Put on a payment plan"
                        if payment_plan_agreed
                        else "Paid In Full"
                    )

                    details = {
                        "expected_arrangement": expected_label,
                        "homeowner_reported_arrangement": reported_label,
                        "agreement_arrangement_type": agreement["arrangement_type"],
                    }

                    conn.execute(
                        """
                        INSERT INTO homeowner_review_discrepancies (
                            review_id,
                            job_id,
                            agreement_id,
                            discrepancy_type,
                            details
                        )
                        VALUES (%s, %s, %s, 'PAYMENT_PLAN', %s)
                        """,
                        (
                            review_id,
                            invitation["job_id"],
                            agreement_id,
                            Jsonb(details),
                        ),
                    )
                    discrepancies.append("PAYMENT_PLAN")

                if (
                    total_cost_submitted
                    and reported_total != agreement["invoice_total"]
                ):
                    difference = reported_total - agreement["invoice_total"]

                    details = {
                        "agreement_invoice_total": str(agreement["invoice_total"]),
                        "homeowner_reported_total": str(reported_total),
                        "difference": str(difference),
                    }

                    conn.execute(
                        """
                        INSERT INTO homeowner_review_discrepancies (
                            review_id,
                            job_id,
                            agreement_id,
                            discrepancy_type,
                            details
                        )
                        VALUES (%s, %s, %s, 'TOTAL_COST', %s)
                        """,
                        (
                            review_id,
                            invitation["job_id"],
                            agreement_id,
                            Jsonb(details),
                        ),
                    )
                    discrepancies.append("TOTAL_COST")

            conn.execute(
                """
                UPDATE homeowner_review_invitations
                SET submitted_at = CURRENT_TIMESTAMP,
                    retry_state = 'COMPLETE',
                    next_attempt_at = NULL,
                    claimed_until = NULL
                WHERE id = %s
                """,
                (invitation["invitation_id"],),
            )

            return {
                "review_id": review_id,
                "job_id": invitation["job_id"],
                "discrepancies": discrepancies,
            }

def validate_homeowner_review_invitation(*, token):
    """Check whether a review link can currently be used, without exposing PII."""
    if not isinstance(token, str) or not token.strip():
        raise InvalidReviewTokenError("This review link is invalid.")

    token = token.strip()

    if len(token) > 256:
        raise InvalidReviewTokenError("This review link is invalid.")

    token_hash = hash_review_token(token)

    with get_connection() as conn:
        invitation = conn.execute(
            """
            SELECT
                token_hash,
                submitted_at,
                (expires_at <= clock_timestamp()) AS is_expired
            FROM homeowner_review_invitations
            WHERE token_hash = %s
            """,
            (token_hash,),
        ).fetchone()

    if invitation is None or not review_token_matches(
        token, invitation["token_hash"]
    ):
        raise InvalidReviewTokenError("This review link is invalid.")

    if invitation["submitted_at"] is not None:
        raise ReviewAlreadySubmittedError(
            "A review has already been submitted for this job."
        )

    if invitation["is_expired"]:
        raise ReviewExpiredError("This review link has expired.")

    return {"available": True}


def list_homeowner_reviews():
    """Return recent homeowner reviews with job and discrepancy summaries."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.id AS review_id,
                r.job_id AS internal_job_id,
                j.job_id AS job_id,
                j.status AS job_status,
                l.lead_id,
                l.customer_name,
                l.customer_email,
                c.contractor_id,
                c.company_name,
                r.rating,
                r.written_feedback,
                r.total_cost::text AS total_cost,
                r.total_cost_submitted,
                r.payment_plan_agreed,
                r.cost_comments,
                r.created_at::text AS created_at,
                COALESCE(ds.discrepancy_count, 0) AS discrepancy_count,
                COALESCE(ds.open_discrepancy_count, 0)
                    AS open_discrepancy_count,
                COALESCE(
                    ds.discrepancy_statuses,
                    ARRAY[]::varchar[]
                ) AS discrepancy_statuses
            FROM homeowner_reviews AS r
            JOIN jobs AS j
                ON j.id = r.job_id
            JOIN leads AS l
                ON l.id = j.lead_id
            JOIN contractors AS c
                ON c.id = j.contractor_id
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) AS discrepancy_count,
                    COUNT(*) FILTER (
                        WHERE d.status IN ('OPEN', 'UNDER_REVIEW')
                    ) AS open_discrepancy_count,
                    ARRAY_AGG(DISTINCT d.status) AS discrepancy_statuses
                FROM homeowner_review_discrepancies AS d
                WHERE d.review_id = r.id
            ) AS ds ON TRUE
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT 200
            """
        ).fetchall()

    return [dict(row) for row in rows]


def get_homeowner_review_detail(*, review_id):
    """Return one review, its discrepancies, and relevant agreement context."""
    with get_connection() as conn:
        review = conn.execute(
            """
            SELECT
                r.id AS review_id,
                r.invitation_id,
                r.job_id AS internal_job_id,
                j.job_id AS job_id,
                j.status AS job_status,
                j.created_at::text AS job_created_at,
                l.lead_id,
                l.customer_name,
                l.customer_email,
                l.customer_phone,
                l.zip_code,
                l.service_type,
                c.contractor_id,
                c.company_name,
                c.contact_name AS contractor_contact_name,
                r.rating,
                r.written_feedback,
                r.total_cost::text AS total_cost,
                r.total_cost_submitted,
                r.payment_plan_agreed,
                r.cost_comments,
                r.created_at::text AS created_at
            FROM homeowner_reviews AS r
            JOIN jobs AS j
                ON j.id = r.job_id
            JOIN leads AS l
                ON l.id = j.lead_id
            JOIN contractors AS c
                ON c.id = j.contractor_id
            WHERE r.id = %s
            """,
            (review_id,),
        ).fetchone()

        if review is None:
            return None

        discrepancies = conn.execute(
            """
            SELECT
                d.id AS discrepancy_id,
                d.discrepancy_type,
                d.status,
                d.details,
                d.created_at::text AS created_at,
                d.resolved_at::text AS resolved_at,
                d.resolved_by_admin_user_id,
                u.username AS resolved_by_username,
                d.resolution_notes,
                a.id AS agreement_id,
                a.version AS agreement_version,
                a.status AS agreement_status,
                a.arrangement_type,
                a.invoice_total::text AS invoice_total,
                a.schedule_type,
                a.installment_amount::text AS installment_amount,
                a.final_installment_amount::text
                    AS final_installment_amount,
                a.plan_start_date::text AS plan_start_date,
                a.final_due_date::text AS final_due_date,
                a.custom_schedule,
                a.down_payment_amount::text AS down_payment_amount,
                a.down_payment_due_date::text AS down_payment_due_date,
                a.installment_count,
                a.admin_revision_reason,
                a.accepted_at::text AS accepted_at,
                a.admin_revised_at::text AS admin_revised_at
            FROM homeowner_review_discrepancies AS d
            JOIN job_payment_agreements AS a
                ON a.id = d.agreement_id
               AND a.job_id = d.job_id
            LEFT JOIN admin_users AS u
                ON u.id = d.resolved_by_admin_user_id
            WHERE d.review_id = %s
              AND d.job_id = %s
            ORDER BY d.created_at, d.id
            """,
            (review_id, review["internal_job_id"]),
        ).fetchall()

        agreements = []

        if discrepancies:
            seen_agreement_ids = set()

            for discrepancy in discrepancies:
                agreement_id = discrepancy["agreement_id"]

                if agreement_id not in seen_agreement_ids:
                    seen_agreement_ids.add(agreement_id)
                    agreements.append({
                        "agreement_id": agreement_id,
                        "version": discrepancy["agreement_version"],
                        "status": discrepancy["agreement_status"],
                        "arrangement_type": discrepancy["arrangement_type"],
                        "invoice_total": discrepancy["invoice_total"],
                        "schedule_type": discrepancy["schedule_type"],
                        "installment_amount": discrepancy["installment_amount"],
                        "final_installment_amount":
                            discrepancy["final_installment_amount"],
                        "plan_start_date": discrepancy["plan_start_date"],
                        "final_due_date": discrepancy["final_due_date"],
                        "custom_schedule": discrepancy["custom_schedule"],
                        "down_payment_amount":
                            discrepancy["down_payment_amount"],
                        "down_payment_due_date":
                            discrepancy["down_payment_due_date"],
                        "installment_count":
                            discrepancy["installment_count"],
                        "admin_revision_reason":
                            discrepancy["admin_revision_reason"],
                        "accepted_at": discrepancy["accepted_at"],
                        "admin_revised_at": discrepancy["admin_revised_at"],
                        "reference_type": "DISCREPANCY_RECORDED",
                    })

        else:
            current_agreement = conn.execute(
                """
                SELECT
                    id AS agreement_id,
                    version,
                    status,
                    arrangement_type,
                    invoice_total::text AS invoice_total,
                    schedule_type,
                    installment_amount::text AS installment_amount,
                    final_installment_amount::text
                        AS final_installment_amount,
                    plan_start_date::text AS plan_start_date,
                    final_due_date::text AS final_due_date,
                    custom_schedule,
                    down_payment_amount::text AS down_payment_amount,
                    down_payment_due_date::text AS down_payment_due_date,
                    installment_count,
                    admin_revision_reason,
                    accepted_at::text AS accepted_at,
                    admin_revised_at::text AS admin_revised_at
                FROM job_payment_agreements
                WHERE job_id = %s
                  AND status IN ('ACCEPTED', 'ADMIN_REVISED')
                """,
                (review["internal_job_id"],),
            ).fetchone()

            if current_agreement is not None:
                agreement = dict(current_agreement)
                agreement["reference_type"] = (
                    "CURRENT_OPERATIVE_NOT_HISTORICAL_SNAPSHOT"
                )
                agreements.append(agreement)

    return {
        "review": dict(review),
        "discrepancies": [dict(row) for row in discrepancies],
        "agreements": agreements,
    }
