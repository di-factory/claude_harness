---
name: entity-research
description: How to research one entity for the market graph - steps, which sources win, the self-check before returning.
---
STEPS: 1 identify the exact entity -> 2 pull primary sources -> 3 extract the fields ->
4 self-check -> 5 return.

DECISION RULES:
- Filings, registries and the entity's own documents outrank coverage of them.
- Conflicting numbers: return both, each with its date. Never average.
- Two pages of one site, or two press releases, are one source.
- A relationship ("supplies", "owns"...) needs a line from a source that states it; quote
  that line as the evidence.

SELF-CHECK before returning:
- Every field traces to one of the sources returned.
- It is the entity the task named (not a namesake, not the parent company).
- Confidence below 0.6: return it with "flagged": true; do not leave it out.

OUTPUT: the JSON return the task describes, nothing else.
