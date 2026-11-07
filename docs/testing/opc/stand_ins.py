"""Stand-ins for the OPC test: a CRM (an MCP server, as the pack connects to one), the product's
monitoring API and LinkedIn, in one process on 127.0.0.1.

    uv run python docs/testing/opc/stand_ins.py [--port 8091] [--record FILE]

- ``/mcp``                 the CRM: search_contacts, get_contact (read), update_contact,
                            send_email (both need the founder's approval in the pack)
- ``/monitoring/metrics``  ``?service=web`` → p95 latency and error rate
- ``/monitoring/alerts``   the open alerts (none until you raise one)
- ``/linkedin/v2/ugcPosts``  POST {text}: "publishes" a post
- ``/control/alert``       POST {name, severity, service}: raise an alert for the CTO to find;
                            DELETE clears them

Everything that would leave the company (a post, an email, a CRM change) is written to the
record file (default: outbox.json next to this script) and printed as one line, so you see
exactly what the team did. Requests must carry a bearer token; any value works.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

CONTACTS: list[dict[str, Any]] = [
    {"id": "c-1", "name": "María Fernández", "email": "maria.fernandez@example.com",
     "company": "Clínica Dental Sonríe", "stage": "trial", "notes": "Asked for WhatsApp reminders."},
    {"id": "c-2", "name": "Jorge Ruiz", "email": "jorge.ruiz@example.com",
     "company": "Estética Bella", "stage": "lead", "notes": "Met at a fair; wants a demo."},
    {"id": "c-3", "name": "Lucía Méndez", "email": "lucia.mendez@example.com",
     "company": "Veterinaria Patitas", "stage": "customer", "notes": "Pays yearly since March."},
]  # fmt: skip


class World:
    """The stand-ins' state: contacts, alerts, and the record of what went out."""

    def __init__(self, record: Path | None = None) -> None:
        self.contacts = [dict(c) for c in CONTACTS]
        self.alerts: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.record = record

    def out(self, kind: str, **data: Any) -> dict[str, Any]:
        entry = {"kind": kind, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **data}
        self.outbox.append(entry)
        if self.record is not None:
            self.record.write_text(json.dumps(self.outbox, indent=2, ensure_ascii=False))
        print(f"OUT {kind}: {json.dumps(data, ensure_ascii=False)[:200]}", flush=True)
        return entry

    # --- CRM ---------------------------------------------------------------------------

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        q = query.lower()
        return [c for c in self.contacts
                if any(q in str(v).lower() for v in c.values())] or []  # fmt: skip

    def get_contact(self, id: str) -> dict[str, Any]:
        found = next((c for c in self.contacts if c["id"] == id), None)
        if found is None:
            raise ValueError(f"no contact {id}")
        return found

    def update_contact(self, id: str, fields: dict[str, Any]) -> dict[str, Any]:
        contact = self.get_contact(id)
        contact.update({k: v for k, v in fields.items() if k not in ("id",)})
        self.out("crm_update", id=id, fields=fields)
        return contact

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        self.out("email", to=to, subject=subject, body=body)
        return {"sent": True, "to": to}

    # --- monitoring and LinkedIn --------------------------------------------------------

    def metrics(self, service: str) -> dict[str, Any]:
        slow = any(a.get("service") in (service, None, "") for a in self.alerts)
        return {"service": service or "web", "p95_ms": 2400 if slow else 180,
                "error_rate": 0.02 if slow else 0.0, "uptime_24h": 0.999}  # fmt: skip

    def handle(self, method: str, path: str, query: dict[str, str],
               body: dict[str, Any]) -> tuple[int, Any]:  # fmt: skip
        """The plain HTTP stand-ins (everything but the CRM)."""
        if method == "GET" and path == "/monitoring/metrics":
            return 200, self.metrics(query.get("service", "web"))
        if method == "GET" and path == "/monitoring/alerts":
            return 200, {"alerts": self.alerts}
        if method == "POST" and path == "/linkedin/v2/ugcPosts":
            text = str(body.get("text") or "")
            if not text.strip():
                return 422, {"error": "a post needs text"}
            entry = self.out("linkedin_post", text=text)
            return 201, {"id": f"urn:li:share:{len(self.outbox)}", "at": entry["at"]}
        if method == "POST" and path == "/control/alert":
            alert = {"name": body.get("name", "p95 latency high"),
                     "severity": body.get("severity", "warning"),
                     "service": body.get("service", "web")}  # fmt: skip
            self.alerts.append(alert)
            print(f"ALERT raised: {alert}", flush=True)
            return 201, alert
        if method == "DELETE" and path == "/control/alert":
            self.alerts.clear()
            return 200, {"alerts": []}
        return 404, {"error": f"no {method} {path}"}


def crm_server(world: World) -> Any:
    """The CRM as an MCP server: reads are marked read-only, so the harness asks the founder
    only for the writes."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("crm")
    read = ToolAnnotations(read_only_hint=True)

    @server.tool(annotations=read)
    def search_contacts(query: str) -> list[dict[str, Any]]:
        """Find contacts whose name, email, company, stage or notes contain the text."""
        return world.search_contacts(query)

    @server.tool(annotations=read)
    def get_contact(id: str) -> dict[str, Any]:
        """One contact by id."""
        return world.get_contact(id)

    @server.tool()
    def update_contact(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Change a contact's fields (stage, notes...)."""
        return world.update_contact(id, fields)

    @server.tool()
    def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
        """Send an email to a contact from the company's address."""
        return world.send_email(to, subject, body)

    return server


def app(world: World) -> Any:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    starlette_app = crm_server(world).streamable_http_app()

    async def plain(request: Request) -> JSONResponse:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return JSONResponse({"error": "a bearer token is required"}, status_code=401)
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        status, out = world.handle(
            request.method, request.url.path, dict(request.query_params), body
        )
        return JSONResponse(out, status_code=status)

    for path in ("/monitoring/metrics", "/monitoring/alerts", "/linkedin/v2/ugcPosts",
                 "/control/alert"):  # fmt: skip
        starlette_app.add_route(path, plain, methods=["GET", "POST", "DELETE"])
    return starlette_app


if __name__ == "__main__":
    import uvicorn

    cli = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    cli.add_argument("--port", type=int, default=8091)
    cli.add_argument("--record", type=Path, default=HERE / "outbox.json")
    args = cli.parse_args()
    print(f"CRM (MCP), monitoring and LinkedIn on http://127.0.0.1:{args.port}"
          f" (record: {args.record})", flush=True)  # fmt: skip
    uvicorn.run(app(World(args.record)), host="127.0.0.1", port=args.port, log_level="warning")
