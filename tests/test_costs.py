"""Cost reports and quality metrics (M4.1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import main
from dif_general_harness.core.messages import Message, Usage
from dif_general_harness.core.scope import Scope
from dif_general_harness.observability import UsageStore
from dif_general_harness.policy.budgets import DEFAULT_PRICES, cost_usd
from dif_general_harness.providers.base import ProviderMessage
from tests.support import ADMIN_H, Env, calls, whatsapp

OPUS = "claude-opus-5-5"


def priced(message: Message, input_tokens: int, output_tokens: int, model: str = OPUS) -> Any:
    return ProviderMessage(
        message=message,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="tool_use" if message.tool_uses() else "end_turn",
        model=model,
    )


def _usd(i: int, o: int) -> float:
    return cost_usd(Usage(input_tokens=i, output_tokens=o), DEFAULT_PRICES[OPUS])


async def test_usage_is_attributed_by_vendor_role_agent_and_model(db: Any, scope: Scope) -> None:
    store = UsageStore(db)
    add = store.add
    await add(
        scope,
        agent="front",
        role="main",
        vendor="anthropic",
        model=OPUS,
        usage=Usage(input_tokens=1000, output_tokens=100, cost_usd=0.006),
        day="2026-09-01",
    )
    await add(
        scope,
        agent="front",
        role="main",
        vendor="anthropic",
        model=OPUS,
        usage=Usage(input_tokens=500, output_tokens=50, cost_usd=0.003),
        day="2026-09-01",
    )
    await add(
        scope,
        agent="verifier",
        role="verifier",
        vendor="openai-compatible",
        model="local-7b",
        usage=Usage(input_tokens=200),
        day="2026-09-02",
    )
    other = Scope(tenant_id="beta", instance_id="beta-desk")
    await add(
        other,
        agent="front",
        role="main",
        vendor="anthropic",
        model=OPUS,
        usage=Usage(input_tokens=9, cost_usd=5.0),
        day="2026-09-01",
    )

    report = await store.report(scope, since="2026-09-01", until="2026-09-30", by=["vendor"])
    assert report["rows"] == [
        {
            "vendor": "anthropic",
            "calls": 2,
            "input_tokens": 1500,
            "output_tokens": 150,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "usd": 0.009,
        },
        {
            "vendor": "openai-compatible",
            "calls": 1,
            "input_tokens": 200,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "usd": 0.0,
        },
    ]
    assert report["total"]["usd"] == 0.009  # the other tenant's $5 is not here
    assert report["unpriced"] == ["local-7b"]  # never silently free
    by_day = await store.report(scope, since="2026-09-01", until="2026-09-30", by=["day", "role"])
    assert [(r["day"], r["role"]) for r in by_day["rows"]] == [
        ("2026-09-01", "main"),
        ("2026-09-02", "verifier"),
    ]
    with pytest.raises(ValueError, match="cannot group by"):
        await store.report(scope, by=["contact"])


async def test_runs_are_charged_once_and_reported(tmp_path: Path) -> None:
    script = [
        priced(calls(("n1", "notes.list", {})), 1200, 80),
        priced(Message.assistant("No note."), 1500, 40),
        priced(calls(("h1", "handoff.human", {"reason": "wants a refund"})), 900, 30),
        priced(Message.assistant("A person will reply."), 1000, 20),
    ]
    env = Env(tmp_path, script)
    inst, headless, client = await env.open()
    async with inst, client:
        for text, sid, sender in (
            ("¿nota?", "SM1", "+5215511111111"),
            ("reembolso", "SM2", "+5215522222222"),
        ):
            body, headers = whatsapp(text, sid, sender=sender)
            await client.post("/channels/whatsapp", content=body, headers=headers)
            await headless.worker().drain()

        expected = round(_usd(1200, 80) + _usd(1500, 40) + _usd(900, 30) + _usd(1000, 20), 6)
        r = await client.get("/admin/costs?by=agent,role,vendor,model", headers=ADMIN_H)
        [row] = r.json()["rows"]
        assert (row["agent"], row["role"], row["vendor"], row["model"]) == (
            "front",
            "main",
            "anthropic",
            OPUS,
        )
        assert row["calls"] == 4 and row["usd"] == expected
        spent = await inst.spend.day(inst.scope)
        assert round(spent["tenant"], 6) == expected == round(spent["vendor:anthropic"], 6)
        bad = await client.get("/admin/costs?by=contact", headers=ADMIN_H)
        assert bad.status_code == 400

        m = (await client.get("/admin/metrics?since=30d", headers=ADMIN_H)).json()
        assert m["tool_calls"] == {"ok": 2} and m["tool_success_rate"] == 1.0
        assert (m["conversations"], m["escalations"], m["resolved"]) == (2, 1, 1)
        assert m["escalation_rate"] == 0.5 and m["cost_per_resolved_usd"] == expected


def test_costs_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def one_turn() -> None:
        env = Env(tmp_path, [priced(Message.assistant("Hola"), 1000, 10)])
        inst, headless, client = await env.open()
        async with inst, client:
            body, headers = whatsapp("hola", "SM1")
            await client.post("/channels/whatsapp", content=body, headers=headers)
            await headless.worker().drain()

    asyncio.run(one_turn())
    instance = tmp_path / "instances" / "acme-desk.json"
    args = [
        "costs",
        str(instance),
        "--packs",
        str(tmp_path / "packs"),
        "--state",
        str(tmp_path / "state"),
        "--by",
        "vendor,model",
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert f"anthropic / {OPUS}" in out and f"${_usd(1000, 10):.4f}" in out
    assert "quality (30d): tool success n/a, escalations 0/1" in out
    assert main([*args[:4], "--state", str(tmp_path / "nowhere")]) == 2
