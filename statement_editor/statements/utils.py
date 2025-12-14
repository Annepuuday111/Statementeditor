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

# Layout constants
SBI_POST_VALUE = "SBI_POST_VALUE"
SBI_TXN_VALUE = "SBI_TXN_VALUE"
SBI_DATE_DETAILS = "SBI_DATE_DETAILS"


def _parse_amount(text):
    if not text:
        return 0.0
    s = str(text).upper()
    s = s.replace("CR", "").replace("DR", "")
    s = s.replace(",", "")
    s = s.replace("\u00A0", " ")
    s = s.replace("(", "-").replace(")", "")
    s = re.sub(r"[^\d\.-]", "", s).strip()
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except Exception:
        return 0.0


def _extract_all_lines(file_obj):
    try:
        file_obj.seek(0)
    except Exception:
        pass
    reader = PdfReader(file_obj)
    lines = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for line in text.splitlines():
            l = line.strip()
            if l:
                lines.append(l)
    return lines


def _normalize_multiline_dates(lines):
    out = []
    i = 0
    day_mon_re = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}$")
    year_re = re.compile(r"^\d{4}$")
    short_day_re = re.compile(r"^\d{1,2}$")
    short_mon_re = re.compile(r"^[A-Za-z]{3}$")
    while i < len(lines):
        line = lines[i]
        if i + 1 < len(lines) and day_mon_re.match(line) and year_re.match(lines[i + 1]):
            out.append(f"{line} {lines[i + 1]}")
            i += 2
            continue
        if i + 2 < len(lines) and short_day_re.match(line) and short_mon_re.match(lines[i + 1]) and year_re.match(lines[i + 2]):
            out.append(f"{line} {lines[i + 1]} {lines[i + 2]}")
            i += 3
            continue
        out.append(line)
        i += 1
    return out


# Parsers (kept robust through iterations)
def _parse_sbi_post_value(file_obj):
    lines = _extract_all_lines(file_obj)
    lines = _normalize_multiline_dates(lines)

    meta = {"account_name": "", "account_number": "", "period": "", "opening_balance": 0.0}

    for line in lines:
        if line.startswith(("Mrs.", "Mr.", "M/s", "Ms.")):
            meta["account_name"] = line
        if "Account No" in line:
            meta["account_number"] = line.split("Account No")[-1].strip()
        if "Statement From" in line:
            meta["period"] = line.split("Statement From")[-1].replace(":", "").strip()
        if line.upper().startswith(("BROUGHT FORWARD", "BALANCE B/F")):
            p = re.findall(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?", line)
            if p:
                meta["opening_balance"] = _parse_amount(p[-1])

    transactions_raw = []
    current = None

    date_line_pattern = re.compile(
        r"^(\d{2}[-/]\d{2}[-/]\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+"
        r"(\d{2}[-/]\d{2}[-/]\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4})"
    )

    header_seen = False
    in_table = False

    for line in lines:
        u = line.upper()
        if "POST DATE" in u and "VALUE DATE" in u and "DESCRIPTION" in u:
            header_seen = True
            continue
        if header_seen and ("DEBIT" in u and "CREDIT" in u and "BALANCE" in u):
            in_table = True
            continue
        if not in_table:
            continue

        m = date_line_pattern.match(line)
        if m:
            if current:
                transactions_raw.append(current)
            current = {"date": m.group(1), "value_date": m.group(2), "lines": []}
            tail = line[m.end():].strip()
            if tail:
                current["lines"].append(tail)
        else:
            if line.lower().startswith("page"):
                continue
            if current:
                current["lines"].append(line)

    if current:
        transactions_raw.append(current)

    transactions = []
    amt_re = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?")

    for block in transactions_raw:
        date = block["date"]
        value_date = block["value_date"]
        merged = " ".join(block["lines"])
        merged = re.sub(r"\s+", " ", merged).strip()

        amounts = [(m.group(0), m.start(), m.end()) for m in amt_re.finditer(merged)]
        debit = credit = balance = 0.0
        balance_tok = ""
        desc_line1 = desc_line2 = ""
        if amounts:
            balance_tok, b_start, b_end = amounts[-1]
            balance = _parse_amount(balance_tok)
            if len(amounts) >= 2:
                amt_tok = amounts[-2][0]
                a_start = amounts[-2][1]
            else:
                amt_tok = amounts[0][0]
                a_start = amounts[0][1]
            desc = merged[:a_start].strip()
            parts = [p.strip() for p in re.split(r"\||￾|\uFFFD|\uFFFE|\/", desc) if p.strip()]
            if parts:
                desc_line1 = parts[0]
                desc_line2 = " ".join(parts[1:]) if len(parts) > 1 else ""
            else:
                p = desc.split(None, 1)
                desc_line1 = p[0] if p else ""
                desc_line2 = p[1] if len(p) > 1 else ""
            desc_upper = (desc_line1 + " " + desc_line2).upper()
            is_debit = any(k in desc_upper for k in ("WDL", "UPI/DR", "IMPS/DR", "ATM WDL", "POS/", "/DR/"))
            if is_debit:
                debit = _parse_amount(amt_tok)
            else:
                credit = _parse_amount(amt_tok)

        transactions.append({
            "date": date,
            "value_date": value_date,
            "description": (desc_line1 + ("\n" + desc_line2 if desc_line2 else "")).strip(),
            "description_line1": desc_line1,
            "description_line2": desc_line2,
            "cheque_ref": "",
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "orig_debit_text": "",
            "orig_credit_text": "",
            "orig_balance_text": balance_tok if 'balance_tok' in locals() else "",
        })

    meta["bank"] = "SBI"
    meta["layout"] = SBI_POST_VALUE
    return {"meta": meta, "transactions": transactions}


def _parse_sbi_txn_value(file_obj):
    lines = _extract_all_lines(file_obj)
    lines = _normalize_multiline_dates(lines)

    meta = {"account_name": "", "account_number": "", "period": "", "opening_balance": 0.0}
    for line in lines:
        if line.startswith(("Mrs.", "Mr.", "M/s", "Ms.")):
            meta["account_name"] = line
        if "Account No" in line:
            meta["account_number"] = line.split("Account No")[-1].strip()
        if "Statement From" in line:
            meta["period"] = line.split("Statement From")[-1].replace(":", "").strip()
        if line.upper().startswith(("BROUGHT FORWARD", "BALANCE B/F")):
            toks = re.findall(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?", line)
            if toks:
                meta["opening_balance"] = _parse_amount(toks[-1])

    date_frag = r"(?:\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{4})"
    row_start = re.compile(rf"^\s*(?P<d1>{date_frag})\s+(?P<d2>{date_frag})\s*(?P<rest>.*)$")

    transactions_raw = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = row_start.match(line)
        if m:
            d1 = m.group("d1").strip()
            d2 = m.group("d2").strip()
            rest = m.group("rest").strip()
            j = i + 1
            block = rest
            while j < len(lines):
                if row_start.match(lines[j]):
                    break
                up = lines[j].upper()
                if up.startswith("PAGE NO") or "CLOSING BALANCE" in up or "SUMMARY" in up or up.startswith("TOTAL"):
                    break
                block += " " + lines[j]
                j += 1
            transactions_raw.append({"date": d1, "value_date": d2, "text": block.strip()})
            i = j
            continue
        i += 1

    if not transactions_raw:
        permissive = re.compile(rf"^\s*{date_frag}\b", re.IGNORECASE)
        i = 0
        while i < len(lines):
            if permissive.match(lines[i]):
                toks = lines[i].split()
                if len(toks) >= 2:
                    d1 = toks[0]; d2 = toks[1]
                    rest = " ".join(toks[2:]) if len(toks) > 2 else ""
                    j = i + 1
                    while j < len(lines):
                        if permissive.match(lines[j]):
                            break
                        up = lines[j].upper()
                        if up.startswith("PAGE NO") or "CLOSING BALANCE" in up or "SUMMARY" in up:
                            break
                        rest += " " + lines[j]
                        j += 1
                    transactions_raw.append({"date": d1, "value_date": d2, "text": rest.strip()})
                    i = j
                    continue
            i += 1

    transactions = []
    amt_re = re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?(?:CR|DR)?|\d+(?:\.\d{1,2})?(?:CR|DR)?")

    for block in transactions_raw:
        date = block.get("date", "").strip()
        value_date = block.get("value_date", "").strip()
        merged = re.sub(r"\s+", " ", block.get("text", "")).strip()

        # Normalizations
        merged = re.sub(r"(?i)(UPI\s*DR|UPIDR)", "UPI/DR", merged)
        merged = re.sub(r"(?i)(UPI\s*CR|UPICR)", "UPI/CR", merged)
        merged = re.sub(r"(?i)Paym(?:e)?-?TRANSFER", "Payme TRANSFER", merged)
        merged = re.sub(r"(?i)(TRANSFER)(?=UPI/)", r"\1 ", merged)
        merged = re.sub(r"\s*/\s*", "/", merged)

        normalized = merged.replace("\uFFFE", " ").replace("\uFFFD", " ").replace("￾", " | ")
        normalized = re.sub(r"\s*\|\s*", " | ", normalized)

        amounts = [(m.group(0), m.start(), m.end()) for m in amt_re.finditer(normalized)]

        debit = credit = balance = 0.0
        balance_tok = ""
        txn_tok = ""
        desc_part = normalized

        if amounts:
            balance_tok, b_start, b_end = amounts[-1]
            balance = _parse_amount(balance_tok)
            if len(amounts) >= 2:
                txn_tok, t_start, t_end = amounts[-2]
            else:
                txn_tok, t_start, t_end = amounts[0]
            desc_part = normalized[:t_start].strip()
            try:
                before = normalized[:t_start].rstrip().rsplit(None, 1)
                if len(before) == 1:
                    prev_tok = before[0]
                    txn_int = re.match(r"^(\d+)(?:\.\d+)?", txn_tok)
                    if prev_tok.isdigit() and txn_int:
                        prev_len = len(prev_tok)
                        txn_int_part = txn_int.group(1)
                        if len(txn_int_part) >= 3 and prev_len >= 6:
                            new_prev = prev_tok + txn_int_part[0]
                            new_txn_int = txn_int_part[1:]
                            if new_txn_int:
                                idx = normalized[:t_start].rfind(prev_tok)
                                if idx != -1:
                                    desc_part = normalized[:t_start]
                                    desc_part = desc_part[:idx] + new_prev + desc_part[idx + len(prev_tok):]
                                    txn_tok = re.sub(r"^\d+", new_txn_int, txn_tok, count=1)
            except Exception:
                pass
        else:
            desc_part = normalized

        cheque_ref = ""
        desc_main = desc_part
        mref = re.search(r"(?i)\bTRANSFER(?:\s+TO|\s+FROM)\b[\s:,-]*([0-9]{4,})", desc_part)
        if mref:
            ref_acct = mref.group(1)
            typ = re.search(r"(?i)\bTRANSFER\s+(TO|FROM)\b", desc_part)
            if typ:
                cheque_ref = f"TRANSFER {typ.group(1).upper()} {ref_acct}"
            else:
                cheque_ref = f"TRANSFER {ref_acct}"
            desc_main = desc_part[:mref.start()].strip()
        else:
            m2 = re.search(r"(?i)(TRANSFER).*?([0-9]{6,})", desc_part)
            if m2:
                acct = m2.group(2)
                if re.search(r"(?i)TO", desc_part[m2.start():m2.end() + 30]):
                    cheque_ref = f"TRANSFER TO {acct}"
                elif re.search(r"(?i)FROM", desc_part[m2.start():m2.end() + 30]):
                    cheque_ref = f"TRANSFER FROM {acct}"
                else:
                    cheque_ref = f"TRANSFER {acct}"
                desc_main = desc_part[:m2.start()].strip()

        desc_main = re.sub(r"\s+", " ", desc_main).strip()
        desc_line1 = ""
        desc_line2 = ""
        if desc_main:
            segs = [s for s in re.split(r"/", desc_main) if s]
            if len(segs) >= 3:
                half = max(1, len(segs) // 3)
                line1 = "/".join(segs[:half]).strip()
                line2 = "/".join(segs[half:half * 2]).strip() if half * 2 <= len(segs) else "/".join(segs[half:]).strip()
                rest = "/".join(segs[half * 2:]).strip() if half * 2 < len(segs) else ""
                desc_line1 = line1
                desc_line2 = (line2 + ("/" + rest if rest else "")).strip()
            else:
                if len(desc_main) > 90:
                    first = desc_main[:45].rsplit(" ", 1)[0]
                    second = desc_main[len(first):].strip()
                    second_first = second[:45].rsplit(" ", 1)[0] if len(second) > 45 else second
                    desc_line1 = first.strip()
                    desc_line2 = second_first.strip()
                else:
                    if len(desc_main) > 45:
                        desc_line1 = desc_main[:45].rsplit(" ", 1)[0]
                        desc_line2 = desc_main[len(desc_line1):].strip()
                    else:
                        desc_line1 = desc_main
                        desc_line2 = ""

        orig_debit_text = orig_credit_text = ""
        if 'txn_tok' in locals() and txn_tok:
            amt_val = _parse_amount(txn_tok)
            desc_up = (desc_main or "").upper()
            is_credit = any(k in desc_up for k in ("BY TRANSFER", "/CR/", "CR/", "CREDIT", "TRANSFER FROM"))
            is_debit = any(k in desc_up for k in ("TO TRANSFER", "TRANSFER TO", "/DR/", "DR", "UPI/DR", "DEBIT", "WDL", "ATM WDL"))
            if is_credit and not is_debit:
                credit = amt_val; orig_credit_text = txn_tok
            elif is_debit and not is_credit:
                debit = amt_val; orig_debit_text = txn_tok
            else:
                if cheque_ref and "TRANSFER TO" in cheque_ref:
                    debit = amt_val; orig_debit_text = txn_tok
                elif cheque_ref and "TRANSFER FROM" in cheque_ref:
                    credit = amt_val; orig_credit_text = txn_tok
                else:
                    if "/CR" in desc_up or "BY TRANSFER" in desc_up:
                        credit = amt_val; orig_credit_text = txn_tok
                    else:
                        debit = amt_val; orig_debit_text = txn_tok

        transactions.append({
            "date": date,
            "value_date": value_date,
            "description": (desc_line1 + ("\n" + desc_line2 if desc_line2 else "")).strip(),
            "description_line1": desc_line1,
            "description_line2": desc_line2,
            "cheque_ref": cheque_ref,
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "orig_debit_text": orig_debit_text,
            "orig_credit_text": orig_credit_text,
            "orig_balance_text": balance_tok if 'balance_tok' in locals() else "",
        })

    if (meta.get("opening_balance") in (0, 0.0, None)) and transactions:
        first = transactions[0]
        ob = (first.get("balance") or 0.0) + (first.get("debit") or 0.0) - (first.get("credit") or 0.0)
        meta["opening_balance"] = ob

    meta = {"bank": "SBI", "layout": SBI_TXN_VALUE, **meta}
    return {"meta": meta, "transactions": transactions}


# Dispatcher
def parse_pdf_to_data(file_obj, layout=None):
    try:
        file_obj.seek(0)
    except Exception:
        pass
    lines = _extract_all_lines(file_obj)
    lines = _normalize_multiline_dates(lines)
    sample = " ".join(lines[:120]).upper()
    auto = None
    if "POST DATE" in sample and "VALUE DATE" in sample and "DESCRIPTION" in sample:
        auto = SBI_POST_VALUE
    elif "TXN DATE" in sample and "VALUE DATE" in sample:
        auto = SBI_TXN_VALUE
    elif "TXN DATE" in sample and "DETAILS" in sample:
        auto = SBI_DATE_DETAILS

    use_layout = (layout.strip().upper() if layout else None) or auto or SBI_POST_VALUE

    try:
        file_obj.seek(0)
    except Exception:
        pass

    if use_layout == SBI_POST_VALUE:
        return _parse_sbi_post_value(file_obj)
    if use_layout == SBI_TXN_VALUE:
        return _parse_sbi_txn_value(file_obj)
    if use_layout == SBI_DATE_DETAILS:
        return _parse_sbi_date_details(file_obj)
    raise ValueError("Unsupported layout")


# Helper: split into up to 3 visual lines
def _make_three_line_cells(text, max_lines=3, approx_chars=34):
    if not text:
        return [""] * max_lines
    s = text.replace("\u00A0", " ").strip()
    segs = [seg.strip() for seg in s.split("/") if seg.strip()]
    lines = []
    if len(segs) >= max_lines:
        per = max(1, len(segs) // max_lines)
        idx = 0
        for i in range(max_lines - 1):
            part = "/".join(segs[idx:idx + per])
            lines.append(part)
            idx += per
        lines.append("/".join(segs[idx:]))
    else:
        joined = " / ".join(segs) if segs else s
        words = joined.split()
        cur = ""
        for w in words:
            if len(cur) + 1 + len(w) <= approx_chars or not cur:
                cur = (cur + " " + w).strip()
            else:
                lines.append(cur)
                cur = w
                if len(lines) >= max_lines:
                    cur = cur + " " + " ".join(words[words.index(w) + 1:]) if words.index(w) + 1 < len(words) else cur
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        while len(lines) < max_lines:
            lines.append("")
        if len(lines) > max_lines:
            lines = lines[:max_lines]
    if len(lines) < max_lines:
        lines += [""] * (max_lines - len(lines))
    return lines[:max_lines]


# PDF generation
def generate_pdf_from_data(statement_obj):
    buffer = io.BytesIO()
    data = getattr(statement_obj, "data", {}) or {}
    meta = data.get("meta", {}) or {}
    txns = data.get("transactions", []) or []
    layout = (getattr(statement_obj, "layout", None) or meta.get("layout") or SBI_POST_VALUE)

    page_size = A4
    width, height = page_size

    # layout-specific margins and row heights
    if layout == SBI_TXN_VALUE:
        left_margin = 10 * mm
        right_margin = 10 * mm
        top_margin = 10 * mm
        bottom_margin = 12 * mm
        ROW_HEIGHT = 14.5 * mm
        HEADER_ROW_HEIGHT = max(ROW_HEIGHT - (3 * mm), 8 * mm)
    else:
        left_margin = 6 * mm
        right_margin = 9 * mm
        top_margin = 5 * mm
        bottom_margin = 8 * mm
        ROW_HEIGHT = 10.7 * mm
        HEADER_ROW_HEIGHT = ROW_HEIGHT

    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
    )

    usable_width = width - left_margin - right_margin

    # Column widths
    if layout == SBI_TXN_VALUE:
        col_widths = [
            usable_width * 0.10,  # Txn Date
            usable_width * 0.10,  # Value Date
            usable_width * 0.24,  # Description
            usable_width * 0.17,  # Ref No.
            usable_width * 0.12,  # Debit
            usable_width * 0.12,  # Credit
            usable_width * 0.13,  # Balance
        ]
    else:
        col_widths = [
            usable_width * 0.11,
            usable_width * 0.11,
            usable_width * 0.26,
            usable_width * 0.16,
            usable_width * 0.12,
            usable_width * 0.12,
            usable_width * 0.12,
        ]

    # Styles and amount alignment per layout
    if layout == SBI_TXN_VALUE:
        style_header = ParagraphStyle(name="header_txn", fontName="Helvetica-Bold", fontSize=10.5, alignment=TA_LEFT, leading=12)
    else:
        style_header = ParagraphStyle(name="header_post", fontName="Helvetica", fontSize=10.5, alignment=TA_CENTER, leading=12)

    if layout == SBI_TXN_VALUE:
        body_font = 9
        amount_alignment = TA_RIGHT
    else:
        body_font = 8
        amount_alignment = TA_CENTER

    style_center = ParagraphStyle(name="center", fontName="Helvetica", fontSize=body_font, alignment=TA_CENTER)
    style_amount = ParagraphStyle(name="amount", fontName="Helvetica", fontSize=body_font, alignment=amount_alignment)
    style_left = ParagraphStyle(name="left", fontName="Helvetica", fontSize=body_font, alignment=TA_LEFT, leading=body_font + 2)

    # compact description style for model1 (tighter leading)
    style_desc_compact = ParagraphStyle(
        name="desc_compact",
        fontName="Helvetica",
        fontSize=body_font,
        leading=max(body_font + 0.5, body_font),
        spaceBefore=0,
        spaceAfter=0,
        leftIndent=0,
        rightIndent=0,
    )

    story = []
    usable_height = height - top_margin - bottom_margin
    if layout == SBI_TXN_VALUE:
        ROWS_PER_PAGE = max(6, int((usable_height - ROW_HEIGHT * 2) / ROW_HEIGHT))
    else:
        header_height = ROW_HEIGHT
        footer_height = ROW_HEIGHT
        ROWS_PER_PAGE = max(8, int((usable_height - header_height - footer_height) / ROW_HEIGHT))

    opening_balance = float(meta.get("opening_balance") or 0)
    running_balance_global = opening_balance

    acct_name = str(meta.get("account_name") or "")
    acct_no = str(meta.get("account_number") or "")
    period = str(meta.get("period") or "")

    if acct_name or acct_no or period:
        meta_table = [
            [Paragraph(f"<b>{acct_name}</b>", style_left),
             Paragraph(f"Opening Balance: {opening_balance:,.2f}" + ("CR" if layout == SBI_POST_VALUE else ""), style_amount)],
            [Paragraph(f"Account No: {acct_no}", style_left),
             Paragraph(f"Period: {period}", style_amount)],
        ]
        mt = Table(meta_table, colWidths=[usable_width * 0.7, usable_width * 0.3])
        mt.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(mt)

    def header_date_label():
        return "Txn Date" if layout == SBI_TXN_VALUE else "Post Date"

    def header_ref_label():
        return "Ref No./Cheque No." if layout == SBI_TXN_VALUE else "Cheque<br/>No/Reference"

    for start in range(0, len(txns), ROWS_PER_PAGE):
        chunk = txns[start:start + ROWS_PER_PAGE]
        table_data = []

        # header row
        table_data.append([
            Paragraph(header_date_label(), style_header),
            Paragraph("Value Date", style_header),
            Paragraph("Description" if layout != SBI_TXN_VALUE else "Description", style_header),
            Paragraph(header_ref_label(), style_header),
            Paragraph("Debit", style_header),
            Paragraph("Credit", style_header),
            Paragraph("Balance", style_header),
        ])

        for txn in chunk:
            debit = float(txn.get("debit") or 0)
            credit = float(txn.get("credit") or 0)
            running_balance_global = running_balance_global - debit + credit

            # prepare 3 visual lines for description
            desc_full = (txn.get("description_line1") or "").strip()
            if txn.get("description_line2"):
                if desc_full:
                    desc_full = desc_full + " / " + txn.get("description_line2").strip()
                else:
                    desc_full = txn.get("description_line2").strip()
            lines3 = _make_three_line_cells(desc_full, max_lines=3, approx_chars=32)

            # Build 3-line nested table for description. Use compact paragraph style for model1 to reduce vertical gaps.
            desc_rows = [
                [Paragraph(lines3[0], style_desc_compact)],
                [Paragraph(lines3[1], style_desc_compact)],
                [Paragraph(lines3[2], style_desc_compact)]
            ]
            desc_table = Table(desc_rows, colWidths=[col_widths[2]])

            if layout == SBI_POST_VALUE:
                # model1: tighten vertical spacing between description lines and add bottom gap so content doesn't touch next row
                desc_table.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),   # left gap
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),    # reduce top padding
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),  # small bottom padding per nested row
                ]))
                # force UPI onward to second line if present
                full_text = " ".join([lines3[0], lines3[1], lines3[2]]).strip()
                m = re.search(r"\bUPI\b", full_text, flags=re.IGNORECASE)
                if m:
                    before = full_text[:m.start()].strip()
                    after = full_text[m.start():].strip()
                    lines3 = [before, after, ""]
                    desc_rows = [
                        [Paragraph(lines3[0], style_desc_compact)],
                        [Paragraph(lines3[1], style_desc_compact)],
                        [Paragraph(lines3[2], style_desc_compact)]
                    ]
                    desc_table = Table(desc_rows, colWidths=[col_widths[2]])
                    desc_table.setStyle(TableStyle([
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]))
            else:
                # model2: compact paddings
                desc_table.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]))

            ref_para = Paragraph(str(txn.get("cheque_ref") or ""), style_left if layout == SBI_TXN_VALUE else style_center)

            debit_para = Paragraph(f"{debit:,.2f}" if debit else "", style_amount)
            credit_para = Paragraph(f"{credit:,.2f}" if credit else "", style_amount)
            balance_display = f"{running_balance_global:,.2f}" + ("CR" if layout == SBI_POST_VALUE else "")
            balance_para = Paragraph(balance_display, style_amount)

            date_para = Paragraph(str(txn.get("date", "")), style_center)
            vdate_para = Paragraph(str(txn.get("value_date", "")), style_center)

            row = [
                date_para,
                vdate_para,
                desc_table,
                ref_para,
                debit_para,
                credit_para,
                balance_para,
            ]
            table_data.append(row)

        if layout == SBI_POST_VALUE:
            table_data.append([Paragraph("", style_center)] * 7)

        # Build row heights array
        if layout == SBI_TXN_VALUE:
            row_heights = [HEADER_ROW_HEIGHT] + [ROW_HEIGHT] * (len(table_data) - 1)
        else:
            row_heights = [ROW_HEIGHT] * len(table_data)

        # Build table
        t = Table(table_data, colWidths=col_widths, repeatRows=1, rowHeights=row_heights)

        last_row = len(table_data) - 1

        # Borders & styles
        if layout == SBI_TXN_VALUE:
            grid_color = colors.black
            grid_width = 0.35
        else:
            grid_color = colors.HexColor("#919090")
            grid_width = 0.20

        styles = [
            ("GRID", (0, 0), (-1, -1), grid_width, grid_color),
            ("BOX", (0, 0), (-1, -1), grid_width, grid_color),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("VALIGN", (2, 0), (2, -1), "TOP"),
            ("LEFTPADDING", (2, 0), (2, -1), 6),
        ]

        if layout == SBI_TXN_VALUE:
            styles.extend([
                ("ALIGN", (0, 0), (1, -1), "LEFT"),
                ("ALIGN", (4, 0), (6, -1), "RIGHT"),
                ("ALIGN", (3, 0), (3, -1), "LEFT"),
                ("VALIGN", (2, 0), (2, 0), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, 0), 2),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
                ("ALIGN", (4, 0), (6, 0), "RIGHT"),
            ])
        else:
            header_bg = colors.HexColor("#bfe1ff")
            styles.extend([
                ("BACKGROUND", (0, 0), (-1, 0), header_bg),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("BACKGROUND", (0, last_row), (-1, last_row), header_bg),
                ("ALIGN", (0, 0), (1, -1), "CENTER"),
                ("ALIGN", (2, 0), (2, 0), "CENTER"),
                ("VALIGN", (2, 0), (2, 0), "MIDDLE"),
                ("ALIGN", (3, 0), (3, -1), "CENTER"),
                ("ALIGN", (4, 0), (6, -1), "CENTER"),
                # description body gap is handled via nested desc_table bottom padding
            ])

        t.setStyle(TableStyle(styles))
        t.hAlign = "LEFT"
        story.append(t)

        if start + ROWS_PER_PAGE < len(txns):
            story.append(PageBreak())

    def _draw_page_number(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 10)
        x = width / 2.0
        y = 14 * mm
        canvas.drawCentredString(x, y, f"Page no. {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)

    buffer.seek(0)
    return ContentFile(buffer.read(), name=f"statement_{getattr(statement_obj, 'id', 'updated')}_updated.pdf")