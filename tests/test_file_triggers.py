"""File and batch triggers (G2): new files and lists of items start work, each exactly once."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message, ToolUseBlock
from dif_general_harness.triggers.files import FileSourceError, build_source, matches
from tests.support import ADMIN_H, Env

CFDI = '<?xml version="1.0"?><cfdi:Comprobante Total="1160.00" Folio="A-17"/>'


def _inbox(inbox: Path, **extra: Any) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["triggers"]["inbox"] = {
            "type": "file",
            "source": {"type": "folder", "path": str(inbox), "poll": "5m"},
            "match": ["*.xml"],
            "dedupe_key": "file.sha256",
            "agent": "ops",
            "input": "New invoice {{event.file.name}}: {{event.file.text}}",
            **extra,
        }

    return edit


async def _received(inst: Any) -> list[str]:
    return [r.subject for r in await inst.audit.records(inst.scope, action="file_received")]


async def test_each_new_file_starts_work_once(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (inbox / "2026").mkdir(parents=True)
    (inbox / "2026" / "a.xml").write_text(CFDI)
    (inbox / "scan.pdf").write_bytes(b"%PDF-1.7")  # not matched
    (inbox / ".a.xml").write_text(CFDI)  # hidden
    (inbox / "b.xml.part").write_text(CFDI)  # still uploading
    script = [Message.assistant("Registered.") for _ in range(3)]
    env = Env(tmp_path, script, edit=_inbox(inbox))
    inst, headless, client = await env.open()
    async with inst, client:
        assert "inbox" in headless.triggers
        await headless.start()
        await headless.worker().drain()
        assert await _received(inst) == ["2026/a.xml"]
        [request] = env.provider.requests
        text = request.messages[-1].text()
        assert "New invoice a.xml" in text and 'Total="1160.00"' in text  # text files inline

        (inbox / "copy.xml").write_text(CFDI)  # the same bytes under another name
        env.clock.now += 300
        await headless.worker().drain()
        duplicates = await inst.audit.records(inst.scope, action="file_duplicate")
        assert [r.subject for r in duplicates] == ["copy.xml"]
        assert len(env.provider.requests) == 1

        (inbox / "2026" / "a.xml").write_text(CFDI.replace("A-17", "A-18"))  # changed bytes
        (inbox / "c.xml").write_text(CFDI.replace("A-17", "A-19"))
        env.clock.now += 300
        await headless.worker().drain()
        assert sorted(await _received(inst)) == ["2026/a.xml", "2026/a.xml", "c.xml"]
        assert len(env.provider.requests) == 3

        # a restart (or a second scan) finds nothing new: what was seen is stored
        assert await headless.scan_files("inbox") == 0
        answer = await client.post("/admin/triggers/inbox/run", headers=ADMIN_H)
        assert answer.json() == {"queued": 0}
        data = await headless.read_file((inbox / "c.xml").resolve().as_uri())
        assert b"A-19" in data


async def test_file_triggers_start_workflows_with_the_file(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "big.xml").write_text("x" * 200)
    (inbox / "ok.xml").write_text(CFDI)
    edit = _inbox(inbox, workflow="report", agent=None, input=None)

    def both(spec: dict[str, Any]) -> None:
        edit(spec)
        trig = spec["triggers"]["inbox"]
        trig.pop("agent"), trig.pop("input")
        trig["source"]["max_bytes"] = 100
        trig["when"] = "event.file.size > 10"

    env = Env(tmp_path, [], edit=both)
    inst, headless, client = await env.open()
    async with inst, client:
        await headless.start()
        await headless.worker().drain()
        rows = await inst.db.fetchall("SELECT workflow, input FROM workflow_runs")
        assert [r["workflow"] for r in rows] == ["report"]
        given = json.loads(rows[0]["input"])["file"]
        assert given["name"] == "ok.xml" and given["content_type"].endswith("/xml")
        assert len(given["sha256"]) == 64 and given["uri"].startswith("file://")
        skipped = await inst.audit.records(inst.scope, action="file_skipped")
        assert [r.subject for r in skipped] == ["big.xml"]  # over max_bytes: never read


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        start = int(kw.get("ContinuationToken") or 0)
        page = keys[start : start + 2]  # small pages: pagination is exercised
        more = start + 2 < len(keys)
        return {
            "Contents": [
                {"Key": k, "Size": len(self.objects[k]), "ETag": f'"{hash(self.objects[k])}"'}
                for k in page
            ],
            "IsTruncated": more,
            **({"NextContinuationToken": str(start + 2)} if more else {}),
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == "acme-inbox"
        self.reads.append(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def _s3(spec: dict[str, Any]) -> None:
    spec["secrets"]["storage"] = {"description": "bucket keys"}
    spec["triggers"]["receipts"] = {
        "type": "file",
        "source": {"type": "s3", "bucket": "acme-inbox", "prefix": "in/",
                   "credentials": {"$secret": "storage"}},
        "match": ["*.xml", "*.pdf"],
        "dedupe_key": "file.sha256",
        "workflow": "report",
    }  # fmt: skip


async def test_s3_buckets_are_scanned_page_by_page(tmp_path: Path) -> None:
    s3 = FakeS3({f"in/{n}.xml": f"<i n='{n}'/>".encode() for n in range(5)} | {"out/x.xml": b""})
    keys = json.dumps({"access_key_id": "AKIA", "secret_access_key": "s"})
    env = Env(tmp_path, [], edit=_s3, secrets={"storage": keys})
    env.s3_client = s3
    inst, headless, client = await env.open()
    async with inst, client:
        await headless.start()
        await headless.worker().drain()
        assert sorted(s3.reads) == [f"in/{n}.xml" for n in range(5)]
        assert len(await _received(inst)) == 5
        s3.reads.clear()
        env.clock.now += 60
        await headless.worker().drain()
        assert s3.reads == []  # unchanged objects are not read again
        assert await headless.read_file("s3://acme-inbox/in/3.xml") == b"<i n='3'/>"


async def test_a_broken_source_turns_the_trigger_off(tmp_path: Path) -> None:
    env = Env(tmp_path, [], edit=_s3, secrets={"storage": "not json"})
    env.s3_client = FakeS3({})
    inst, headless, client = await env.open()
    async with inst, client:
        [issue] = [i for i in headless.issues if i.path == "triggers.receipts"]
        assert issue.code == "trigger_unavailable" and "JSON" in issue.message
        assert "receipts" not in headless.triggers


def test_sources_and_patterns() -> None:
    for bad in ({"type": "ftp"}, {"type": "folder"}, {"type": "s3"}, "inbox"):
        try:
            build_source(bad, s3_client=object())
        except FileSourceError:
            continue
        raise AssertionError(f"{bad} was accepted")
    assert matches("2026/A.XML", ["*.xml"]) and not matches("a.pdf", ["*.xml"])
    assert matches("in/a.pdf", ["in/*.pdf"]) and matches("anything", None)


def _batch(spec: dict[str, Any]) -> None:
    spec["triggers"]["nightly"] = {
        "type": "batch",
        "cron": "0 2 * * *",
        "source": {"tool": "notes.read", "args": {"key": "queue"}},
        "dedupe_key": "item.id",
        "agent": "ops",
        "input": "Reconcile invoice {{event.item.id}} ({{event.batch.index}})",
    }
    spec["triggers"]["pushed"] = {"type": "batch", "workflow": "report"}
    spec["triggers"]["writer"] = {
        "type": "batch", "source": "notes.write", "workflow": "report"
    }  # fmt: skip


async def test_batches_fan_out_one_run_per_new_item(tmp_path: Path) -> None:
    script = [Message.assistant(f"Done {i}.") for i in range(3)]
    env = Env(tmp_path, script, edit=_batch)
    inst, headless, client = await env.open()
    async with inst, client:
        [issue] = [i for i in headless.issues if i.path == "triggers.writer"]
        assert "must be a read tool" in issue.message  # a batch source never changes anything
        queue = json.dumps([{"id": "F-1"}, {"id": "F-2"}, {"total": 3}])
        call = ToolUseBlock(id="w", name="notes.write", input={"key": "queue", "text": queue})
        await inst.tools.execute(call)
        await headless.start()
        planned = await inst.db.fetchall("SELECT kind FROM jobs WHERE kind = 'batch_run'")
        assert len(planned) == 1  # the next 2am

        first = (await client.post("/admin/triggers/nightly/run", headers=ADMIN_H)).json()
        assert (first["queued"], first["skipped"]) == (2, 1)  # an item without an id is skipped
        await headless.worker().drain()
        prompts = sorted(r.messages[-1].text() for r in env.provider.requests)
        assert prompts == ["Reconcile invoice F-1 (0)", "Reconcile invoice F-2 (1)"]

        again = (await client.post("/admin/triggers/nightly/run", headers=ADMIN_H)).json()
        assert (again["queued"], again["skipped"]) == (0, 3)  # nothing is processed twice

        pushed = await client.post(
            "/admin/triggers/pushed/run", headers=ADMIN_H, json={"items": [{"n": 1}, {"n": 2}]}
        )
        assert pushed.json()["queued"] == 2
        await headless.worker().drain()
        runs = await inst.db.fetchall("SELECT input FROM workflow_runs WHERE workflow = 'report'")
        assert sorted(json.loads(r["input"])["item"]["n"] for r in runs) == [1, 2]
        refused = await client.post("/admin/triggers/pushed/run", headers=ADMIN_H, json={})
        assert refused.status_code == 409  # no source and nothing pushed

        env.clock.now += 86400  # the scheduled run
        await headless.worker().drain()
        started = await inst.audit.records(inst.scope, action="batch_started")
        assert len(started) == 4
