"""Stand-ins for the receipt test: a CFDI validator (the SAT check) and an accounting system.

    python3 docs/testing/receipts/mock_services.py [--port 8090] [--ledger FILE]

Both listen on 127.0.0.1 only. What the harness posts lands in the ledger file
(default: erp-ledger.json next to this script), so you can see exactly what reached the
"ERP". Every request is printed as one line (never the Authorization header).

- ``POST /cfdi/parse``     {xml}                                   the CFDI's fields
- ``POST /cfdi/validate``  {uuid, rfc_emisor, rfc_receptor, total} {valid, status}
- ``GET  /erp/entries?uuid=``                                      {found, entry}
- ``POST /erp/entries``    {invoice}                               {id, created}

The SAT answer is decided by ``SAT`` below: a UUID listed as cancelled is not valid, an
unknown UUID is "not found" (not valid), and the RFCs and total must match what was issued.
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent

# What SAT "knows": every CFDI in samples/, as issued (uuid -> emisor, receptor, total, status)
SAT: dict[str, dict[str, Any]] = {
    "6F1A2B3C-4D5E-4F60-8A71-92B3C4D5E6F7": {"emisor": "XIA190128J61", "receptor": "EKU9003173C9", "total": 4640.00, "status": "Vigente"},  # noqa: E501
    "7A2B3C4D-5E6F-4A71-9B82-A3C4D5E6F708": {"emisor": "XIA190128J61", "receptor": "EKU9003173C9", "total": 38280.00, "status": "Vigente"},  # noqa: E501
    "8B3C4D5E-6F70-4B82-AC93-B4D5E6F70819": {"emisor": "XIA190128J61", "receptor": "IIA040805DZ4", "total": 2900.00, "status": "Vigente"},  # noqa: E501
    "9C4D5E6F-7081-4C93-BDA4-C5E6F708192A": {"emisor": "CACX7605101P8", "receptor": "EKU9003173C9", "total": 1160.00, "status": "Cancelado"},  # noqa: E501
    "AD5E6F70-8192-4DA4-8EB5-D6F708192A3B": {"emisor": "CACX7605101P8", "receptor": "EKU9003173C9", "total": 13920.00, "status": "Vigente"},  # noqa: E501
}

CFDI = "http://www.sat.gob.mx/cfd/4"
TFD = "http://www.sat.gob.mx/TimbreFiscalDigital"


def parse_cfdi(xml: str) -> dict[str, Any]:
    """The fields of a CFDI 4.0 (refuses DTDs and entities, like the harness does)."""
    if re.search(r"<!(DOCTYPE|ENTITY)", xml, re.IGNORECASE):
        raise ValueError("DTDs and entities are not accepted")
    root = ET.fromstring(xml)
    emisor = root.find(f"{{{CFDI}}}Emisor")
    receptor = root.find(f"{{{CFDI}}}Receptor")
    stamp = root.find(f".//{{{TFD}}}TimbreFiscalDigital")
    items = [
        {"description": c.get("Descripcion", ""), "amount": float(c.get("Importe", 0))}
        for c in root.iter(f"{{{CFDI}}}Concepto")
    ]
    taxes = root.find(f"{{{CFDI}}}Impuestos")
    return {
        "uuid": stamp.get("UUID") if stamp is not None else "",
        "rfc_emisor": emisor.get("Rfc") if emisor is not None else "",
        "rfc_receptor": receptor.get("Rfc") if receptor is not None else "",
        "date": (root.get("Fecha") or "")[:10],
        "subtotal": float(root.get("SubTotal", 0)),
        "taxes": float(taxes.get("TotalImpuestosTrasladados", 0)) if taxes is not None else 0.0,
        "total": float(root.get("Total", 0)),
        "currency": root.get("Moneda", "MXN"),
        "line_items": items,
    }


def validate(body: dict[str, Any]) -> dict[str, Any]:
    issued = SAT.get(str(body.get("uuid", "")).upper())
    if issued is None:
        return {"valid": False, "status": "No encontrado"}
    if issued["status"] != "Vigente":
        return {"valid": False, "status": issued["status"]}
    same = (
        str(body.get("rfc_emisor", "")).upper() == issued["emisor"]
        and str(body.get("rfc_receptor", "")).upper() == issued["receptor"]
        and abs(float(body.get("total") or 0) - issued["total"]) < 0.01
    )
    return {"valid": same, "status": "Vigente" if same else "Datos no coinciden"}


def handle(
    method: str, url: str, body: dict[str, Any], ledger: list[dict[str, Any]]
) -> tuple[int, dict[str, Any]]:
    """One request; ``ledger`` is the ERP's entries (changed in place on a post)."""
    parsed = urlparse(url)
    path, query = parsed.path.rstrip("/"), parse_qs(parsed.query)
    try:
        if method == "POST" and path == "/cfdi/parse":
            return 200, parse_cfdi(str(body.get("xml", "")))
        if method == "POST" and path == "/cfdi/validate":
            return 200, validate(body)
        if method == "GET" and path == "/erp/entries":
            uuid = (query.get("uuid") or [""])[0].upper()
            entry = next((e for e in ledger if str(e.get("uuid", "")).upper() == uuid), None)
            return 200, {"found": entry is not None, "entry": entry}
        if method == "POST" and path == "/erp/entries":
            invoice = body.get("invoice")
            if not isinstance(invoice, dict) or not invoice.get("uuid"):
                return 422, {"error": "an entry needs an invoice with a uuid"}
            entry = {"id": f"E-{len(ledger) + 1:04d}", **invoice}
            ledger.append(entry)
            return 201, {"id": entry["id"], "created": True}
    except (ValueError, ET.ParseError) as exc:
        return 400, {"error": str(exc)}
    return 404, {"error": f"no {method} {path}"}


def serve(port: int, ledger_file: Path) -> None:
    ledger: list[dict[str, Any]] = (
        json.loads(ledger_file.read_text()) if ledger_file.exists() else []
    )

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                status, out = 401, {"error": "a bearer token is required"}
            else:
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                status, out = handle(method, self.path, body, ledger)
                if status == 201:
                    ledger_file.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
            data = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            print(f"{method} {self.path} -> {status} {json.dumps(out, ensure_ascii=False)[:160]}")

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def log_message(self, *args: Any) -> None:  # one line per request is printed above
            return

    print(f"CFDI validator and ERP on http://127.0.0.1:{port} (ledger: {ledger_file})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    cli.add_argument("--port", type=int, default=8090)
    cli.add_argument("--ledger", type=Path, default=HERE / "erp-ledger.json")
    args = cli.parse_args()
    try:
        serve(args.port, args.ledger)
    except KeyboardInterrupt:
        print("stopped")
