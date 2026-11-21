"""Build the ledger used for the Ledger Check demo (eval/demo/ledger_demo.xlsx and .csv).

The ledger holds digital_01.pdf and image_01.png (exact copies of what is printed on them) plus
other invoices from the sample dataset, and deliberately leaves out digital_00.pdf and
image_00.png. In the demo, checking digital_01 / image_01 against it pairs them; checking
digital_00 / image_00 reports them as missing from the ledger.

Needs the sample dataset first:  python eval/generate_dataset.py
Run:                             python eval/make_demo_ledger.py
"""
import csv
import json
from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent
OUT = HERE / "demo"

MUST_BE_IN = ["digital_01.pdf", "image_01.png"]
MUST_BE_OUT = ["digital_00.pdf", "image_00.png"]
OTHERS = ["digital_02.pdf", "digital_03.pdf", "digital_04.pdf", "digital_05.pdf",
          "digital_06.pdf", "digital_07.pdf"]

# CRM-style headers; the sheet mapper recognises them without any configuration
COLUMNS = [("invoice_number", "Inv No"), ("invoice_date", "Date"), ("supplier_name", "Vendor"),
           ("customer_name", "Client"), ("subtotal", "Net"), ("tax", "VAT"),
           ("total", "Grand Total"), ("currency", "Currency")]
MONEY = ("subtotal", "tax", "total")


def main() -> None:
    labels = {e["file"]: e for e in json.loads((HERE / "dataset" / "labels.json").read_text())}
    rows = []
    for name in OTHERS[:3] + MUST_BE_IN[:1] + OTHERS[3:5] + MUST_BE_IN[1:] + OTHERS[5:]:
        rows.extend(labels[name]["invoices"])

    absent = {i["invoice_number"] for n in MUST_BE_OUT for i in labels[n]["invoices"]}
    assert not absent & {r["invoice_number"] for r in rows}, "a must-be-absent invoice leaked in"

    OUT.mkdir(exist_ok=True)
    header = [h for _, h in COLUMNS]
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws.append(header)
    for r in rows:
        ws.append([float(r[k]) if k in MONEY else r[k] for k, _ in COLUMNS])
    wb.save(OUT / "ledger_demo.xlsx")
    with open(OUT / "ledger_demo.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows([[r[k] for k, _ in COLUMNS] for r in rows])
    print(f"wrote {len(rows)} invoices to {OUT}")
    for r in rows:
        print(" ", r["invoice_number"], r["supplier_name"], r["total"], r["currency"])


if __name__ == "__main__":
    main()
