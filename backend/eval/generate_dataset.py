"""Generate a small labeled synthetic invoice dataset into eval/dataset/ (seeded, reproducible).

Every file gets an entry in labels.json:
  {"file", "kind", "invoices": [truth...], "expect_status", "expect_error_code", "expect_issues"}
Truth values are what is PRINTED in the file, so a "wrong total" case expects the wrong total
to be extracted and TOTAL_MISMATCH to be raised.

Run:  python eval/generate_dataset.py
"""
import csv
import json
import random
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import fitz  # PyMuPDF
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "dataset"
rng = random.Random(7)

SUPPLIERS = ["Northwind Traders", "Acme Industrial Supply", "Blue Harbor Logistics", "Kestrel Office Goods",
             "Summit Catering Ltd", "Orchid Print Works", "Lakeside Hardware", "Meridian IT Services",
             "Pioneer Packaging", "Granite Peak Consulting", "Verde Cleaning Co", "Atlas Freight GmbH"]
CUSTOMERS = ["Cottage Home Care", "Riverside Dental", "Oakridge School", "Bright Path Clinic", "Harbor View Cafe"]
ITEMS = [("Printer paper A4 (box)", 18), ("Toner cartridge", 64), ("Consulting hour", 90), ("Delivery fee", 25),
         ("Cleaning service", 120), ("Network cable 10m", 9), ("Office chair", 140), ("Catering platter", 55),
         ("Safety gloves (pack)", 12), ("Software licence", 210)]
CURRENCIES = ["USD", "EUR", "GBP"]
LABEL_SETS = [  # label template per layout so documents are not all identical
    dict(num="Invoice Number", date="Invoice Date", sup="Supplier", cus="Bill To", sub="Subtotal", tax="Tax", tot="Total", cur="Currency"),
    dict(num="Invoice No", date="Date", sup="From", cus="Customer", sub="Subtotal", tax="VAT", tot="Grand Total", cur="Currency"),
    dict(num="Invoice #", date="Date", sup="Vendor", cus="Client", sub="Subtotal", tax="Tax", tot="Total", cur="Currency"),
]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def money(x: Decimal) -> str:
    return f"{x:.2f}"


def make_invoice(n: int, supplier: str | None = None) -> dict:
    lines = []
    for desc, price in rng.sample(ITEMS, rng.randint(1, 4)):
        qty = rng.randint(1, 10)
        lines.append({"description": desc, "quantity": str(qty), "unit_price": money(Decimal(price)),
                      "line_total": money(Decimal(price) * qty)})
    subtotal = sum(Decimal(l["line_total"]) for l in lines)
    tax = (subtotal * Decimal("0.10")).quantize(Decimal("0.01"))
    d = date(2025, 1, 1) + timedelta(days=rng.randint(0, 300))
    return {"invoice_number": f"INV-{2025}{n:04d}", "invoice_date": d.isoformat(),
            "supplier_name": supplier or rng.choice(SUPPLIERS), "customer_name": rng.choice(CUSTOMERS),
            "subtotal": money(subtotal), "tax": money(tax), "total": money(subtotal + tax),
            "currency": rng.choice(CURRENCIES), "line_items": lines}


def text_lines(inv: dict, style: int) -> list[str]:
    L = LABEL_SETS[style % len(LABEL_SETS)]
    d = date.fromisoformat(inv["invoice_date"])
    shown = d.isoformat() if style % 2 == 0 else f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    out = ["INVOICE", "",
           f"{L['num']}: {inv['invoice_number']}" if inv["invoice_number"] else "",
           f"{L['date']}: {shown}", f"{L['sup']}: {inv['supplier_name']}", f"{L['cus']}: {inv['customer_name']}", "",
           "Description            Qty    Unit price    Amount"]
    for l in inv["line_items"]:
        out.append(f"{l['description']:<24}{l['quantity']:>3}  {l['unit_price']:>10}  {l['line_total']:>10}")
    out.append("")
    out.append(f"{L['sub']}: {inv['subtotal']}")
    out.append(f"{L['tax']}: {inv['tax']}")
    if inv["total"] is not None:
        out.append(f"{L['tot']}: {inv['total']}")
    out.append(f"{L['cur']}: {inv['currency']}")
    return [x for x in out if x is not None]


def digital_pdf(path: Path, invoices: list[dict], style: int, filler_pages: int = 0) -> None:
    doc = fitz.open()
    for inv in invoices:
        page = doc.new_page()
        y = 60
        for line in text_lines(inv, style):
            page.insert_text((50, y), line, fontsize=11, fontname="cour")
            y += 18
    for i in range(filler_pages):
        page = doc.new_page()
        for j in range(30):
            page.insert_text((50, 60 + j * 22), f"Terms and conditions clause {i}.{j}: payment is due within 30 days.",
                             fontsize=10)
    doc.save(str(path))


def render_image(inv: dict, style: int, degrade: bool) -> Image.Image:
    img = Image.new("L", (1240, 1000), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    y = 40
    for line in text_lines(inv, style):
        draw.text((60, y), line, fill=0, font=font)
        y += 38
    if degrade:
        img = img.rotate(rng.uniform(-2.0, 2.0), fillcolor=255, expand=False)
        img = img.filter(ImageFilter.GaussianBlur(1.1))
        px = img.load()
        for _ in range(25000):
            x, yy = rng.randrange(img.width), rng.randrange(img.height)
            px[x, yy] = rng.randint(0, 255)
    return img


def scanned_pdf(path: Path, inv: dict, style: int) -> None:
    tmp = path.with_suffix(".png")
    render_image(inv, style, degrade=True).save(tmp)
    doc = fitz.open()
    page = doc.new_page(width=595, height=480)
    page.insert_image(page.rect, filename=str(tmp))
    doc.save(str(path))
    tmp.unlink()


def sheet_rows(invoices: list[dict], headers: dict[str, str]) -> tuple[list[str], list[list]]:
    keys = list(headers)
    return [headers[k] for k in keys], [[inv[k] for k in keys] for inv in invoices]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    labels: list[dict] = []
    n = [0]

    def nxt(supplier=None) -> dict:
        n[0] += 1
        return make_invoice(n[0], supplier)

    def add(file: str, kind: str, invoices: list[dict], expect_status="completed", expect_error=None, issues=()):
        truth = [{k: v for k, v in inv.items() if k != "line_items"} | {"line_count": len(inv["line_items"])}
                 for inv in invoices]
        labels.append({"file": file, "kind": kind, "invoices": truth, "expect_status": expect_status,
                       "expect_error_code": expect_error, "expect_issues": list(issues)})

    for i in range(10):                                           # digital PDFs
        inv = nxt()
        digital_pdf(OUT / f"digital_{i:02d}.pdf", [inv], i)
        add(f"digital_{i:02d}.pdf", "digital_pdf", [inv])
    for i in range(6):                                            # degraded scanned PDFs (OCR)
        inv = nxt()
        scanned_pdf(OUT / f"scan_{i:02d}.pdf", inv, i)
        add(f"scan_{i:02d}.pdf", "scanned_pdf", [inv])
    for i in range(3):                                            # clean PNG
        inv = nxt()
        render_image(inv, i, False).save(OUT / f"image_{i:02d}.png")
        add(f"image_{i:02d}.png", "image", [inv])
    for i in range(3):                                            # degraded JPEG
        inv = nxt()
        render_image(inv, i, True).save(OUT / f"photo_{i:02d}.jpg", quality=70)
        add(f"photo_{i:02d}.jpg", "image_degraded", [inv])

    # spreadsheets: one row per invoice
    rows = [nxt() for _ in range(5)]
    h, data = sheet_rows(rows, {"invoice_number": "Invoice Number", "invoice_date": "Invoice Date",
                                "supplier_name": "Supplier", "customer_name": "Customer", "subtotal": "Subtotal",
                                "tax": "Tax", "total": "Total", "currency": "Currency"})
    with open(OUT / "invoices.csv", "w", newline="") as f:
        csv.writer(f).writerows([h, *data])
    add("invoices.csv", "csv", rows)
    for name, hdr in (("invoices_a.xlsx", {"invoice_number": "Inv No", "invoice_date": "Date",
                                           "supplier_name": "Vendor", "customer_name": "Client",
                                           "subtotal": "Net", "tax": "VAT", "total": "Grand Total",
                                           "currency": "Currency"}),
                      ("invoices_b.xlsx", {"invoice_number": "Invoice #", "invoice_date": "Issue Date",
                                          "supplier_name": "Billed By", "customer_name": "Bill To",
                                          "subtotal": "Subtotal", "tax": "Tax Amount", "total": "Total Amount",
                                          "currency": "Currency"})):
        rows = [nxt() for _ in range(5)]
        h, data = sheet_rows(rows, hdr)
        wb = Workbook()
        ws = wb.active
        ws.append(h)
        for r in data:
            ws.append([float(v) if k in ("subtotal", "tax", "total") else v for k, v in zip(hdr, r)])
        wb.save(OUT / name)
        add(name, "xlsx", rows)

    # problem cases
    inv = nxt()                                                   # printed total disagrees with the sum
    inv["total"] = money(Decimal(inv["total"]) + Decimal("37.50"))
    digital_pdf(OUT / "problem_wrong_total.pdf", [inv], 0)
    add("problem_wrong_total.pdf", "problem", [inv], issues=["TOTAL_MISMATCH"])

    inv = nxt()                                                   # total missing
    inv["total"] = None
    digital_pdf(OUT / "problem_missing_total.pdf", [inv], 1)
    add("problem_missing_total.pdf", "problem", [inv], issues=["MISSING_FIELD"])

    inv = nxt()                                                   # tax absurdly high
    inv["tax"] = money(Decimal(inv["subtotal"]) * Decimal("0.6"))
    inv["total"] = money(Decimal(inv["subtotal"]) + Decimal(inv["tax"]))
    digital_pdf(OUT / "problem_high_tax.pdf", [inv], 2)
    add("problem_high_tax.pdf", "problem", [inv], issues=["TAX_ANOMALY"])

    inv = nxt()                                                   # same invoice re-sent in a different layout
    digital_pdf(OUT / "problem_dup_original.pdf", [inv], 0)
    add("problem_dup_original.pdf", "problem", [inv])
    digital_pdf(OUT / "problem_dup_resend.pdf", [inv], 2)
    add("problem_dup_resend.pdf", "problem", [inv], issues=["DUPLICATE_INVOICE"])

    pair = [nxt(), nxt()]                                         # two invoices in one PDF
    digital_pdf(OUT / "multi_two_invoices.pdf", pair, 1)
    add("multi_two_invoices.pdf", "multi_invoice", pair)

    blank = fitz.open()                                           # blank page: nothing to extract
    blank.new_page()
    blank.save(str(OUT / "problem_blank.pdf"))
    add("problem_blank.pdf", "problem", [], expect_status="failed", expect_error="NO_TEXT_FOUND")

    (OUT / "problem_corrupt.pdf").write_bytes(b"%PDF-1.4\nthis is not a real pdf" + bytes(rng.randrange(256) for _ in range(300)))
    add("problem_corrupt.pdf", "problem", [], expect_status="failed", expect_error="CORRUPT_FILE")

    inv = nxt()                                                   # one invoice buried in a 60-page PDF
    digital_pdf(OUT / "large_60_pages.pdf", [inv], 0, filler_pages=59)
    add("large_60_pages.pdf", "large", [inv])

    (OUT / "labels.json").write_text(json.dumps(labels, indent=1))
    print(f"wrote {len(labels)} files, {sum(len(l['invoices']) for l in labels)} invoices to {OUT}")


if __name__ == "__main__":
    main()
