# Research graph: a verified map of a market, kept up to date

**Status: built after v1 (decision 86) with offline tests; not yet run live.** Pack:
[`research-graph`](../spec/examples/research-graph/pack.json).

## What it is

Recurring research with a memory that has a shape: a graph of the companies in a client's
market (competitors, suppliers, partners), each one checked at two independent sources, and
every relationship ("supplies", "competes with", "partners with", "owns", "shares a vendor")
with the line of evidence that supports it. A researcher agent updates it every week; an
analyst agent answers questions about it, with sources. It only reads: it never sends, pays
or publishes.

## The story for a client

Knowing your market is a weekly job nobody has time for, and the notes go stale. The graph
starts from the companies you name and grows from what it finds; every Monday it re-checks
what is stale and researches what is new. Ask "who supplies our two biggest competitors?" and
the answer comes with the sources, never from the model's memory.

## How it works

- **Every Monday at 07:00** (or when an entity changes), the `scan` workflow researches the
  entities that need it, one task each, in the fixed research shape.
- **A company counts as verified** only with two independent sources; a relationship found
  with less than 0.6 confidence is dropped; anything older than 30 days is re-checked.
- **Aliases** ("Acme", "Acme S.A. de C.V.") are merged from an aliases file; collisions are
  reported for a person.
- **The analyst** answers through `graph.query`, citing nodes, edges and their evidence.

## The safeguards

- **Read-only:** web reads and the graph only; anything external is denied.
- **Pages are data:** what a web page says is evidence to weigh, never an instruction.
- **Budget:** USD 0.50 per research task, USD 15 per day.

## Before a live test

It needs only a model key and a list of companies: it is the easiest of the untested packs
to try live, through its API channel.
