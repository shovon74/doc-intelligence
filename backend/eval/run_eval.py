"""Upload the generated dataset through the real API, wait for processing, score against labels.json.

Run (api + worker must be up, dataset generated):
    python eval/generate_dataset.py
    python eval/run_eval.py [--provider gemini] [--api http://api:8000] [--timeout 1500]
Writes eval/results.json; docs/EVALUATION.md is built from it by eval/make_report.py.
"""
import argparse
import json
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

HERE = Path(__file__).parent
DATASET = HERE / "dataset"
FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
MONEY = {"subtotal", "tax", "total"}
TERMINAL = {"completed", "needs_review", "failed"}


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower()) if s is not None else ""


def same(field: str, want, got) -> bool:
    if want is None or got is None:
        return want is None and got is None
    if field in MONEY:
        try:
            return abs(Decimal(str(want)) - Decimal(str(got))) < Decimal("0.005")
        except InvalidOperation:
            return False
    if field == "invoice_date":
        return str(want) == str(got)[:10]
    return norm(want) == norm(got)


def seconds(doc: dict) -> float | None:
    try:
        a = datetime.fromisoformat(doc["created_at"])
        b = datetime.fromisoformat(doc["completed_at"])
        return round((b - a).total_seconds(), 1)
    except (TypeError, ValueError, KeyError):
        return None


def score_invoices(label: dict, got: list[dict]) -> dict:
    """Match expected to extracted invoices by invoice number; compare field by field."""
    by_num = {norm(g["invoice_number"]): g for g in got if g.get("invoice_number")}
    fields = {f: [0, 0] for f in FIELDS}                      # [correct, expected]
    found = exact = lines_ok = lines_n = 0
    misses = []
    for want in label["invoices"]:
        g = by_num.get(norm(want["invoice_number"]))
        if g is None:
            for f in FIELDS:
                fields[f][1] += 1
            misses.append([want["invoice_number"], "(invoice not found)", None, None])
            continue
        found += 1
        ok = [same(f, want[f], g.get(f)) for f in FIELDS]
        for f, k in zip(FIELDS, ok):
            fields[f][0] += k
            fields[f][1] += 1
            if not k:
                misses.append([want["invoice_number"], f, want[f], g.get(f)])
        exact += all(ok)
        if label["kind"] not in ("csv", "xlsx"):
            lines_n += 1
            lines_ok += len(g.get("line_items") or []) == want["line_count"]
    return {"expected": len(label["invoices"]), "found": found, "exact": exact, "extracted": len(got),
            "fields": fields, "misses": misses, "lines_ok": lines_ok, "lines_n": lines_n}


def retry(call, *a, **k):
    """The dev API server may reload and drop a connection; try again a few times."""
    for attempt in range(6):
        try:
            return call(*a, **k)
        except httpx.TransportError:
            time.sleep(2 * (attempt + 1))
    return call(*a, **k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://api:8000")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--timeout", type=int, default=1500)
    args = ap.parse_args()
    api = args.api.rstrip("/") + "/api/v1"
    labels = json.loads((DATASET / "labels.json").read_text())

    http = httpx.Client(timeout=120)
    email, pw = f"eval-{uuid.uuid4().hex[:8]}@example.com", "eval-password-1"
    http.post(f"{api}/auth/register", json={"email": email, "password": pw}).raise_for_status()
    token = http.post(f"{api}/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    http.headers["Authorization"] = f"Bearer {token}"

    ids: dict[str, str] = {}
    rejected: dict[str, str] = {}

    def upload(batch: list[dict]) -> None:
        for lab in batch:
            r = http.post(f"{api}/documents", data={"llm_provider": args.provider},
                          files={"files": (lab["file"], (DATASET / lab["file"]).read_bytes())})
            body = r.json()
            if r.status_code == 202 and body["documents"]:
                ids[lab["file"]] = body["documents"][0]["id"]
            else:
                rejected[lab["file"]] = json.dumps(body)[:200]

    def wait(files: list[str]) -> dict[str, dict]:
        deadline, docs = time.time() + args.timeout, {}
        while time.time() < deadline:
            docs = {f: retry(http.get, f"{api}/documents/{ids[f]}").json() for f in files if f in ids}
            if all(d["status"] in TERMINAL for d in docs.values()):
                return docs
            time.sleep(3)
        return docs

    started = time.time()
    first = [l for l in labels if "resend" not in l["file"]]
    upload(first)
    docs = wait([l["file"] for l in first])
    later = [l for l in labels if "resend" in l["file"]]      # duplicate must arrive after its original
    upload(later)
    docs |= wait([l["file"] for l in later])
    wall = round(time.time() - started, 1)

    rows = []
    for lab in labels:
        f = lab["file"]
        row = {"file": f, "kind": lab["kind"], "expect_status": lab["expect_status"],
               "expect_error_code": lab["expect_error_code"], "expect_issues": lab["expect_issues"]}
        if f in rejected:
            rows.append(row | {"status": "rejected", "detail": rejected[f]})
            continue
        doc = docs[f]
        row |= {"status": doc["status"], "error_code": doc["error_code"], "seconds": seconds(doc),
                "pages": doc["page_count"]}
        if doc["status"] in ("completed", "needs_review"):
            res = retry(http.get, f"{api}/documents/{ids[f]}/result", params={"include_text": "true"}).json()
            inv = res["invoices"]
            row["scores"] = score_invoices(lab, inv)
            row["issue_codes"] = sorted({i["code"] for v in inv for i in v["issues"]})
            row["tokens_in"] = sum(v.get("tokens_in") or 0 for v in inv)
            row["tokens_out"] = sum(v.get("tokens_out") or 0 for v in inv)
            row["llm_latency_ms"] = sum(v.get("latency_ms") or 0 for v in inv)
            row["models"] = sorted({f"{v['provider']}/{v['model']}" for v in inv if v.get("provider")})
            row["needs_review"] = any(v["needs_review"] for v in inv)
            row["ocr_pages"] = sum(p["source_type"] == "ocr" for p in res["pages"] or [])
        rows.append(row)

    out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "provider": args.provider,
           "files": len(labels), "wall_clock_seconds": wall, "rows": rows}
    (HERE / "results.json").write_text(json.dumps(out, indent=1))
    done = sum(r["status"] in TERMINAL for r in rows)
    print(f"{done}/{len(rows)} documents finished in {wall}s -> eval/results.json")


if __name__ == "__main__":
    main()
