---
name: dif-constructor
description: Build, verify and prepare the deploy of a client solution on the Di-Factory general harness (dif-general-harness). Use when asked to set up, adjust or deploy a solution for a client. Deploys always wait for Jag's signed approval.
---

# Di-Factory constructor (dif-general-harness)

You drive the harness constructor for Teky. The harness does the work; you run its
commands, relay its questions and report its outputs. Read each command's output before
the next step.

## Rules

- **Never approve a deploy.** Only Jag approves (architecture decision 37). You do not have,
  request, create or use an approver key. You ask Jag to run `approve` himself.
- **Never handle secret values.** Report the secret *names* the summary lists; the client
  stores the values in their own vault.
- **Never invent capability.** If `build` says no pack fits, stop and report that a new
  pack is Di-Factory design work.
- Work from the harness repository root with `uv run dif-general-harness ...`.

## 1. Match and interview

```bash
uv run dif-general-harness build --request "<what the client needs>" \
  --packs <packs-dir> --out instances/<client>
```

- Exit code 3: no pack fits. Stop and report.
- Interactive questions show `[group] question (answered_by; example; default)`. Relay
  `client` questions to the client contact, answer `difactory` ones from Di-Factory's
  standards, or ask Jag.
- For a non-interactive run, write the answers to a YAML file (`tenant`, `solution`,
  `values` sections; see a previous `<id>.answers.yaml`) and pass `--answers <file>`.
- Output: `<id>.json` (instance spec), `<id>.answers.yaml`, `<id>.summary.md`.
  `NOT READY` lists open answers: resolve them and rebuild.

## 2. Verify

```bash
uv run dif-general-harness spec validate instances/<client>/<id>.json --packs <packs-dir>
uv run dif-general-harness eval instances/<client>/<id>.json --packs <packs-dir>
```

Report the pass rate, skipped cases (with their reasons) and any unsafe actions. A
failing eval blocks the approval request.

## 3. Ask Jag to approve

Send Jag the summary (`<id>.summary.md`), the validation and eval results, and the exact
command for him to run on his machine:

```bash
uv run dif-general-harness approve instances/<client>/<id>.json --packs <packs-dir> \
  --target <docker|aws> --key ~/.dif/keys/jag.key --by jag
```

It writes `<id>.<target>.approval.json`. Any later change to the instance, a prompt or a
pack invalidates it; if something changes, ask again.

## 4. Deploy (after approval)

```bash
uv run dif-general-harness deploy instances/<client>/<id>.json --packs <packs-dir> \
  --target <docker|aws> --approvers .dif/approvers.json
```

- `refused:` means no valid approval for exactly this solution: report it, do not work
  around it.
- The output lists the secrets the client must store and the commands. Add `--run` only
  when Jag asked you to execute them and the target credentials are available.
- After the deploy, check `GET /healthz` and `GET /readyz` on the instance URL and report.

## Adjusting a live instance

Edit `<id>.answers.yaml`, rebuild with `--answers`, verify, and ask Jag to approve again.
Then deploy as above, or have the control plane push the new version.
