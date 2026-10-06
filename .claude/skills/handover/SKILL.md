---
name: handover
description: Hand a finished client solution to its client on this server - their own keys, the documents their Claude starts from, Di-Factory's access removed. Use when the operator says the solution is final and the client takes over (the last step, after setup and fine-tuning).
---

# /handover: the client takes their server

The last step of every client: **setup** (`./setup.sh`, the questionnaire) → **fine-tuning**
(`./setup.sh` again, reusing the client, as many rounds as needed) → **handover** (this). Run it
on the client's server, from the harness folder, with the Di-Factory operator at the keyboard.

Rules for the whole skill:
- **Never print, read or ask for a secret value.** Keys are typed by the operator in their own
  terminal or piped between commands; you only see lengths and prefixes.
- **Ask before every irreversible step** (deleting Jag's key, removing SSH access) and do them
  in the order below: the operator's own access goes last.
- Report each step's result in one line; at the end, a short summary of what is done and what
  is left (from `HANDOVER.md`).

## 1. Which client, and is it final?
1. List the clients: `ls clients/*.json` (instances: `"kind": "instance"`). If there are
   several, ask which one. Call it `$ID` (the file name without `.json`).
2. Ask the operator to confirm the solution is final (no fine-tuning pending). If not, stop:
   fine-tuning is `./setup.sh` → reuse the client.
3. Check it is online: `curl -fsS https://$HOST/healthz` (the host is the second line of
   `.dif/online`). If it is not, stop and say so.
4. Check what it watches by itself: `uv run dif-general-harness admin inbox` should hold no
   unread `review` items (replies that got worse in the nightly replay), and
   `uv run dif-general-harness admin review` no open `proposal`. If there are, go through
   them with the operator first: a client should not inherit Di-Factory's open work. The
   nightly watch and the weekly review need the pack's `verifier` model role; without it they
   stay off (`admin review --now` says so): tell the operator.

## 2. What the documents need
Ask, in one message: the owner's name, the language they speak (es, en...), and how they reach
Di-Factory (email or WhatsApp). Also ask whether the server stays in Di-Factory's AWS account or
moves to the client's (moving is manual; it is listed in `HANDOVER.md`).

## 3. The client's own keys
1. **Model key.** The client's Anthropic key, from their own workspace, with a spending limit.
   The operator types it in their terminal (not here): `uv run dif-general-harness secrets set
   anthropic`. Then confirm with `uv run dif-general-harness secrets check clients/$ID.json`.
2. **A new admin token**, which only the client will hold:
   `openssl rand -hex 24 | uv run dif-general-harness secrets set admin_token`.
3. **Put both in the running instance** and restart it (the folder is the first line of
   `.dif/online`, `$FOLDER`):
   ```bash
   sudo cp ~/.dif/secrets/anthropic ~/.dif/secrets/admin_token $FOLDER/secrets/
   sudo chown 10001 $FOLDER/secrets/* && sudo chmod 600 $FOLDER/secrets/*
   sudo docker compose -f $FOLDER/docker-compose.yml restart instance
   ```
   Wait ~20 s and check: `uv run dif-general-harness admin status` must say `Online`.
   Other accounts (Twilio, Google...) follow the same pattern; `secrets check` lists them.

## 4. The documents
```bash
uv run dif-general-harness handover clients/$ID.json --owner "NAME" --lang LANG \
    --support "CONTACT" --url https://$HOST
```
It writes `~/<tenant>/` (CLAUDE.md, docs/, GUIA.md, the `negocio` command, .claude/ with the
skills estado, bandeja, responder, faq, preguntas, mejoras and costos) and lists its checks.
Then try the client's command: `cd ~/<tenant> && ./negocio status`, and
`./negocio review` (the weekly review's proposals, usually none yet).

## 5. Jag's key leaves the server
Deploys are signed by Di-Factory, never on a client's machine.
1. Ask the operator to copy it to their own computer first and confirm it arrived, e.g. from
   their machine: `scp -i <key>.pem ubuntu@<IP>:~/.dif/keys/jag.key ./jag.key`.
2. Only after they confirm: `rm ~/.dif/keys/jag.key`.
3. Run the `handover` command of step 4 again: every automatic check must show ✓.

## 6. What is left by hand
Read `~/<tenant>/HANDOVER.md` with the operator and go through its unchecked boxes: the AWS
account, backups (a daily EBS snapshot), a health alert on `/healthz`, Twilio/Google accounts in
the client's name. Mark which are done.

## 7. The client's first session, then Di-Factory steps out
1. Log Di-Factory's account out of Claude Code on this server (`/logout`), so the client signs
   in with their own.
2. With the client: `cd ~/<tenant> && claude`, they log in, and try «¿cómo va todo?» and
   `GUIA.md`'s examples.
3. **Last**, and only with the operator's explicit OK: remove Di-Factory's SSH key from
   `~/.ssh/authorized_keys` once the client's access works (or hand over the AWS account).
   After this, Di-Factory reaches the server only if the client lets it.

## Summary to give
What was done (keys swapped, documents at `~/<tenant>/`, Jag's key off the server, checks ✓),
what is left from `HANDOVER.md`, and where the client starts: `cd ~/<tenant> && claude`.
