
"""Generate contractor invoice PDFs in memory."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


BRAND_NAME = "EliteMatch Group"

BRAND_DARK = colors.HexColor("#202A35")
BRAND_ACCENT = colors.HexColor("#C5A45A")
TEXT_DARK = colors.HexColor("#303943")
TEXT_MUTED = colors.HexColor("#687480")
TABLE_BACKGROUND = colors.HexColor("#F2F4F6")
BORDER_COLOR = colors.HexColor("#DCE1E6")


def _money(value: Any) -> Decimal:
    """Convert a database amount to Decimal without float arithmetic."""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid invoice monetary amount.") from exc

    if not amount.is_finite():
        raise ValueError("Invoice amounts must be finite.")

    return amount.quantize(Decimal("0.01"))


def _format_money(value: Any) -> str:
    """Format an amount as US currency."""

    return f"${_money(value):,.2f}"


def _format_date(value: date | datetime) -> str:
    """Format a date consistently for the invoice."""

    if isinstance(value, datetime):
        value = value.date()

    if not isinstance(value, date):
        raise ValueError("Invoice dates must be date or datetime values.")

    return value.strftime("%B %d, %Y")


def _paragraph(
    value: Any,
    style: ParagraphStyle,
) -> Paragraph:
    """Create a paragraph with escaped database-sourced text."""

    return Paragraph(escape(str(value)), style)


def render_contractor_invoice_pdf(
    invoice: dict[str, Any],
    contractor: dict[str, Any],
    obligations: list[dict[str, Any]],
    issue_date: date,
    due_date: date,
) -> bytes:
    """
    Render one contractor invoice and return its PDF bytes.

    Expected invoice fields:
        invoice_number
        billing_period_start
        billing_period_end
        total_amount

    Expected contractor fields:
        contractor_id
        company_name
        contact_name

    Expected obligation fields:
        job_id
        service_type
        zip_code
        original_amount
    """

    if not obligations:
        raise ValueError("An invoice must contain at least one job.")

    if due_date < issue_date:
        raise ValueError("The invoice due date cannot precede its issue date.")

    invoice_number = str(invoice["invoice_number"]).strip()

    if not invoice_number:
        raise ValueError("An invoice number is required.")

    company_name = str(
        contractor.get("company_name") or ""
    ).strip()

    if not company_name:
        raise ValueError("A contractor company name is required.")

    total_amount = _money(invoice["total_amount"])

    line_total = sum(
        (_money(item["original_amount"]) for item in obligations),
        Decimal("0.00"),
    )

    if line_total != total_amount:
        raise ValueError(
            "Invoice total does not match the sum of its job obligations."
        )

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.65 * inch,
        title=f"Contractor Invoice {invoice_number}",
        author=BRAND_NAME,
        subject="Contractor referral fee invoice",
    )

    base_styles = getSampleStyleSheet()

    styles = {
        "brand": ParagraphStyle(
            "Brand",
            parent=base_styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=25,
            textColor=BRAND_DARK,
            alignment=TA_LEFT,
            spaceAfter=3,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle",
            parent=base_styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=TEXT_MUTED,
        ),
        "invoice_title": ParagraphStyle(
            "InvoiceTitle",
            parent=base_styles["Heading1"],
            fontSize=19,
            leading=23,
            textColor=BRAND_DARK,
            alignment=TA_RIGHT,
        ),
        "section": ParagraphStyle(
            "Section",
            parent=base_styles["Heading2"],
            fontSize=10,
            leading=13,
            textColor=BRAND_DARK,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base_styles["Normal"],
            fontSize=9,
            leading=13,
            textColor=TEXT_DARK,
        ),
        "muted": ParagraphStyle(
            "Muted",
            parent=base_styles["Normal"],
            fontSize=8,
            leading=11,
            textColor=TEXT_MUTED,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=base_styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "TableCell",
            parent=base_styles["Normal"],
            fontSize=8,
            leading=11,
            textColor=TEXT_DARK,
        ),
        "table_cell_right": ParagraphStyle(
            "TableCellRight",
            parent=base_styles["Normal"],
            fontSize=8,
            leading=11,
            textColor=TEXT_DARK,
            alignment=TA_RIGHT,
        ),
        "total_label": ParagraphStyle(
            "TotalLabel",
            parent=base_styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=BRAND_DARK,
            alignment=TA_RIGHT,
        ),
        "total_amount": ParagraphStyle(
            "TotalAmount",
            parent=base_styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=BRAND_DARK,
            alignment=TA_RIGHT,
        ),
    }

    story = []

    # Header
    header = Table(
        [
            [
                [
                    Paragraph(BRAND_NAME, styles["brand"]),
                    Paragraph(
                        "Contractor referral fee statement",
                        styles["subtitle"],
                    ),
                ],
                Paragraph("INVOICE", styles["invoice_title"]),
            ]
        ],
        colWidths=[4.2 * inch, 2.5 * inch],
    )

    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                (
                    "LINEBELOW",
                    (0, 0),
                    (-1, -1),
                    2,
                    BRAND_ACCENT,
                ),
            ]
        )
    )

    story.append(header)
    story.append(Spacer(1, 0.22 * inch))

    # Invoice and contractor information
    contractor_id = contractor.get("contractor_id") or ""

    contact_name = contractor.get("contact_name") or ""

    invoice_details = [
        [
            _paragraph("BILL TO", styles["section"]),
            _paragraph("INVOICE DETAILS", styles["section"]),
        ],
        [
            [
                _paragraph(company_name, styles["body"]),
                _paragraph(contact_name, styles["muted"])
                if contact_name
                else Spacer(1, 1),
                _paragraph(
                    f"Contractor ID: {contractor_id}",
                    styles["muted"],
                ),
            ],
            [
                _paragraph(
                    f"Invoice number: {invoice_number}",
                    styles["body"],
                ),
                _paragraph(
                    f"Issue date: {_format_date(issue_date)}",
                    styles["body"],
                ),
                _paragraph(
                    f"Due date: {_format_date(due_date)}",
                    styles["body"],
                ),
                _paragraph(
                    (
                        "Billing period: "
                        f"{_format_date(invoice['billing_period_start'])}"
                        " – "
                        f"{_format_date(invoice['billing_period_end'])}"
                    ),
                    styles["muted"],
                ),
            ],
        ],
    ]

    details_table = Table(
        invoice_details,
        colWidths=[3.35 * inch, 3.35 * inch],
    )

    details_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    story.append(details_table)
    story.append(Spacer(1, 0.3 * inch))

    # Job-level invoice lines
    story.append(
        Paragraph("INVOICE ITEMS", styles["section"])
    )

    line_items = [
        [
            _paragraph("JOB ID", styles["table_header"]),
            _paragraph("SERVICE", styles["table_header"]),
            _paragraph("ZIP CODE", styles["table_header"]),
            _paragraph("AMOUNT", styles["table_header"]),
        ]
    ]

    for item in obligations:
        line_items.append(
            [
                _paragraph(item["job_id"], styles["table_cell"]),
                _paragraph(item["service_type"], styles["table_cell"]),
                _paragraph(item["zip_code"], styles["table_cell"]),
                _paragraph(
                    _format_money(item["original_amount"]),
                    styles["table_cell_right"],
                ),
            ]
        )

    items_table = Table(
        line_items,
        colWidths=[
            1.25 * inch,
            2.75 * inch,
            1.0 * inch,
            1.7 * inch,
        ],
        repeatRows=1,
    )

    items_table.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    BRAND_DARK,
                ),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.4, BORDER_COLOR),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                    colors.white,
                    TABLE_BACKGROUND,
                ]),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )

    story.append(items_table)
    story.append(Spacer(1, 0.2 * inch))

    # Invoice total
    total_table = Table(
        [
            [
                _paragraph("TOTAL DUE", styles["total_label"]),
                _paragraph(
                    _format_money(total_amount),
                    styles["total_amount"],
                ),
            ]
        ],
        colWidths=[5.0 * inch, 1.7 * inch],
    )

    total_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEABOVE", (0, 0), (-1, -1), 1, BRAND_ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    story.append(total_table)
    story.append(Spacer(1, 0.25 * inch))

    story.append(
        Paragraph(
            (
                "Payment is due within seven calendar days "
                "of the invoice date."
            ),
            styles["body"],
        )
    )

    story.append(Spacer(1, 0.12 * inch))

    story.append(
        Paragraph(
            (
                "If you have questions about this invoice, "
                "please contact EliteMatch Group directly."
            ),
            styles["muted"],
        )
    )

    story.append(Spacer(1, 0.35 * inch))

    story.append(
        Paragraph(
            BRAND_NAME,
            ParagraphStyle(
                "Footer",
                parent=styles["muted"],
                alignment=TA_CENTER,
            ),
        )
    )

    document.build(story)

    return buffer.getvalue()
