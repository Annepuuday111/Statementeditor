# import io
# import re
# from reportlab.lib.pagesizes import A4
# from reportlab.lib import colors
# from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, PageBreak
# from reportlab.lib.units import mm
# from reportlab.lib.styles import ParagraphStyle
# from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
# from PyPDF2 import PdfReader
# from django.core.files.base import ContentFile


# def _parse_amount(text: str) -> float:
#     if not text:
#         return 0.0
#     text = text.replace("CR", "").replace("DR", "").strip()
#     text = text.replace(",", "")
#     try:
#         return float(text)
#     except ValueError:
#         return 0.0


# def _clean_description_from_block(text: str):
#     text = re.sub(r"\s+", " ", text).strip()
#     line1 = ""
#     line2 = ""
#     upper = text.upper()
#     if upper.startswith("TO TRANSFER"):
#         line1 = "TO TRANSFER"
#         line2 = text[len("TO TRANSFER"):].strip()
#     elif upper.startswith("BY TRANSFER"):
#         line1 = "BY TRANSFER"
#         line2 = text[len("BY TRANSFER"):].strip()
#     elif upper.startswith("ATM WDL"):
#         line1 = "ATM WDL"
#         line2 = text[len("ATM WDL"):].strip()
#     else:
#         parts = text.split(" ", 1)
#         line1 = parts[0]
#         line2 = parts[1] if len(parts) > 1 else ""
#     return line1, line2, -1


# def parse_pdf_to_data(file_obj):
#     reader = PdfReader(file_obj)

#     all_lines = []
#     for page in reader.pages:
#         text = page.extract_text() or ""
#         for line in text.splitlines():
#             line = line.strip()
#             if line:
#                 all_lines.append(line)

#     meta = {
#         "account_name": "",
#         "account_number": "",
#         "period": "",
#         "opening_balance": 0.0,
#         "bank": "SBI",
#         "layout": "SBI_TXN_VALUE",
#     }

#     for line in all_lines:
#         if line.startswith(("Mrs.", "Mr.", "M/s", "Ms.")):
#             meta["account_name"] = line
#         if "Account No" in line:
#             meta["account_number"] = line.split("Account No")[-1].strip()
#         if "Statement From :" in line:
#             meta["period"] = line.replace("Statement From :", "").strip()
#         if line.upper().startswith(("BROUGHT FORWARD", "BALANCE B/F")):
#             parts = line.split()
#             for part in reversed(parts):
#                 if re.match(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?", part):
#                     meta["opening_balance"] = _parse_amount(part)
#                     break

#     transactions_raw = []
#     current = None
#     in_table = False

#     date_pattern = r"(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})"
#     row_start_pattern = re.compile(
#         rf"^(?P<d1>{date_pattern})\s+(?P<d2>{date_pattern})\s+(?P<rest>.+)$"
#     )

#     for line in all_lines:
#         upper = line.upper()

#         if not in_table and "TXN DATE" in upper and "VALUE DATE" in upper:
#             in_table = True
#             continue

#         if not in_table:
#             continue

#         if any(stop in upper for stop in ["CLOSING BALANCE", "CLOSING BAL.", "TOTAL", "SUMMARY", "PAGE NO"]):
#             if current:
#                 transactions_raw.append(current)
#                 current = None
#             break

#         m = row_start_pattern.match(line)
#         if m:
#             if current:
#                 transactions_raw.append(current)
#             current = {
#                 "date": m.group("d1"),
#                 "value_date": m.group("d2"),
#                 "lines": [m.group("rest").strip()],
#             }
#         else:
#             if current:
#                 current["lines"].append(line)

#     if current:
#         transactions_raw.append(current)

#     transactions = []
#     amount_pattern = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}(?:CR|DR)?")

#     for block in transactions_raw:
#         date = block["date"]
#         value_date = block["value_date"]
#         body = " ".join(block["lines"])
#         body = re.sub(r"\s+", " ", body).strip()

#         amounts_with_pos = [(mm.group(0), mm.start(), mm.end()) for mm in amount_pattern.finditer(body)]
#         debit = 0.0
#         credit = 0.0
#         balance = 0.0
#         balance_tok = ""

#         if amounts_with_pos:
#             balance_tok, b_start, b_end = amounts_with_pos[-1]
#             balance = _parse_amount(balance_tok)
#             if len(amounts_with_pos) >= 2:
#                 amount_tok = amounts_with_pos[-2][0]
#                 first_amt_start = amounts_with_pos[0][1]
#             else:
#                 amount_tok = amounts_with_pos[0][0]
#                 first_amt_start = amounts_with_pos[0][1]

#             upper_body = body.upper()
#             is_debit = (
#                 "UPI/DR" in upper_body
#                 or " DR/" in upper_body
#                 or upper_body.endswith("DR")
#                 or "/DR " in upper_body
#                 or "TO TRANSFER" in upper_body
#                 or "ATM WDL" in upper_body
#             )
#             if amount_tok.endswith("DR"):
#                 is_debit = True
#             if amount_tok.endswith("CR"):
#                 is_debit = False

#             amt_val = _parse_amount(amount_tok)
#             if is_debit:
#                 debit = amt_val
#             else:
#                 credit = amt_val
#         else:
#             first_amt_start = len(body)

#         desc_ref_part = body[:first_amt_start].strip()
#         tokens = desc_ref_part.split()
#         ref_tokens = []
#         while tokens and re.match(r"^[A-Z0-9/-]{6,}$", tokens[-1]):
#             ref_tokens.insert(0, tokens.pop())
#         cheque_ref = " ".join(ref_tokens)
#         desc_text = " ".join(tokens)

#         line1, line2, _ = _clean_description_from_block(desc_text)
#         description = f"{line1}\n{line2}" if line1 and line2 else (line1 or line2)

#         transactions.append({
#             "date": date,
#             "value_date": value_date,
#             "description": description,
#             "description_line1": line1 or description,
#             "description_line2": line2,
#             "cheque_ref": cheque_ref,
#             "debit": debit,
#             "credit": credit,
#             "balance": balance,
#             "orig_debit_text": "",
#             "orig_credit_text": "",
#             "orig_balance_text": balance_tok,
#         })

#     if (meta.get("opening_balance") in (0, 0.0, None)) and transactions:
#         first = transactions[0]
#         ob = (first.get("balance") or 0.0) + (first.get("debit") or 0.0) - (first.get("credit") or 0.0)
#         meta["opening_balance"] = ob

#     return {
#         "meta": meta,
#         "transactions": transactions,
#     }


# def generate_pdf_from_data(statement_obj):
#     buffer = io.BytesIO()

#     data = statement_obj.data or {}
#     meta = data.get("meta", {})
#     txns = data.get("transactions", [])

#     width, height = A4
#     left_margin = 4 * mm
#     right_margin = 9 * mm
#     top_margin = 5 * mm
#     bottom_margin = 8 * mm

#     doc = SimpleDocTemplate(
#         buffer,
#         pagesize=A4,
#         leftMargin=left_margin,
#         rightMargin=right_margin,
#         topMargin=top_margin,
#         bottomMargin=bottom_margin,
#     )

#     usable_width = width - left_margin - right_margin

#     col_widths = [
#         usable_width * 0.11,
#         usable_width * 0.11,
#         usable_width * 0.26,
#         usable_width * 0.16,
#         usable_width * 0.12,
#         usable_width * 0.12,
#         usable_width * 0.12,
#     ]

#     style_header = ParagraphStyle(
#         name='header',
#         fontName='Helvetica',
#         fontSize=10,
#         alignment=TA_CENTER,
#     )

#     style_center = ParagraphStyle(
#         name='center',
#         fontName='Helvetica',
#         fontSize=8,
#         alignment=TA_CENTER,
#     )

#     style_number = ParagraphStyle(
#         name='number',
#         fontName='Helvetica',
#         fontSize=8,
#         alignment=TA_RIGHT,
#     )

#     style_left = ParagraphStyle(
#         name='left',
#         fontName='Helvetica',
#         fontSize=8,
#         alignment=TA_LEFT,
#     )

#     story = []
#     ROW_HEIGHT = 10.7 * mm

#     usable_height = height - top_margin - bottom_margin
#     header_height = ROW_HEIGHT
#     footer_height = ROW_HEIGHT
#     ROWS_PER_PAGE = max(8, int((usable_height - header_height - footer_height) / ROW_HEIGHT))

#     opening_balance = float(meta.get("opening_balance") or 0)
#     running_balance_global = opening_balance

#     acct_name = str(meta.get('account_name') or '')
#     acct_no = str(meta.get('account_number') or '')
#     period = str(meta.get('period') or '')

#     if acct_name or acct_no or period:
#         meta_table = [
#             [Paragraph(f"<b>{acct_name}</b>", style_left),
#              Paragraph(f"Opening Balance: {opening_balance:,.2f}", style_number)],
#             [Paragraph(f"Account No: {acct_no}", style_left),
#              Paragraph(f"Period: {period}", style_number)],
#         ]
#         mt = Table(meta_table, colWidths=[usable_width * 0.7, usable_width * 0.3])
#         mt.setStyle(TableStyle([
#             ('VALIGN', (0, 0), (-1, -1), 'TOP'),
#             ('LEFTPADDING', (0, 0), (-1, -1), 1.5),
#             ('RIGHTPADDING', (0, 0), (-1, -1), 0),
#             ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
#         ]))
#         story.append(mt)

#     for start in range(0, len(txns), ROWS_PER_PAGE):
#         chunk = txns[start:start + ROWS_PER_PAGE]

#         table_data = []

#         table_data.append([
#             Paragraph("Txn Date", style_header),
#             Paragraph("Value Date", style_header),
#             Paragraph("Details", style_header),
#             Paragraph("Ref No./Cheque No.", style_header),
#             Paragraph("Debit", style_header),
#             Paragraph("Credit", style_header),
#             Paragraph("Balance", style_header),
#         ])

#         for txn in chunk:
#             debit = float(txn.get("debit") or 0)
#             credit = float(txn.get("credit") or 0)
#             running_balance_global = running_balance_global - debit + credit

#             details = txn.get('description_line1') or txn.get('description') or ''
#             desc_para = Paragraph(details, style_left)

#             row = [
#                 Paragraph(str(txn.get("date", "")), style_center),
#                 Paragraph(str(txn.get("value_date", "")), style_center),
#                 desc_para,
#                 Paragraph(str(txn.get("cheque_ref", "")), style_center),
#                 Paragraph(f"{debit:,.2f}" if debit else "", style_center),
#                 Paragraph(f"{credit:,.2f}" if credit else "", style_center),
#                 Paragraph(f"{running_balance_global:,.2f}", style_center),
#             ]
#             table_data.append(row)

#         footer_row = [
#             Paragraph("", style_center),
#             Paragraph("", style_center),
#             Paragraph("", style_center),
#             Paragraph("", style_center),
#             Paragraph("", style_center),
#             Paragraph("", style_center),
#             Paragraph("", style_center),
#         ]
#         table_data.append(footer_row)

#         t = Table(table_data, colWidths=col_widths, repeatRows=1, rowHeights=ROW_HEIGHT)

#         last_row_idx = len(table_data) - 1

#         t.setStyle(TableStyle([
#             ('GRID', (0, 0), (-1, -1), 0.30, colors.HexColor('#B0B0B0')),
#             ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#bfe1ff')),
#             ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
#             ('BACKGROUND', (0, last_row_idx), (-1, last_row_idx), colors.HexColor('#bfe1ff')),
#             ('TEXTCOLOR', (0, last_row_idx), (-1, last_row_idx), colors.black),
#             ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
#             ('LEFTPADDING', (0, 0), (-1, -1), 3),
#             ('RIGHTPADDING', (0, 0), (-1, -1), 3),
#             ('TOPPADDING', (0, 0), (-1, -1), 2),
#             ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
#             ('LEFTPADDING', (2, 0), (2, -1), 6),
#             ('ROWHEIGHT', (0, 0), (-1, -1), ROW_HEIGHT),
#         ]))

#         t.hAlign = 'LEFT'
#         story.append(t)

#         if start + ROWS_PER_PAGE < len(txns):
#             story.append(PageBreak())

#     def _draw_page_number(canvas, doc_obj):
#         canvas.saveState()
#         canvas.setFont('Helvetica', 10)
#         x = width / 2.0
#         y = 14 * mm
#         canvas.drawCentredString(x, y, f"Page no. {doc_obj.page}")
#         canvas.restoreState()

#     doc.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)

#     buffer.seek(0)
#     return ContentFile(buffer.read(), name=f"statement_{statement_obj.id}_updated.pdf")
