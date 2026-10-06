"""pass^k and ablations: consistency across trials, and whether each harness component still
pays for itself (decision 91)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor.ablation import verdict, without
from dif_general_harness.constructor.evals import CaseResult, EvalReport
from dif_general_harness.providers import FakeProvider
from dif_general_harness.spec import PackCatalog, load_instance


def _report(*cases: tuple[str, int, str, float]) -> EvalReport:
    return EvalReport([CaseResult("s", c, st, cost_usd=cost, trial=t)  # type: ignore[arg-type]
                       for c, t, st, cost in cases], {"pass_k": 1.0})  # fmt: skip


def test_pass_k_counts_only_cases_that_passed_every_trial() -> None:
    report = _report(("a", 1, "passed", 0.1), ("a", 2, "passed", 0.1),
                     ("b", 1, "passed", 0.1), ("b", 2, "failed", 0.1))  # fmt: skip
    assert report.pass_rate == pytest.approx(0.75) and report.pass_k == pytest.approx(0.5)
    assert report.trials == 2 and not report.ok  # a pass_k threshold of 1.0 is not met


def test_verdicts_compare_quality_first_then_cost() -> None:
    base = _report(("a", 1, "passed", 0.30))
    assert verdict(base, _report(("a", 1, "failed", 0.10))) == "keeps its place"
    assert verdict(base, _report(("a", 1, "passed", 0.10))) == "no measured lift"
    assert verdict(base, _report(("a", 1, "passed", 0.30))) == "no difference"


def test_components_are_removed_from_a_copy_only(examples: Path) -> None:
    resolved = load_instance(examples / "instances" / "clinica-sonrisa.json",
                             PackCatalog(roots=[examples]))  # fmt: skip
    assert resolved.data["skills"]
    ablated = without(resolved, "skills")
    assert ablated.data["skills"] == [] and resolved.data["skills"]  # the original is intact
    with pytest.raises(ValueError, match="unknown component"):
        without(resolved, "everything")


def test_eval_reports_pass_k_and_each_component_s_effect(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = examples / "instances" / "clinica-sonrisa.json"
    code = main(["eval", str(instance), "--state", str(tmp_path), "--repeat", "2",
                 "--ablate", "skills,memory"], provider=FakeProvider([]))  # fmt: skip
    out = capsys.readouterr().out
    assert "PASSED   confirm.yaml / opt-out-before-reminder (trial 2)" in out
    assert "pass rate 33% over 6 run(s), 3 skipped, pass^2 33%" in out
    assert "Without each component" in out
    assert "-skills" in out and "-memory" in out and "no difference" in out
    assert code == 1
