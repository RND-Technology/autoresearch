"""
Liv Hana SI — Parallel Experiment Runner

Runs multiple mutations concurrently on M4 Max using ProcessPoolExecutor.
Each worker gets an isolated copy of voice_optimizer.py, mutates and evaluates
independently. Main thread collects results and promotes the single best winner.

This file is NEW — added as part of RVOS (Recursive Voice Optimization System).
"""

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("liv_hana.parallel_runner")

ROOT = Path(__file__).parent.parent
MUTABLE_FILE = ROOT / "liv_hana" / "voice_optimizer.py"
EVALUATOR_FILE = ROOT / "liv_hana" / "evaluator.py"
LOCK_FILE = ROOT / "liv_hana" / ".optimizer.lock"


@dataclass
class ExperimentResult:
    """Result from a single parallel experiment."""
    exp_id: str
    status: str  # "improved", "regressed", "failed", "blocked"
    score: float | None = None
    delta: float | None = None
    ttfa_ms: float | None = None
    ralph_pass: bool | None = None
    tps: float | None = None
    barge_accuracy: float | None = None
    proposed_code: str | None = None
    mutation_metadata: dict | None = None
    elapsed_s: float = 0.0
    error: str | None = None


def _evaluate_in_isolation(
    exp_id: str,
    proposed_code: str,
    evaluator_path: str,
    root_path: str,
    timeout: int,
) -> dict:
    """
    Run evaluation in an isolated temp directory.
    This function runs in a child process via ProcessPoolExecutor.
    Returns a dict (not ExperimentResult — must be picklable).
    """
    t0 = time.time()
    tmpdir = None
    try:
        # Create isolated workspace
        tmpdir = tempfile.mkdtemp(prefix=f"livhana_{exp_id}_")
        iso_dir = Path(tmpdir) / "liv_hana"
        iso_dir.mkdir()

        # Write proposed code and copy evaluator
        (iso_dir / "voice_optimizer.py").write_text(proposed_code)
        (iso_dir / "__init__.py").write_text("")
        shutil.copy2(evaluator_path, iso_dir / "evaluator.py")

        # Run evaluator
        result = subprocess.run(
            [sys.executable, str(iso_dir / "evaluator.py")],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tmpdir,
            env={**os.environ, "LIV_HANA_EVAL_MODE": "1"},
        )

        elapsed = time.time() - t0

        if result.returncode != 0:
            return {
                "exp_id": exp_id,
                "status": "failed",
                "error": result.stderr[:200],
                "elapsed_s": elapsed,
            }

        # Parse output
        stdout = result.stdout
        score = None
        ttfa_ms = None
        ralph_pass = None
        tps = None
        barge_accuracy = None

        for line in stdout.splitlines():
            m = re.search(r"SCORE:\s*([0-9.]+)", line)
            if m:
                score = float(m.group(1))
            m2 = re.search(r"TTFA_MS:\s*([0-9.]+)", line)
            if m2:
                ttfa_ms = float(m2.group(1))
            m3 = re.search(r"RALPH_PASS:\s*(True|False|1|0)", line)
            if m3:
                ralph_pass = m3.group(1) in ("True", "1")
            m4 = re.search(r"TPS:\s*([0-9.]+)", line)
            if m4:
                tps = float(m4.group(1))
            m5 = re.search(r"BARGE_ACCURACY:\s*([0-9.]+)", line)
            if m5:
                barge_accuracy = float(m5.group(1))

        if score is None:
            return {
                "exp_id": exp_id,
                "status": "failed",
                "error": "no_score_line",
                "elapsed_s": elapsed,
            }

        return {
            "exp_id": exp_id,
            "status": "evaluated",
            "score": score,
            "ttfa_ms": ttfa_ms,
            "ralph_pass": ralph_pass,
            "tps": tps,
            "barge_accuracy": barge_accuracy,
            "elapsed_s": elapsed,
        }

    except subprocess.TimeoutExpired:
        return {
            "exp_id": exp_id,
            "status": "failed",
            "error": "timeout",
            "elapsed_s": time.time() - t0,
        }
    except Exception as e:
        return {
            "exp_id": exp_id,
            "status": "failed",
            "error": str(e),
            "elapsed_s": time.time() - t0,
        }
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


class ParallelExperimentRunner:
    """
    Runs N experiments in parallel using ProcessPoolExecutor.

    Architecture:
    1. Main thread proposes N mutations (via BayesianMutator)
    2. Main thread runs AST security scan on all N
    3. Dispatches N evaluations to ProcessPoolExecutor
    4. Collects results, picks best winner above current best
    5. Promotes winner to voice_optimizer.py (file-locked)
    """

    def __init__(self, workers: int | None = None, timeout: int = 300):
        self.workers = workers or min(os.cpu_count() or 4, 6)
        self.timeout = timeout
        log.info(f"ParallelExperimentRunner initialized with {self.workers} workers")

    def run_batch(
        self,
        proposals: list[tuple[str, str, dict]],  # [(exp_id, proposed_code, metadata), ...]
        best_score: float,
    ) -> list[ExperimentResult]:
        """
        Run a batch of experiments in parallel.

        Args:
            proposals: List of (exp_id, proposed_code, mutation_metadata)
            best_score: Current best score to beat

        Returns:
            List of ExperimentResults for all proposals
        """
        results = []
        t0 = time.time()

        with ProcessPoolExecutor(max_workers=self.workers) as executor:
            futures = {}
            for exp_id, proposed_code, metadata in proposals:
                future = executor.submit(
                    _evaluate_in_isolation,
                    exp_id,
                    proposed_code,
                    str(EVALUATOR_FILE),
                    str(ROOT),
                    self.timeout,
                )
                futures[future] = (exp_id, proposed_code, metadata)

            for future in as_completed(futures):
                exp_id, proposed_code, metadata = futures[future]
                try:
                    raw = future.result(timeout=self.timeout + 30)
                except Exception as e:
                    raw = {
                        "exp_id": exp_id,
                        "status": "failed",
                        "error": str(e),
                        "elapsed_s": 0,
                    }

                result = ExperimentResult(
                    exp_id=raw["exp_id"],
                    status=raw["status"],
                    score=raw.get("score"),
                    ttfa_ms=raw.get("ttfa_ms"),
                    ralph_pass=raw.get("ralph_pass"),
                    tps=raw.get("tps"),
                    barge_accuracy=raw.get("barge_accuracy"),
                    elapsed_s=raw.get("elapsed_s", 0),
                    error=raw.get("error"),
                    proposed_code=proposed_code,
                    mutation_metadata=metadata,
                )

                # Classify result
                if result.status == "evaluated" and result.score is not None:
                    if result.ralph_pass is False:
                        result.status = "ralph_violation"
                        result.delta = None
                    elif result.score > best_score:
                        result.status = "improved"
                        result.delta = result.score - best_score
                    else:
                        result.status = "regressed"
                        result.delta = result.score - best_score

                results.append(result)

        batch_time = time.time() - t0
        wins = sum(1 for r in results if r.status == "improved")
        log.info(
            f"⚡ Parallel batch complete: {len(results)} experiments in {batch_time:.1f}s | "
            f"Wins: {wins} | Best delta: {max((r.delta or 0) for r in results):+.4f}"
        )

        return results

    def promote_winner(self, winner: ExperimentResult) -> bool:
        """
        Promote the winning code to voice_optimizer.py with file locking.
        Returns True if promotion succeeded.
        """
        if not winner.proposed_code:
            return False

        try:
            lock_fd = open(LOCK_FILE, "w")
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                # Backup current
                backup = MUTABLE_FILE.with_suffix(".py.bak")
                shutil.copy2(MUTABLE_FILE, backup)
                # Write winner
                MUTABLE_FILE.write_text(winner.proposed_code)
                log.info(
                    f"✅ Promoted {winner.exp_id}: score {winner.score:.4f} "
                    f"(Δ+{winner.delta:.4f})"
                )
                return True
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
        except BlockingIOError:
            log.warning("Could not acquire lock — another process is promoting")
            return False
        except Exception as e:
            log.error(f"Promotion failed: {e}")
            return False

    def pick_best_winner(self, results: list[ExperimentResult]) -> ExperimentResult | None:
        """Pick the single best improvement from a batch of results."""
        winners = [r for r in results if r.status == "improved" and r.delta is not None]
        if not winners:
            return None
        return max(winners, key=lambda r: r.delta)
