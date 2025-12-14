import io
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, PageBreak, KeepTogether
)
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from PyPDF2 import PdfReader
from django.core.files.base import ContentFile

HEADER_LINE = "Post Date Value Date Description Cheque No/Reference Debit Credit Balance"


# ---------------------------------------------------------------------
#  BASIC HELPERS
# ---------------------------------------------------------------------
def _parse_amount(text: str) -> float:
    """
    Convert strings like:
      '10,245.00CR', '1,000.00', '0.00', ''  -> float
    Removes commas and CR/DR suffix.
    """
    if not text:
        return 0.0
    text = text.replace("CR", "").replace("DR", "").strip()
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _is_number_token(tok: str) -> bool:
    """
    Match tokens like:
      1,800.00
      352.00CR
      19.00DR
    """
    return bool(re.match(r"^\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?$", tok))


# ---------------------------------------------------------------------
#  DESCRIPTION CLEANER (2 LINES, NO AMOUNTS)
# ---------------------------------------------------------------------
def _clean_description_from_block(text: str):
    """
    Given the full merged text for one transaction (without dates),
    return description as EXACTLY 1–2 lines:

      Line 1: WDL TFR / DEP TFR / ATM WDL
      Line 2: UPI/... or IMPS/... or ATM CASH ... or POS/...

    No numeric amount is included in the description.
    """

    # Normalize spaces
    text = re.sub(r"\s+", " ", text).strip()

    # ---- Find LINE 1 keyword (allow missing spaces like 'WDLTFR' etc.) ----
    first_patterns = [
        (r"WDL\s*TFR", "WDL TFR"),
        (r"DEP\s*TFR", "DEP TFR"),
        (r"ATM\s*WDL", "ATM WDL"),
    ]
    line1 = ""
    line1_pos = -1
    line1_end = -1

    for pat, canon in first_patterns:
        m = re.search(pat, text)
        if m:
            pos = m.start()
            end = m.end()
            if line1_pos == -1 or pos < line1_pos:
                line1 = canon
                line1_pos = pos
                line1_end = end

    # Fallback: if we didn't find the full canonical pattern, try to find lone
    # keywords like 'WDL' or 'DEP' and then include a nearby 'TFR' if present.
    if not line1:
        lone_patterns = [
            (r"\bWDL\b", "WDL TFR"),
            (r"\bDEP\b", "DEP TFR"),
            (r"ATM\s*WDL", "ATM WDL"),
        ]
        for pat, canon in lone_patterns:
            m = re.search(pat, text)
            if m:
                pos = m.start()
                end = m.end()
                # try to find TFR immediately after (allow missing space)
                tfr_m = re.search(r"TFR", text[end:end+8])
                if tfr_m:
                    # extend end to include TFR
                    end = end + tfr_m.end()
                line1 = canon
                line1_pos = pos
                line1_end = end
                break

    # ---- Find LINE 2 prefix (UPI/..., IMPS/..., ATM CASH ..., POS/...) ----
    # Find the earliest occurrence of any second-line keyword anywhere in the
    # text so we can force everything from that keyword onto the second line.
    second_patterns = [
        (r"UPI/", "UPI/"),
        (r"IMPS/", "IMPS/"),
        (r"ATM\s*CASH", "ATM CASH"),
        (r"POS/", "POS/"),
    ]
    line2 = ""
    line2_pos = -1

    for pat, canon in second_patterns:
        m = re.search(pat, text)
        if m:
            pos = m.start()
            if line2_pos == -1 or pos < line2_pos:
                line2_pos = pos

    if line2_pos != -1:
        # Take substring from line2_pos up to the first amount occurrence
        amount_pattern = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?")
        sub = text[line2_pos:]
        m_amt = amount_pattern.search(sub)
        if m_amt:
            sub = sub[:m_amt.start()]
        sub = sub.strip()

        # Fix broken names like "GUD ISE" -> "GUDISE", "CHIN NAB" -> "CHINNAB"
        sub = sub.replace("GUD ISE", "GUDISE")
        sub = sub.replace("CHIN NAB", "CHINNAB")

        # Normalize spaces
        line2 = re.sub(r"\s+", " ", sub).strip()

    # Final description assembly (keep line1 and line2 separate)
    # Normalize canonical first-line tokens with non-breaking space
    line1_nb = line1.replace("WDL TFR", "WDL\u00A0TFR").replace("DEP TFR", "DEP\u00A0TFR").replace("ATM WDL", "ATM\u00A0WDL") if line1 else ""
    line2_clean = re.sub(r"\s+", " ", line2).strip() if line2 else ""

    # Return tuple: (line1_normalized, line2_normalized, line2_pos)
    return line1_nb, line2_clean, (line2_pos if line2_pos != -1 else -1)


# ---------------------------------------------------------------------
#  PARSER: PDF -> JSON (meta + transactions)
# ---------------------------------------------------------------------
def parse_pdf_to_data(file_obj):
    """
    SBI STATEMENT PARSER – 2-LINE DESCRIPTION FORMAT

    For each transaction, description will be EXACTLY:

        Line 1: WDL TFR / DEP TFR / ATM WDL
        Line 2: UPI/DR/... or UPI/CR/... or IMPS/... or ATM CASH ...

    - No amount tokens are kept inside description
    - Amounts are parsed into debit / credit / balance
    """
    reader = PdfReader(file_obj)

    # ---- Collect all lines ----
    all_lines = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for line in text.splitlines():
            line = line.strip()
            if line:
                all_lines.append(line)

    # ---- META ----
    meta = {
        "account_name": "",
        "account_number": "",
        "period": "",
        "opening_balance": 0.0,
    }

    for line in all_lines:
        if line.startswith(("Mrs.", "Mr.", "M/s", "Ms.")):
            meta["account_name"] = line

        if "Account No" in line:
            meta["account_number"] = line.split("Account No")[-1].strip()

        if "Statement From :" in line:
            meta["period"] = line.replace("Statement From :", "").strip()

        if line.startswith("BROUGHT FORWARD"):
            meta["opening_balance"] = _parse_amount(
                line.split("BROUGHT FORWARD")[-1]
            )

    # ---- Group lines by transaction (date row + following lines) ----
    transactions_raw = []
    current = None
    header_seen = False
    in_table = False

    # date row looks like: "10-03-2025 10-03-2025  ..." (two dates)
    date_line_pattern = re.compile(r"^(\d{2}-\d{2}-\d{4})\s+(\d{2}-\d{2}-\d{4})")

    for line in all_lines:
        # detect header two-lines
        if "Post Date" in line and "Value Date" in line and "Description" in line:
            header_seen = True
            continue
        if header_seen and "Debit" in line and "Credit" in line and "Balance" in line:
            in_table = True
            continue

        if not in_table:
            continue

        m = date_line_pattern.match(line)
        if m:
            # save older transaction
            if current:
                transactions_raw.append(current)

            # new transaction block
            current = {
                "date": m.group(1),
                "value_date": m.group(2),
                "lines": [],
            }

            # tail of the line after the two dates (may contain WDL TFR...)
            tail = line[m.end():].strip()
            if tail:
                current["lines"].append(tail)
        else:
            # Skip page footer inside statement text
            if line.startswith("Page no."):
                continue
            if current:
                current["lines"].append(line)

    if current:
        transactions_raw.append(current)

    # ---- Convert raw blocks into structured rows ----
    transactions = []

    for block in transactions_raw:
        date = block["date"]
        value_date = block["value_date"]
        raw_lines = block["lines"]

        # Join block text (without dates)
        merged = " ".join(raw_lines)
        merged = re.sub(r"\s+", " ", merged).strip()

        # ----- DESCRIPTION: 2 lines, no amounts -----
        line1, line2, line2_pos = _clean_description_from_block(merged)
        # Store both lines in the transaction for precise rendering later
        description = (f"{line1}\n{line2}" if line1 and line2 else (line1 or line2))

        # ----- NUMERIC TOKENS FOR AMOUNTS -----
        # Find amount-like substrings anywhere in the merged text. This
        # handles cases where the amount is glued to other text (e.g.
        # "GUDISE100.00") which a simple split() would miss.
        amount_pattern = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?")
        amounts_with_pos = [(m.group(0), m.start(), m.end()) for m in amount_pattern.finditer(merged)]
        amounts = [a[0] for a in amounts_with_pos]

        debit = 0.0
        credit = 0.0
        balance = 0.0
        balance_tok = ""

        if amounts:
            # Last matched amount is almost always the running balance.
            balance_tok, balance_start, balance_end = amounts_with_pos[-1]
            balance = _parse_amount(balance_tok)

            # Try to find the transaction amount that occurs within the
            # description's second-line region (i.e. after line2_pos and
            # before the balance). This avoids picking small numeric parts
            # inside references that do not represent the transaction
            # amount. If none found, fall back to the previous match.
            amount_tok = None
            if line2_pos != -1:
                for tok, s, e in amounts_with_pos:
                    if s >= line2_pos and e <= balance_start:
                        amount_tok = tok
                        break

            if not amount_tok:
                if len(amounts) >= 2:
                    amount_tok = amounts[-2]
                else:
                    amount_tok = amounts[0]

            desc_upper = (description or "").upper()
            is_debit = (
                "WDL" in desc_upper
                or "UPI/DR" in desc_upper
                or "IMPS/DR" in desc_upper
                or "ATM WDL" in desc_upper
                or "POS/" in desc_upper
            )

            if is_debit:
                debit = _parse_amount(amount_tok)
            else:
                credit = _parse_amount(amount_tok)

        row = {
            "date": date,
            "value_date": value_date,
            "description": description,   # exactly 1–2 lines, NO amount
            "description_line1": line1,
            "description_line2": line2,
            "cheque_ref": "",
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "orig_debit_text": "",
            "orig_credit_text": "",
            "orig_balance_text": balance_tok,
        }
        transactions.append(row)

    return {
        "meta": meta,
        "transactions": transactions,
    }


# ---------------------------------------------------------------------
#  GENERATOR: JSON -> SBI-STYLE TABLE PDF
# ---------------------------------------------------------------------
def generate_pdf_from_data(statement_obj):
    """
    Generate SBI-style statement pages using only a table:

    - Only the table (no SBI logo / account details)
    - Per page ~25 rows of transactions
    - Top header band (blue)
    - Bottom blue row INSIDE the table, last cell has 'Page no. X'
    - Description supports two lines
    - All columns except Description are center-aligned
    - Header is NOT bold, but slightly larger font
    """
    buffer = io.BytesIO()

    data = statement_obj.data or {}
    meta = data.get("meta", {})
    txns = data.get("transactions", [])

    width, height = A4

    # ---------- DOCUMENT / MARGINS ----------
    left_margin = 4 * mm
    right_margin = 9 * mm
    top_margin = 5 * mm
    bottom_margin = 8 * mm

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
    )

    usable_width = width - left_margin - right_margin

    # Column widths (sum = 1.0 of usable width)
    col_widths = [
        usable_width * 0.11,  # Post Date
        usable_width * 0.11,  # Value Date
        usable_width * 0.26,  # Description
        usable_width * 0.16,  # Cheque No/Reference
        usable_width * 0.12,  # Debit
        usable_width * 0.12,  # Credit
        usable_width * 0.12,  # Balance
    ]

    # ---------- Paragraph Styles ----------
    # Header: NOT bold, slightly larger size
    style_header = ParagraphStyle(
        name='header',
        fontName='Helvetica',
        fontSize=10,
        alignment=TA_CENTER,
    )

    style_center = ParagraphStyle(
        name='center',
        fontName='Helvetica',
        fontSize=8,
        alignment=TA_CENTER,
    )

    style_number = ParagraphStyle(
        name='number',
        fontName='Helvetica',
        fontSize=8,
        alignment=TA_RIGHT,
    )

    style_left = ParagraphStyle(
        name='left',
        fontName='Helvetica',
        fontSize=8,
        alignment=TA_LEFT,
    )

    story = []
    # Fixed row height (all cells will use this). Increase if two lines
    # need more vertical space.
    ROW_HEIGHT = 10.5 * mm

    # Compute rows per page dynamically from usable height so rows fit
    usable_height = height - top_margin - bottom_margin
    header_height = ROW_HEIGHT
    footer_height = ROW_HEIGHT
    ROWS_PER_PAGE = max(8, int((usable_height - header_height - footer_height) / ROW_HEIGHT))
    page_no = 1

    opening_balance = float(meta.get("opening_balance") or 0)
    running_balance_global = opening_balance

    # Add account meta block at the top of the first page
    acct_name = str(meta.get('account_name') or '')
    acct_no = str(meta.get('account_number') or '')
    period = str(meta.get('period') or '')

    if acct_name or acct_no or period:
        meta_table = [
            [Paragraph(f"<b>{acct_name}</b>", style_left), Paragraph(f"Opening Balance: {opening_balance:,.2f}CR", style_number)],
            [Paragraph(f"Account No: {acct_no}", style_left), Paragraph(f"Period: {period}", style_number)],
        ]
        # Small two-row meta table
        mt = Table(meta_table, colWidths=[usable_width * 0.7, usable_width * 0.3])
        mt.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 1.5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(mt)

    for start in range(0, len(txns), ROWS_PER_PAGE):
        chunk = txns[start:start + ROWS_PER_PAGE]

        table_data = []

        # ---------- Header row ----------
        table_data.append([
            Paragraph("Post Date", style_header),
            Paragraph("Value Date", style_header),
            Paragraph("Description", style_header),
            Paragraph("Cheque<br/>No/Reference", style_header),
            Paragraph("Debit", style_header),
            Paragraph("Credit", style_header),
            Paragraph("Balance", style_header),
        ])

        # ---------- Transaction rows ----------
        for txn in chunk:
            debit = float(txn.get("debit") or 0)
            credit = float(txn.get("credit") or 0)
            running_balance_global = running_balance_global - debit + credit

            # Use the stored description lines (cleaned by parser) so we
            # render the first line exactly as detected (no accidental wrap).
            # Build a nested two-row Table for the description cell which
            # guarantees the exact break and is a proper Flowable for Table.
            line1 = txn.get('description_line1') or ''
            line2 = txn.get('description_line2') or ''
            if line1 and line2:
                desc_table = Table([
                    [Paragraph(line1, style_left)],
                    [Paragraph(line2, style_left)],
                ], colWidths=[col_widths[2]])
                desc_table.setStyle(TableStyle([
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ]))
            else:
                desc_table = Paragraph(line1 or line2 or "", style_left)

            row = [
                Paragraph(str(txn.get("date", "")), style_center),
                Paragraph(str(txn.get("value_date", "")), style_center),
                desc_table,  # nested table (or single Paragraph) for description
                Paragraph(str(txn.get("cheque_ref", "")), style_center),
                Paragraph(f"{debit:,.2f}" if debit else "", style_center),
                Paragraph(f"{credit:,.2f}" if credit else "", style_center),
                Paragraph(f"{running_balance_global:,.2f}CR", style_center),
            ]
            table_data.append(row)

        # ---------- Bottom blue row inside table (no page number requested) ----------
        footer_row = [
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
        ]
        table_data.append(footer_row)

        # Force uniform row heights so every cell has equal vertical size.
        t = Table(table_data, colWidths=col_widths, repeatRows=1, rowHeights=ROW_HEIGHT)

        last_row_idx = len(table_data) - 1

        # ---------- TABLE STYLE ----------
        t.setStyle(TableStyle([
            # grid
            ('GRID', (0, 0), (-1, -1), 0.30, colors.HexColor('#B0B0B0')),

            # header band (first row) - sky blue #bfe1ff
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#bfe1ff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),

            # bottom blue band (footer row inside table)
            ('BACKGROUND', (0, last_row_idx), (-1, last_row_idx), colors.HexColor('#bfe1ff')),
            ('TEXTCOLOR', (0, last_row_idx), (-1, last_row_idx), colors.black),

            # vertical alignment
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),

            # padding (default)
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),

            # increase left padding specifically for Description column
            ('LEFTPADDING', (2, 0), (2, -1), 6),

            # uniform row height (style fallback)
            ('ROWHEIGHT', (0, 0), (-1, -1), ROW_HEIGHT),
        ]))

        t.hAlign = 'LEFT'
        story.append(t)

        page_no += 1
        if start + ROWS_PER_PAGE < len(txns):
            story.append(PageBreak())

    # draw page number at bottom of each page using canvas callback
    def _draw_page_number(canvas, doc_obj):
        canvas.saveState()
        # increase footer font size for better readability
        canvas.setFont('Helvetica', 10)
        # place the page number centered horizontally beneath the table
        # area (center = left_margin + usable_width/2), and a bit above the
        # bottom margin for a comfortable gap.
        # Use absolute page-centered coordinates so the page number is
        # fixed in the same place on every page regardless of margins
        # or table widths. Center horizontally using full page width,
        # and place a fixed distance (14mm) above the bottom edge.
        x = width / 2.0
        y = 14 * mm
        canvas.drawCentredString(x, y, f"Page no. {doc_obj.page}")
        canvas.restoreState()

    # build PDF with footer callback
    doc.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)

    buffer.seek(0)
    return ContentFile(buffer.read(), name=f"statement_{statement_obj.id}_updated.pdf")











import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponseForbidden, HttpResponseBadRequest

from .forms import LoginForm, StatementUploadForm
from .models import Statement
from .utils import parse_pdf_to_data, generate_pdf_from_data


def login_view(request):
    if request.user.is_authenticated:
        return redirect('statements:dashboard')

    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('statements:dashboard')

    return render(request, 'statements/login.html', {'form': form})


@login_required
def logout_view(request):
    logout(request)
    return redirect('statements:login')


@login_required
def dashboard(request):
    statements = Statement.objects.filter(user=request.user).order_by('-uploaded_at')
    upload_form = StatementUploadForm()
    return render(request, 'statements/dashboard.html', {
        'statements': statements,
        'upload_form': upload_form,
    })


@login_required
def upload_statement(request):
    if request.method != 'POST':
        return redirect('statements:dashboard')

    form = StatementUploadForm(request.POST, request.FILES)
    if form.is_valid():
        stmt = form.save(commit=False)
        stmt.user = request.user

        bank = request.POST.get('bank') or 'SBI'
        layout = request.POST.get('layout') or 'SBI_POST_VALUE'

        uploaded_file = request.FILES['original_file']

        parsed_data = parse_pdf_to_data(uploaded_file, layout)

        if 'meta' not in parsed_data or parsed_data['meta'] is None:
            parsed_data['meta'] = {}
        parsed_data['meta']['bank'] = bank
        parsed_data['meta']['layout'] = layout

        uploaded_file.seek(0)

        stmt.data = parsed_data
        stmt.save()

        return redirect('statements:edit', pk=stmt.pk)

    statements = Statement.objects.filter(user=request.user).order_by('-uploaded_at')
    return render(request, 'statements/dashboard.html', {
        'statements': statements,
        'upload_form': form,
    })


@login_required
def edit_statement(request, pk):
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)
    data = stmt.data or {}
    return render(request, 'statements/edit_statement.html', {
        'statement': stmt,
        'data_json': json.dumps(data),
        'data': data,
    })


@login_required
def save_statement(request, pk):
    if request.method != 'POST':
        return HttpResponseForbidden("Only POST allowed")

    stmt = get_object_or_404(Statement, pk=pk, user=request.user)

    data_json = request.POST.get('data_json')
    if not data_json:
        return HttpResponseBadRequest("Missing data_json in POST")

    try:
        updated_data = json.loads(data_json)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON data")

    stmt.data = updated_data
    stmt.save()

    action = request.POST.get('action', 'download')

    if action == 'save':
        return redirect('statements:edit', pk=stmt.pk)
    else:
        return redirect('statements:download', pk=stmt.pk)


@login_required
def delete_statement(request, pk):
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)

    if stmt.original_file:
        stmt.original_file.delete(save=False)
    if stmt.edited_file:
        stmt.edited_file.delete(save=False)

    stmt.delete()
    return redirect('statements:dashboard')


@login_required
def download_statement(request, pk):
    stmt = get_object_or_404(Statement, pk=pk, user=request.user)

    pdf_file = generate_pdf_from_data(stmt)
    stmt.edited_file.save(pdf_file.name, pdf_file, save=True)

    return FileResponse(
        stmt.edited_file.open('rb'),
        as_attachment=True,
        filename=stmt.edited_file.name
    )














from django.db import models
from django.conf import settings

class Statement(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    original_file = models.FileField(upload_to='statements/original/')
    edited_file = models.FileField(upload_to='statements/edited/', null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField()

    def __str__(self):
        return f"Statement #{self.id} by {self.user.username}"
















{% extends 'statements/base.html' %}
{% block title %}Dashboard{% endblock %}

{% block extra_head %}
<style>
  body {
    overflow: hidden;
    background: radial-gradient(circle at top, #e0f2ff 0, #f9fafb 50%, #eef2ff 100%);
  }

  .dashboard-container {
    padding: 2rem 1rem;
    max-height: calc(100vh - 70px);
    overflow-y: auto;
    display: flex;
    justify-content: center;
  }

  .dashboard-shell {
    width: 100%;
    max-width: 1120px;
  }

  .card {
    border-radius: 16px;
    border: 1px solid rgba(148, 163, 184, 0.35);
    box-shadow: 0 18px 45px rgba(15, 23, 42, 0.12);
    backdrop-filter: blur(12px);
    background: rgba(255, 255, 255, 0.96);
  }

  .card-soft {
    border-radius: 14px;
    border: 1px solid rgba(148, 163, 184, 0.25);
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    background: rgba(255, 255, 255, 0.98);
  }

  .card-title {
    font-weight: 700;
    font-size: 1.25rem;
    margin-bottom: 0.35rem;
    letter-spacing: 0.01em;
  }

  .step-label {
    font-size: 0.8rem;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.15em;
    color: #6b7280;
  }

  .text-muted-small {
    font-size: 0.82rem;
    color: #6b7280;
  }

  .d-none {
    display: none !important;
  }

  .bank-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1rem;
  }

  .bank-card {
    cursor: pointer;
    border-radius: 14px;
    padding: 0.9rem 0.85rem;
    color: #fff;
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border: 1px solid transparent;
    transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease, background-position 0.3s ease;
    background-size: 160% 160%;
    background-position: 0 0;
  }

  .bank-icon-circle {
    width: 40px;
    height: 40px;
    border-radius: 999px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(15, 23, 42, 0.1);
    backdrop-filter: blur(6px);
    flex-shrink: 0;
  }

  .bank-icon-circle img {
    width: 26px;
    height: 26px;
    object-fit: contain;
  }

  .bank-name {
    font-weight: 700;
    font-size: 0.98rem;
  }

  .bank-subtitle {
    font-size: 0.78rem;
    opacity: 0.95;
  }

  .bank-card-sbi {
    background-image: linear-gradient(135deg, #0047ab, #00b4ff);
  }

  .bank-card-hdfc {
    background-image: linear-gradient(135deg, #b3001b, #ff4b5c);
  }

  .bank-card-icici {
    background-image: linear-gradient(135deg, #f46b45, #eea849);
  }

  .bank-card-axis {
    background-image: linear-gradient(135deg, #6d28d9, #d946ef);
  }

  .bank-card:hover {
    transform: translateY(-2px) translateZ(0);
    box-shadow: 0 18px 40px rgba(15, 23, 42, 0.35);
    border-color: rgba(255, 255, 255, 0.7);
    background-position: 20% 20%;
  }

  .bank-card.selected {
    box-shadow: 0 20px 50px rgba(15, 23, 42, 0.45);
    border-color: rgba(255, 255, 255, 0.9);
    transform: translateY(-1px);
  }

  .bank-pill {
    position: absolute;
    top: 0.6rem;
    right: 0.7rem;
    font-size: 0.65rem;
    background: rgba(15, 23, 42, 0.25);
    border-radius: 999px;
    padding: 0.1rem 0.45rem;
    font-weight: 600;
  }

  .model-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.75rem;
  }

  .model-card {
    cursor: pointer;
    border-radius: 12px;
    border: 1px solid rgba(148, 163, 184, 0.4);
    padding: 0.8rem 0.85rem;
    transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease, background-color 0.16s ease;
    background: linear-gradient(135deg, #f9fafb, #eef2ff);
  }

  .model-card:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 22px rgba(15, 23, 42, 0.18);
    border-color: #2563eb;
  }

  .model-card.selected {
    border-color: #2563eb;
    box-shadow: 0 0 0 1px rgba(37, 99, 235, 0.45);
    background: linear-gradient(135deg, #e0ecff, #eef2ff);
  }

  .model-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.25rem;
  }

  .model-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: #111827;
  }

  .model-badge {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: #2563eb;
  }

  .upload-card-inner {
    padding: 1.3rem 1.2rem 1.1rem;
  }

  .upload-hint {
    font-size: 0.82rem;
    color: #4b5563;
  }

  .selected-model-label {
    font-weight: 600;
    color: #111827;
  }

  .btn-primary {
    border-radius: 999px;
    font-weight: 600;
    letter-spacing: 0.03em;
  }

  .file-input-wrapper {
    border-radius: 12px;
    border: 1px dashed rgba(148, 163, 184, 0.9);
    padding: 0.8rem 0.9rem;
    background: #f9fafb;
  }

  .file-input-wrapper label {
    font-size: 0.85rem;
    font-weight: 500;
    margin-bottom: 0.35rem;
    color: #111827;
  }

  .file-input-wrapper input[type="file"] {
    font-size: 0.82rem;
  }

  @media (max-width: 991.98px) {
    .dashboard-container {
      padding: 1.5rem 0.75rem;
    }
    .dashboard-shell {
      max-width: 100%;
    }
    .bank-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .model-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (max-width: 575.98px) {
    .dashboard-container {
      padding: 1.2rem 0.6rem;
    }
    .bank-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.8rem;
    }
    .model-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .bank-card {
      padding: 0.8rem 0.7rem;
    }
    .bank-icon-circle {
      width: 36px;
      height: 36px;
    }
    .card {
      border-radius: 14px;
    }
  }
</style>

<script>
let selectedBank = null;
let selectedLayout = null;

function resetModelsAndUpload() {
  document.querySelectorAll('.bank-models-group').forEach(el => el.classList.add('d-none'));
  document.querySelectorAll('.model-card').forEach(el => el.classList.remove('selected'));
  const uploadCard = document.getElementById('uploadCard');
  if (uploadCard) uploadCard.classList.add('d-none');
  const label = document.getElementById('selectedModelLabel');
  const bankInput = document.getElementById('bankInput');
  const layoutInput = document.getElementById('layoutInput');
  const uploadBtn = document.getElementById('uploadSubmitBtn');
  if (label) label.textContent = 'No model selected';
  if (bankInput) bankInput.value = '';
  if (layoutInput) layoutInput.value = '';
  if (uploadBtn) uploadBtn.disabled = true;
}

function selectBank(bank) {
  selectedBank = bank;
  selectedLayout = null;
  document.querySelectorAll('.bank-card').forEach(el => el.classList.remove('selected'));
  const bankCard = document.getElementById('bank-card-' + bank);
  if (bankCard) bankCard.classList.add('selected');
  resetModelsAndUpload();
  const modelsGroup = document.getElementById('models-' + bank);
  if (modelsGroup) modelsGroup.classList.remove('d-none');
}

function selectModel(bank, layout, label) {
  selectedBank = bank;
  selectedLayout = layout;
  document.querySelectorAll('.model-card').forEach(el => el.classList.remove('selected'));
  const modelCard = document.getElementById('model-card-' + layout);
  if (modelCard) modelCard.classList.add('selected');
  const bankInput = document.getElementById('bankInput');
  const layoutInput = document.getElementById('layoutInput');
  const labelSpan = document.getElementById('selectedModelLabel');
  const uploadCard = document.getElementById('uploadCard');
  const uploadBtn = document.getElementById('uploadSubmitBtn');
  if (bankInput) bankInput.value = bank;
  if (layoutInput) layoutInput.value = layout;
  if (labelSpan) labelSpan.textContent = label + ' (' + bank + ')';
  if (uploadCard) uploadCard.classList.remove('d-none');
  if (uploadBtn) uploadBtn.disabled = false;
}

document.addEventListener('DOMContentLoaded', function() {});
</script>
{% endblock %}

{% block content %}
<div class="dashboard-container">
  <div class="dashboard-shell">
    <div class="card mb-4">
      <div class="card-body" style="padding: 1.4rem 1.3rem 1.2rem;">
        <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center mb-3">
          <div>
            <div class="step-label">Step 1</div>
            <h5 class="card-title mb-1">Choose your bank</h5>
            <p class="text-muted-small mb-0">Then select the exact statement format and upload the PDF.</p>
          </div>
        </div>

        <div class="bank-grid">
          <div>
            <div id="bank-card-SBI"
                 class="bank-card bank-card-sbi"
                 onclick="selectBank('SBI')">
              <div class="bank-pill">Supported</div>
              <div class="bank-icon-circle">
                <img src="https://1000logos.net/wp-content/uploads/2018/03/SBI-Logo.png" alt="SBI">
              </div>
              <div>
                <div class="bank-name">SBI</div>
                <div class="bank-subtitle">Multiple statement layouts</div>
              </div>
            </div>
          </div>

          <div>
            <div id="bank-card-HDFC"
                 class="bank-card bank-card-hdfc"
                 onclick="selectBank('HDFC')">
              <div class="bank-pill">Coming soon</div>
              <div class="bank-icon-circle">
                <img src="https://crystalpng.com/wp-content/uploads/2025/09/hdfc-bank-logo.png" alt="HDFC">
              </div>
              <div>
                <div class="bank-name">HDFC Bank</div>
                <div class="bank-subtitle">Planned support</div>
              </div>
            </div>
          </div>

          <div>
            <div id="bank-card-ICICI"
                 class="bank-card bank-card-icici"
                 onclick="selectBank('ICICI')">
              <div class="bank-pill">Coming soon</div>
              <div class="bank-icon-circle">
                <img src="https://cdn.freebiesupply.com/logos/large/2x/icici-1-logo-svg-vector.svg" alt="ICICI">
              </div>
              <div>
                <div class="bank-name">ICICI Bank</div>
                <div class="bank-subtitle">Planned support</div>
              </div>
            </div>
          </div>

          <div>
            <div id="bank-card-AXIS"
                 class="bank-card bank-card-axis"
                 onclick="selectBank('AXIS')">
              <div class="bank-pill">Coming soon</div>
              <div class="bank-icon-circle">
                <img src="https://www.jobsgyan.in/wp-content/uploads/2021/05/Axis-Bank-PNG-Logo-.png" alt="Axis">
              </div>
              <div>
                <div class="bank-name">Axis Bank</div>
                <div class="bank-subtitle">Planned support</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div id="models-SBI" class="card card-soft mb-3 bank-models-group d-none">
      <div class="card-body" style="padding: 1.2rem 1.2rem 1.05rem;">
        <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center mb-3">
          <div>
            <div class="step-label">Step 2</div>
            <h5 class="card-title mb-1">Choose SBI statement format</h5>
            <p class="text-muted-small mb-0">Match the columns of your PDF with one of the formats below.</p>
          </div>
          <div class="mt-3 mt-md-0 text-muted-small">
            Bank selected: <span class="fw-semibold">SBI</span>
          </div>
        </div>

        <div class="model-grid">
          <div>
            <div id="model-card-SBI_POST_VALUE"
                 class="model-card"
                 onclick="selectModel('SBI', 'SBI_POST_VALUE', 'SBI – Post Date / Value Date')">
              <div class="model-header">
                <span class="model-title">Post Date / Value Date</span>
                <span class="model-badge">Model 1</span>
              </div>
              <div class="text-muted-small">
                Columns: Post Date, Value Date, Description, Cheque No/Reference, Debit, Credit, Balance
              </div>
            </div>
          </div>

          <div>
            <div id="model-card-SBI_TXN_VALUE"
                 class="model-card"
                 onclick="selectModel('SBI', 'SBI_TXN_VALUE', 'SBI – Txn Date / Value Date')">
              <div class="model-header">
                <span class="model-title">Txn Date / Value Date</span>
                <span class="model-badge">Model 2</span>
              </div>
              <div class="text-muted-small">
                Columns: Txn Date, Value Date, Description, Ref No./Cheque No., Debit, Credit, Balance
              </div>
            </div>
          </div>

          <div>
            <div id="model-card-SBI_DATE_DETAILS"
                 class="model-card"
                 onclick="selectModel('SBI', 'SBI_DATE_DETAILS', 'SBI – Date / Details')">
              <div class="model-header">
                <span class="model-title">Date / Details / Ref No.</span>
                <span class="model-badge">Model 3</span>
              </div>
              <div class="text-muted-small">
                Columns: Date, Details, Ref No./Cheque No., Debit, Credit, Balance
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div id="uploadCard" class="card card-soft mb-4 d-none">
      <div class="card-body upload-card-inner">
        <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center mb-3">
          <div>
            <div class="step-label">Step 3</div>
            <h5 class="card-title mb-1">Upload statement PDF</h5>
            <p class="upload-hint mb-0">
              Selected model:
              <span id="selectedModelLabel" class="selected-model-label">No model selected</span>
            </p>
          </div>
        </div>

        <form method="post" action="{% url 'statements:upload' %}" enctype="multipart/form-data">
          {% csrf_token %}
          {{ upload_form.non_field_errors }}

          <input type="hidden" name="bank" id="bankInput">
          <input type="hidden" name="layout" id="layoutInput">

          <div class="file-input-wrapper mb-3">
            {{ upload_form.original_file.label_tag }}
            {{ upload_form.original_file }}
            {{ upload_form.original_file.errors }}
            <div class="text-muted-small mt-1">
              Supported: SBI PDF statements. Make sure the format matches the selected model.
            </div>
          </div>

          <button type="submit"
                  id="uploadSubmitBtn"
                  class="btn btn-primary w-100">
            Upload and continue
          </button>
        </form>
      </div>
    </div>
  </div>
</div>
{% endblock %}
















import io
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, PageBreak
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from PyPDF2 import PdfReader
from django.core.files.base import ContentFile


def _parse_amount(text: str) -> float:
    if not text:
        return 0.0
    text = text.replace("CR", "").replace("DR", "").strip()
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _clean_description_from_block(text: str):
    text = re.sub(r"\s+", " ", text).strip()
    line1 = ""
    line2 = ""
    upper = text.upper()
    if upper.startswith("TO TRANSFER"):
        line1 = "TO TRANSFER"
        line2 = text[len("TO TRANSFER"):].strip()
    elif upper.startswith("BY TRANSFER"):
        line1 = "BY TRANSFER"
        line2 = text[len("BY TRANSFER"):].strip()
    elif upper.startswith("ATM WDL"):
        line1 = "ATM WDL"
        line2 = text[len("ATM WDL"):].strip()
    else:
        parts = text.split(" ", 1)
        line1 = parts[0]
        line2 = parts[1] if len(parts) > 1 else ""
    return line1, line2, -1


def parse_pdf_to_data(file_obj):
    reader = PdfReader(file_obj)

    all_lines = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for line in text.splitlines():
            line = line.strip()
            if line:
                all_lines.append(line)

    meta = {
        "account_name": "",
        "account_number": "",
        "period": "",
        "opening_balance": 0.0,
        "bank": "SBI",
        "layout": "SBI_TXN_VALUE",
    }

    for line in all_lines:
        if line.startswith(("Mrs.", "Mr.", "M/s", "Ms.")):
            meta["account_name"] = line
        if "Account No" in line:
            meta["account_number"] = line.split("Account No")[-1].strip()
        if "Statement From :" in line:
            meta["period"] = line.replace("Statement From :", "").strip()
        if line.upper().startswith(("BROUGHT FORWARD", "BALANCE B/F")):
            parts = line.split()
            for part in reversed(parts):
                if re.match(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?", part):
                    meta["opening_balance"] = _parse_amount(part)
                    break

    transactions_raw = []
    current = None
    in_table = False

    date_pattern = r"(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})"
    row_start_pattern = re.compile(
        rf"^(?P<d1>{date_pattern})\s+(?P<d2>{date_pattern})\s+(?P<rest>.+)$"
    )

    for line in all_lines:
        upper = line.upper()

        if not in_table and "TXN DATE" in upper and "VALUE DATE" in upper:
            in_table = True
            continue

        if not in_table:
            continue

        if any(stop in upper for stop in ["CLOSING BALANCE", "CLOSING BAL.", "TOTAL", "SUMMARY", "PAGE NO"]):
            if current:
                transactions_raw.append(current)
                current = None
            break

        m = row_start_pattern.match(line)
        if m:
            if current:
                transactions_raw.append(current)
            current = {
                "date": m.group("d1"),
                "value_date": m.group("d2"),
                "lines": [m.group("rest").strip()],
            }
        else:
            if current:
                current["lines"].append(line)

    if current:
        transactions_raw.append(current)

    transactions = []
    amount_pattern = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?")

    for block in transactions_raw:
        date = block["date"]
        value_date = block["value_date"]
        body = " ".join(block["lines"])
        body = re.sub(r"\s+", " ", body).strip()

        amounts_with_pos = [(mm.group(0), mm.start(), mm.end()) for mm in amount_pattern.finditer(body)]
        debit = 0.0
        credit = 0.0
        balance = 0.0
        balance_tok = ""

        if amounts_with_pos:
            balance_tok, b_start, b_end = amounts_with_pos[-1]
            balance = _parse_amount(balance_tok)
            if len(amounts_with_pos) >= 2:
                amount_tok = amounts_with_pos[-2][0]
                first_amt_start = amounts_with_pos[0][1]
            else:
                amount_tok = amounts_with_pos[0][0]
                first_amt_start = amounts_with_pos[0][1]

            upper_body = body.upper()
            is_debit = (
                "UPI/DR" in upper_body
                or " DR/" in upper_body
                or upper_body.endswith("DR")
                or "/DR " in upper_body
                or "TO TRANSFER" in upper_body
                or "ATM WDL" in upper_body
            )
            if amount_tok.endswith("DR"):
                is_debit = True
            if amount_tok.endswith("CR"):
                is_debit = False

            amt_val = _parse_amount(amount_tok)
            if is_debit:
                debit = amt_val
            else:
                credit = amt_val
        else:
            first_amt_start = len(body)

        desc_ref_part = body[:first_amt_start].strip()
        tokens = desc_ref_part.split()
        ref_tokens = []
        while tokens and re.match(r"^[A-Z0-9/-]{6,}$", tokens[-1]):
            ref_tokens.insert(0, tokens.pop())
        cheque_ref = " ".join(ref_tokens)
        desc_text = " ".join(tokens)

        line1, line2, _ = _clean_description_from_block(desc_text)
        description = f"{line1}\n{line2}" if line1 and line2 else (line1 or line2)

        transactions.append({
            "date": date,
            "value_date": value_date,
            "description": description,
            "description_line1": line1 or description,
            "description_line2": line2,
            "cheque_ref": cheque_ref,
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "orig_debit_text": "",
            "orig_credit_text": "",
            "orig_balance_text": balance_tok,
        })

    if (meta.get("opening_balance") in (0, 0.0, None)) and transactions:
        first = transactions[0]
        ob = (first.get("balance") or 0.0) + (first.get("debit") or 0.0) - (first.get("credit") or 0.0)
        meta["opening_balance"] = ob

    return {
        "meta": meta,
        "transactions": transactions,
    }


def generate_pdf_from_data(statement_obj):
    buffer = io.BytesIO()

    data = statement_obj.data or {}
    meta = data.get("meta", {})
    txns = data.get("transactions", [])

    width, height = A4
    left_margin = 4 * mm
    right_margin = 9 * mm
    top_margin = 5 * mm
    bottom_margin = 8 * mm

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
    )

    usable_width = width - left_margin - right_margin

    col_widths = [
        usable_width * 0.11,
        usable_width * 0.11,
        usable_width * 0.26,
        usable_width * 0.16,
        usable_width * 0.12,
        usable_width * 0.12,
        usable_width * 0.12,
    ]

    style_header = ParagraphStyle(
        name='header',
        fontName='Helvetica',
        fontSize=10,
        alignment=TA_CENTER,
    )

    style_center = ParagraphStyle(
        name='center',
        fontName='Helvetica',
        fontSize=8,
        alignment=TA_CENTER,
    )

    style_number = ParagraphStyle(
        name='number',
        fontName='Helvetica',
        fontSize=8,
        alignment=TA_RIGHT,
    )

    style_left = ParagraphStyle(
        name='left',
        fontName='Helvetica',
        fontSize=8,
        alignment=TA_LEFT,
    )

    story = []
    ROW_HEIGHT = 10.7 * mm

    usable_height = height - top_margin - bottom_margin
    header_height = ROW_HEIGHT
    footer_height = ROW_HEIGHT
    ROWS_PER_PAGE = max(8, int((usable_height - header_height - footer_height) / ROW_HEIGHT))

    opening_balance = float(meta.get("opening_balance") or 0)
    running_balance_global = opening_balance

    acct_name = str(meta.get('account_name') or '')
    acct_no = str(meta.get('account_number') or '')
    period = str(meta.get('period') or '')

    if acct_name or acct_no or period:
        meta_table = [
            [Paragraph(f"<b>{acct_name}</b>", style_left),
             Paragraph(f"Opening Balance: {opening_balance:,.2f}", style_number)],
            [Paragraph(f"Account No: {acct_no}", style_left),
             Paragraph(f"Period: {period}", style_number)],
        ]
        mt = Table(meta_table, colWidths=[usable_width * 0.7, usable_width * 0.3])
        mt.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 1.5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(mt)

    for start in range(0, len(txns), ROWS_PER_PAGE):
        chunk = txns[start:start + ROWS_PER_PAGE]

        table_data = []

        table_data.append([
            Paragraph("Txn Date", style_header),
            Paragraph("Value Date", style_header),
            Paragraph("Details", style_header),
            Paragraph("Ref No./Cheque No.", style_header),
            Paragraph("Debit", style_header),
            Paragraph("Credit", style_header),
            Paragraph("Balance", style_header),
        ])

        for txn in chunk:
            debit = float(txn.get("debit") or 0)
            credit = float(txn.get("credit") or 0)
            running_balance_global = running_balance_global - debit + credit

            details = txn.get('description_line1') or txn.get('description') or ''
            desc_para = Paragraph(details, style_left)

            row = [
                Paragraph(str(txn.get("date", "")), style_center),
                Paragraph(str(txn.get("value_date", "")), style_center),
                desc_para,
                Paragraph(str(txn.get("cheque_ref", "")), style_center),
                Paragraph(f"{debit:,.2f}" if debit else "", style_center),
                Paragraph(f"{credit:,.2f}" if credit else "", style_center),
                Paragraph(f"{running_balance_global:,.2f}", style_center),
            ]
            table_data.append(row)

        footer_row = [
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
            Paragraph("", style_center),
        ]
        table_data.append(footer_row)

        t = Table(table_data, colWidths=col_widths, repeatRows=1, rowHeights=ROW_HEIGHT)

        last_row_idx = len(table_data) - 1

        t.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.30, colors.HexColor('#B0B0B0')),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#bfe1ff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('BACKGROUND', (0, last_row_idx), (-1, last_row_idx), colors.HexColor('#bfe1ff')),
            ('TEXTCOLOR', (0, last_row_idx), (-1, last_row_idx), colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (2, 0), (2, -1), 6),
            ('ROWHEIGHT', (0, 0), (-1, -1), ROW_HEIGHT),
        ]))

        t.hAlign = 'LEFT'
        story.append(t)

        if start + ROWS_PER_PAGE < len(txns):
            story.append(PageBreak())

    def _draw_page_number(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 10)
        x = width / 2.0
        y = 14 * mm
        canvas.drawCentredString(x, y, f"Page no. {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)

    buffer.seek(0)
    return ContentFile(buffer.read(), name=f"statement_{statement_obj.id}_updated.pdf")















{% extends 'statements/base.html' %}
{% block title %}Edit Statement {{ statement.id }}{% endblock %}

{% block extra_head %}
<script>
let statementData = JSON.parse('{{ data_json|escapejs }}');

function recalcBalances() {
  const meta = statementData.meta || {};
  let opening = parseFloat(meta.opening_balance || 0);
  let runningBalance = isNaN(opening) ? 0 : opening;

  statementData.transactions.forEach((txn, idx) => {
    const debit = parseFloat(txn.debit || 0) || 0;
    const credit = parseFloat(txn.credit || 0) || 0;
    runningBalance = runningBalance - debit + credit;
    txn.balance = runningBalance;

    const balInput = document.querySelector(`#row-${idx} .balance-cell`);
    if (balInput) {
      balInput.value = runningBalance.toFixed(2);
    }
  });
}

function onAmountChange(idx, field, inputElem) {
  const val = parseFloat(inputElem.value || 0);
  statementData.transactions[idx][field] = isNaN(val) ? 0 : val;
  recalcBalances();
}

function onMetaChange(field, inputElem) {
  if (!statementData.meta) statementData.meta = {};
  statementData.meta[field] = inputElem.value;
}

function onSubmitForm() {
  statementData.transactions.forEach((txn, idx) => {
    ['date','value_date','description','cheque_ref','debit','credit','balance'].forEach(field => {
      const input = document.querySelector(`#row-${idx} .${field}-cell`);
      if (!input) return;
      if (['debit','credit','balance'].includes(field)) {
        const val = parseFloat(input.value || 0);
        txn[field] = isNaN(val) ? 0 : val;
      } else {
        txn[field] = input.value;
      }
    });
  });

  document.getElementById('data_json').value = JSON.stringify(statementData);
  return true;
}

document.addEventListener('DOMContentLoaded', function() {
  recalcBalances();
});
</script>
{% endblock %}

{% block content %}
{% with layout=data.meta.layout|default:'SBI_POST_VALUE' %}
<h4 class="mb-2">Edit Statement #{{ statement.id }}</h4>
<p class="text-muted mb-3">
  Bank: {{ data.meta.bank|default:"SBI" }},
  Layout:
  {% if layout == "SBI_TXN_VALUE" %}
    <span class="badge text-bg-primary">SBI – Txn Date / Value Date</span>
  {% else %}
    <span class="badge text-bg-secondary">SBI – Post Date / Value Date</span>
  {% endif %}
</p>

<form method="post" action="{% url 'statements:save' statement.id %}" onsubmit="return onSubmitForm();">
  {% csrf_token %}
  <input type="hidden" name="data_json" id="data_json">

  <div class="card mb-3 shadow-sm">
    <div class="card-body">
      <h5 class="card-title">Account Details</h5>
      <div class="row g-2">
        <div class="col-md-3">
          <label class="form-label">Account Name</label>
          <input type="text" class="form-control"
                 value="{{ data.meta.account_name }}"
                 oninput="onMetaChange('account_name', this)">
        </div>
        <div class="col-md-3">
          <label class="form-label">Account Number</label>
          <input type="text" class="form-control"
                 value="{{ data.meta.account_number }}"
                 oninput="onMetaChange('account_number', this)">
        </div>
        <div class="col-md-4">
          <label class="form-label">Period</label>
          <input type="text" class="form-control"
                 value="{{ data.meta.period }}"
                 oninput="onMetaChange('period', this)">
        </div>
        <div class="col-md-2">
          <label class="form-label">Opening Balance</label>
          <input type="number" step="0.01" class="form-control"
                 value="{{ data.meta.opening_balance }}"
                 oninput="onMetaChange('opening_balance', this); recalcBalances();">
        </div>
      </div>
    </div>
  </div>

  <div class="card shadow-sm">
    <div class="card-body">
      <h5 class="card-title">Transactions</h5>
      <div class="table-responsive">
        <table class="table table-sm table-bordered align-middle">
          <thead class="table-light">
            <tr>
              {% if layout == "SBI_TXN_VALUE" %}
                <th>Txn Date</th>
                <th>Value Date</th>
                <th>Details</th>
                <th>Ref No./Cheque No.</th>
                <th>Debit</th>
                <th>Credit</th>
                <th>Balance</th>
              {% else %}
                <th>Post Date</th>
                <th>Value Date</th>
                <th>Description</th>
                <th>Cheque / Ref</th>
                <th>Debit</th>
                <th>Credit</th>
                <th>Balance</th>
              {% endif %}
            </tr>
          </thead>
          <tbody>
          {% for txn in data.transactions %}
            <tr id="row-{{ forloop.counter0 }}">
              <td>
                <input type="text" class="form-control form-control-sm date-cell"
                       value="{{ txn.date }}">
              </td>
              <td>
                <input type="text" class="form-control form-control-sm value_date-cell"
                       value="{{ txn.value_date }}">
              </td>
              <td>
                <input type="text" class="form-control form-control-sm description-cell"
                       value="{{ txn.description }}">
              </td>
              <td>
                <input type="text" class="form-control form-control-sm cheque_ref-cell"
                       value="{{ txn.cheque_ref }}">
              </td>
              <td>
                <input type="number" step="0.01"
                       class="form-control form-control-sm debit-cell"
                       value="{{ txn.debit }}"
                       oninput="onAmountChange({{ forloop.counter0 }}, 'debit', this)">
              </td>
              <td>
                <input type="number" step="0.01"
                       class="form-control form-control-sm credit-cell"
                       value="{{ txn.credit }}"
                       oninput="onAmountChange({{ forloop.counter0 }}, 'credit', this)">
              </td>
              <td>
                <input type="number" step="0.01"
                       class="form-control form-control-sm balance-cell"
                       value="{{ txn.balance }}" readonly>
              </td>
            </tr>
          {% empty %}
            <tr>
              <td colspan="7" class="text-center">
                No transactions parsed from this PDF for
                {% if layout == "SBI_TXN_VALUE" %}
                  Txn Date / Value Date format.
                {% else %}
                  Post Date / Value Date format.
                {% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>

      <div class="d-flex justify-content-between mt-3">
        <a href="{% url 'statements:dashboard' %}" class="btn btn-outline-secondary">Back</a>
        <div>
          <button type="submit" name="action" value="save" class="btn btn-outline-primary me-2">
            Save
          </button>
          <button type="submit" name="action" value="download" class="btn btn-primary">
            Save &amp; Download Updated PDF
          </button>
        </div>
      </div>
    </div>
  </div>
</form>
{% endwith %}
{% endblock %}
