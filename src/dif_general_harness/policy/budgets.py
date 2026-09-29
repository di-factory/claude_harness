"""Budgets and cost accounting (ARCHITECTURE §3.12, decision 17).

Costs are computed from a price table (USD per million tokens) so every run can be
attributed per tenant and vendor. Unknown models are priced at zero and reported, so a
missing price never silently disables a USD budget: ``unpriced`` lists them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..core.messages import Usage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


def _p(inp: float, out: float) -> Price:
    return Price(input=inp, output=out, cache_read=inp * 0.1, cache_write=inp * 1.25)


# USD per 1M tokens, Anthropic first-party API rates (Claude API skill, cached 2026-09-25).
DEFAULT_PRICES: dict[str, Price] = {
    "claude-fable-5-1": _p(10.0, 50.0),
    "claude-fable-5": _p(10.0, 50.0),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": _p(5.0, 25.0),
    "claude-opus-4-8": _p(5.0, 25.0),
    "claude-opus-4-7": _p(5.0, 25.0),
    "claude-opus-4-6": _p(5.0, 25.0),
    "claude-sonnet-5-5": Price(2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": _p(2.0, 10.0),
    "claude-sonnet-4-6": _p(3.0, 15.0),
    "claude-haiku-4-5": _p(1.0, 5.0),
}


def cost_usd(usage: Usage, price: Price) -> float:
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / 1_000_000


@dataclass(frozen=True)
class Limits:
    usd: float | None = None
    tokens: int | None = None
    turns: int | None = None
    wall_time_s: float | None = None

    @classmethod
    def from_spec(cls, raw: dict[str, object] | None) -> Limits:
        raw = raw or {}
        wall = raw.get("wall_time")
        return cls(
            usd=_num(raw.get("usd")),
            tokens=_int(raw.get("tokens")),
            turns=_int(raw.get("turns")),
            wall_time_s=_duration_s(str(wall)) if wall else None,
        )

    def tighten(self, other: Limits) -> Limits:
        """The stricter of two limits, field by field (a per-agent budget never loosens)."""

        def pick[T: (int, float)](a: T | None, b: T | None) -> T | None:
            return b if a is None else a if b is None else min(a, b)

        return Limits(
            usd=pick(self.usd, other.usd),
            tokens=pick(self.tokens, other.tokens),
            turns=pick(self.turns, other.turns),
            wall_time_s=pick(self.wall_time_s, other.wall_time_s),
        )


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _int(v: object) -> int | None:
    return int(v) if isinstance(v, (int, float)) else None


def _duration_s(value: str) -> float:
    unit = value[-1]
    return float(value[:-1]) * {"s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class DailySpend:
    """Spend per tenant per UTC day. In memory for M1; persisted with Postgres in M2."""

    _totals: dict[tuple[str, str], float] = field(default_factory=dict)

    def _key(self, tenant: str) -> tuple[str, str]:
        return tenant, datetime.now(UTC).date().isoformat()

    def add(self, tenant: str, usd: float) -> None:
        key = self._key(tenant)
        self._totals[key] = self._totals.get(key, 0.0) + usd

    def today(self, tenant: str) -> float:
        return self._totals.get(self._key(tenant), 0.0)

    def set_today(self, key: str, usd: float) -> None:
        """Load a persisted total (see ``SpendStore``)."""
        self._totals[self._key(key)] = usd


class RunMeter:
    """The loop's ``Meter`` for one run: prices each call and enforces the limits."""

    def __init__(
        self,
        tenant: str,
        per_run: Limits,
        per_tenant_day: Limits | None = None,
        *,
        prices: dict[str, Price] | None = None,
        daily: DailySpend | None = None,
        agent: str | None = None,
        per_agent_day: Limits | None = None,
    ) -> None:
        self.tenant = tenant
        self.per_run = per_run
        self.per_day = per_tenant_day or Limits()
        self.agent_key = f"{tenant}/{agent}" if agent else None
        self.per_agent_day = per_agent_day or Limits()
        self.prices = DEFAULT_PRICES if prices is None else prices
        self.daily = daily or DailySpend()
        self.total = Usage()
        self.by_model: dict[str, float] = {}
        self.unpriced: set[str] = set()
        self.pending: list[tuple[str, Usage]] = []  # priced calls not yet persisted
        self._started = time.monotonic()

    def charge(self, usage: Usage, model: str | None) -> Usage:
        price = self.prices.get(model or "")
        if price is None:
            self.unpriced.add(model or "unknown")
            priced = usage
        else:
            priced = usage.model_copy(update={"cost_usd": cost_usd(usage, price)})
        self.total = self.total + priced
        name = model or "unknown"
        self.by_model[name] = self.by_model.get(name, 0.0) + priced.cost_usd
        self.pending.append((name, priced))
        self.daily.add(self.tenant, priced.cost_usd)
        if self.agent_key:
            self.daily.add(self.agent_key, priced.cost_usd)
        return priced

    def exceeded(self, turns: int) -> str | None:
        run, day = self.per_run, self.per_day
        tokens = self.total.input_tokens + self.total.output_tokens
        if run.usd is not None and self.total.cost_usd >= run.usd:
            return f"run cost ${self.total.cost_usd:.4f} reached the ${run.usd} limit"
        if run.tokens is not None and tokens >= run.tokens:
            return f"run used {tokens} tokens, limit {run.tokens}"
        if run.turns is not None and turns >= run.turns:
            return f"run reached {turns} turns, limit {run.turns}"
        elapsed = time.monotonic() - self._started
        if run.wall_time_s is not None and elapsed >= run.wall_time_s:
            return f"run took {elapsed:.0f}s, limit {run.wall_time_s:.0f}s"
        if day.usd is not None and self.daily.today(self.tenant) >= day.usd:
            return f"tenant spent ${self.daily.today(self.tenant):.4f} today, limit ${day.usd}"
        agent_day = self.per_agent_day.usd
        if self.agent_key and agent_day is not None:
            spent = self.daily.today(self.agent_key)
            if spent >= agent_day:
                return f"agent spent ${spent:.4f} today, limit ${agent_day}"
        return None
