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
1. List the clients: `ls -d ~/clients/*/` (each is its own git repository, outside the
   harness; an older client may still be a flat `clients/$ID.json` in the harness folder:
   `./setup.sh` → reuse it moves it into `~/clients/$ID/`). If
   there are several, ask which one. Call it `$ID` (the folder name) and its instance
   `$SPEC=~/clients/$ID/$ID.json`.
2. Ask the operator to confirm the solution is final (no fine-tuning pending). If not, stop:
   fine-tuning is `./setup.sh` → reuse the client.
3. Check it is online: `curl -fsS https://$HOST/healthz` (the host is the second line of
   `.dif/online`). If it is not, stop and say so.

## 2. What the documents need
Ask, in one message: the owner's name, the language they speak (es, en...), and how they reach
Di-Factory (email or WhatsApp). Also ask whether the server stays in Di-Factory's AWS account or
moves to the client's (moving is manual; it is listed in `HANDOVER.md`).

## 3. The client's own keys
1. **Model key.** The client's Anthropic key, from their own workspace, with a spending limit.
   The operator types it in their terminal (not here): `uv run dif-general-harness secrets set
   anthropic`. Then confirm with `uv run dif-general-harness secrets check $SPEC`.
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
uv run dif-general-harness handover $SPEC --owner "NAME" --lang LANG \
    --support "CONTACT" --url https://$HOST
```
It writes into the client's repository `~/clients/$ID/` (CLAUDE.md, docs/, GUIA.md, the `negocio`
command, .claude/), commits "Handover to NAME" (and pushes it when a GitHub token is stored),
and lists its checks. Then try the client's command: `cd ~/clients/$ID && ./negocio status`.

## 4b. The repository goes to the client
The client's repository holds everything about their solution (the instance, answers, FAQ,
signatures, these documents; never keys). Ask the operator which way:
1. If it is not on GitHub yet (the handover said "not on GitHub yet"): the operator stores a
   token that can create repositories in Di-Factory's organization, in their own terminal:
   `uv run dif-general-harness secrets set github`, then
   `uv run dif-general-harness client publish ~/clients/$ID`.
2. **Transfer** (the client owns it; they need a GitHub account and must accept the transfer
   in GitHub): `uv run dif-general-harness client transfer ~/clients/$ID --to ACCOUNT`.
   **Or invite** (Di-Factory keeps it, the client gets write access):
   `uv run dif-general-harness client invite ~/clients/$ID --user NAME`.
3. After a transfer, the server's copy pushes to the new address only if the client gives it
   access; say so, and leave the `origin` remote for the client's Claude to update
   (`git remote set-url origin https://github.com/ACCOUNT/client-$ID.git`).
4. Remove the GitHub token from the server once done: `rm ~/.dif/secrets/github`.

## 5. Jag's key leaves the server
Deploys are signed by Di-Factory, never on a client's machine.
1. Ask the operator to copy it to their own computer first and confirm it arrived, e.g. from
   their machine: `scp -i <key>.pem ubuntu@<IP>:~/.dif/keys/jag.key ./jag.key`.
2. Only after they confirm: `rm ~/.dif/keys/jag.key`.
3. Run the `handover` command of step 4 again: every automatic check must show ✓.

## 6. What is left by hand
Read `~/clients/$ID/HANDOVER.md` with the operator and go through its unchecked boxes: the AWS
account, backups (a daily EBS snapshot), a health alert on `/healthz`, Twilio/Google accounts in
the client's name. Mark which are done.

## 7. The client's first session, then Di-Factory steps out
1. Log Di-Factory's account out of Claude Code on this server (`/logout`), so the client signs
   in with their own.
2. With the client: `cd ~/clients/$ID && claude`, they log in, and try «¿cómo va todo?» and
   `GUIA.md`'s examples.
3. **Last**, and only with the operator's explicit OK: remove Di-Factory's SSH key from
   `~/.ssh/authorized_keys` once the client's access works (or hand over the AWS account).
   After this, Di-Factory reaches the server only if the client lets it.

## Summary to give
What was done (keys swapped, documents at `~/clients/$ID/`, Jag's key off the server, checks ✓),
what is left from `HANDOVER.md`, and where the client starts: `cd ~/clients/$ID && claude`.
