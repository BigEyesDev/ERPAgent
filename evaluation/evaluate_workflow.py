"""Compares pipeline output against `data/expected/*.json` ground truth.

Reports extraction/outcome accuracy and, as the headline safety metric,
the **false-auto-approval rate**: the fraction of the *entire* fixture set
that the pipeline auto-created when it should not have. Framed as a share
of everything the system saw (not just of its `AUTO_CREATE` decisions),
since that is the number an operations owner actually cares about: "out of
every order that came in, how often did something wrong get through
unattended." This is the one number worth actually computing.

Run from the repo root: `uv run python -m evaluation.evaluate_workflow`
(module form, not a direct script path - needed so `src` resolves as a
package regardless of the caller's working directory).

Defaults to **replay** mode: extractions are read from
`data/extraction_cache.json`, so it runs in seconds with no API key. Pass
`--live` to call the model through OpenRouter instead (needs a valid
`OPENROUTER_API_KEY` and takes a minute or two).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Replay mode makes no live call, but `src.config` still validates that the
# key exists at import time, so provide a harmless placeholder before importing.
os.environ.setdefault("OPENROUTER_API_KEY", "replay-mode-no-key-needed")

from src.duplicate_detection import DuplicateDetector
from src.erp_client import ERPClient
from src.pipeline import PipelineResult, run_fixture_set

REPO_ROOT = Path(__file__).resolve().parent.parent
EMAILS_DIR = REPO_ROOT / "data" / "emails"
EXPECTED_DIR = REPO_ROOT / "data" / "expected"


@dataclass
class FixtureEvaluation:
    """Actual vs. expected outcome for one fixture."""

    fixture: str
    expected_outcome: str
    actual_outcome: str
    correct: bool
    false_auto_approval: bool


@dataclass
class EvaluationReport:
    """Aggregate evaluation over the whole fixture set."""

    evaluations: list[FixtureEvaluation] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.evaluations)

    @property
    def correct_outcome_rate(self) -> float:
        return sum(e.correct for e in self.evaluations) / self.total if self.total else 0.0

    @property
    def false_auto_approval_rate(self) -> float:
        return sum(e.false_auto_approval for e in self.evaluations) / self.total if self.total else 0.0

    def rate_of(self, outcome: str) -> float:
        return sum(e.actual_outcome == outcome for e in self.evaluations) / self.total if self.total else 0.0

    def print_report(self) -> None:
        print(f"{'fixture':38s} {'expected':24s} {'actual':24s} correct")
        print("-" * 100)
        for e in self.evaluations:
            marker = "OK" if e.correct else "MISMATCH"
            print(f"{e.fixture:38s} {e.expected_outcome:24s} {e.actual_outcome:24s} {marker}")
        print()
        print(f"Fixtures evaluated:        {self.total}")
        print(f"Correct-outcome rate:      {self.correct_outcome_rate:.1%}")
        print(f"False-auto-approval rate:  {self.false_auto_approval_rate:.1%}  <- headline safety metric")
        print(f"Human-review rate:         {self.rate_of('HUMAN_REVIEW'):.1%}")
        print(f"Clarification rate:        {self.rate_of('CLARIFICATION_REQUIRED'):.1%}")
        print(f"Security-quarantine rate:  {self.rate_of('SECURITY_QUARANTINE'):.1%}")


def _expected_outcome_for(fixture_stem: str) -> str:
    payload = json.loads((EXPECTED_DIR / f"{fixture_stem}.json").read_text(encoding="utf-8"))
    return payload["expected_workflow"]["outcome"]


def evaluate(results: list[PipelineResult], fixture_paths: list[Path]) -> EvaluationReport:
    """Builds an `EvaluationReport` from pipeline results paired with their fixtures.

    Args:
        results: One `PipelineResult` per fixture, same order as `fixture_paths`
            (as returned by `pipeline.run_fixture_set`).
        fixture_paths: The `.eml` paths the results correspond to.
    """
    report = EvaluationReport()
    for path, result in zip(fixture_paths, results, strict=True):
        expected_outcome = _expected_outcome_for(path.stem)
        actual_outcome = result.decision.outcome.value if result.decision else "NONE"
        report.evaluations.append(
            FixtureEvaluation(
                fixture=path.stem,
                expected_outcome=expected_outcome,
                actual_outcome=actual_outcome,
                correct=actual_outcome == expected_outcome,
                false_auto_approval=actual_outcome == "AUTO_CREATE" and expected_outcome != "AUTO_CREATE",
            )
        )
    return report


def main(*, live: bool = False) -> EvaluationReport:
    cache_path = REPO_ROOT / "data" / "extraction_cache.json"
    mode = "live (calling the model)" if live else "replay (cached extractions)"
    fixture_paths = sorted(EMAILS_DIR.glob("*.eml"))
    print(f"Evaluating {len(fixture_paths)} fixtures in {mode} mode...\n", flush=True)

    def progress(i: int, n: int, path: Path) -> None:
        print(f"  [{i:2d}/{n}] {path.stem}", flush=True)

    erp = ERPClient(audit_path=REPO_ROOT / "data" / "evaluation_audit.jsonl")
    detector = DuplicateDetector()
    results = run_fixture_set(
        EMAILS_DIR, erp, detector, use_cache=not live, cache_path=None if live else cache_path, progress=progress
    )

    print()
    report = evaluate(results, fixture_paths)
    report.print_report()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the email-to-ERP workflow against ground truth.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the model through OpenRouter instead of the replay cache (needs OPENROUTER_API_KEY).",
    )
    args = parser.parse_args()
    main(live=args.live)
