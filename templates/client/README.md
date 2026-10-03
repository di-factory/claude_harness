# @@NAME@@

The client solution **@@ID@@**, built by Di-Factory on its general harness
(pack: `@@PACKS@@`). This repository holds everything that is this client's own;
the harness itself is a dependency, pinned in `HARNESS_VERSION`.

| File | What it is |
|---|---|
| `@@ID@@.json` | The instance: this client's values and choices on top of the pack |
| `@@ID@@.answers.yaml` | The questionnaire answers it is built from (edit these, then rebuild) |
| `@@ID@@.knowledge/` | The client's own FAQ and documents the assistant answers from |
| `@@ID@@.summary.md` | What was built, what is not connected yet, recommendations |
| `@@ID@@.*.approval.json` | Di-Factory's signature over each deployed version |
| `HARNESS_VERSION` | The harness version (git ref) this solution runs on |
| `CLAUDE.md`, `docs/`, `GUIA.md`, `.claude/` | After the handover: what the client's own Claude starts from |

**Never in this repository:** keys and secrets (they live in `~/.dif/secrets` on the
server), signing keys, the staged deploy folder or the database.

## Working on it

On the server, from the harness folder:

```bash
./setup.sh                      # reuse this client: rebuild from the answers, sign, deploy
uv run dif-general-harness admin status      # the running assistant
```

Every build, fine-tuning round and signature is a commit here, so the history is the
record of what changed and when. CI (`.github/workflows/validate.yml`) checks that the
instance still validates against the pinned harness.
