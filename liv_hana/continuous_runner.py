"""
Liv Hana SI — Continuous Autonomous Runner

Wraps the full RVOS loop for 24/7 autonomous operation.
- Runs experiment batches continuously
- Cooling schedule: aggressive when winning, relaxed when plateaued
- Progress reports at configurable intervals
- Council gate + DSPy promotion when thresholds met
- Cloud config sync between rounds

Usage:
    python liv_hana/continuous_runner.py --parallel 6 --strategy bayesian
    python liv_hana/continuous_runner.py --parallel 4 --council-gate --sync-cloud

This file is NEW — added as part of RVOS (Recursive Voice Optimization System).
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("liv_hana.continuous")

ROOT = Path(__file__).parent.parent
MUTABLE_FILE = ROOT / "liv_hana" / "voice_optimizer.py"
EXPERIMENTS_LOG = ROOT / "experiments_log.jsonl"

# Ensure autoresearch root is on sys.path for liv_hana imports
_root_str = str(ROOT)
if _root_str not in sys.path:
    sys.path.insert(0, _root_str)


class ContinuousRunner:
    """
    24/7 autonomous voice optimization runner.

    Features:
    - Adaptive cooling: 5s between batches when winning, 30s when plateaued
    - Council gate: submits for review after 3+ improvements
    - DSPy bridge: promotes winning configs and syncs from cloud
    - Progress reports: summary every N experiments
    """

    def __init__(
        self,
        strategy: str = "bayesian",
        parallel: int = 4,
        batch_size: int = 20,
        timeout: int = 30,
        report_interval: int = 50,
        council_gate: bool = True,
        sync_cloud: bool = False,
    ):
        self.strategy = strategy
        self.parallel = parallel
        self.batch_size = batch_size
        self.timeout = timeout
        self.report_interval = report_interval
        self.use_council = council_gate
        self.sync_cloud = sync_cloud

        # State
        self.best_ever = 0.0
        self.baseline_score = 0.0
        self.total_experiments = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_fails = 0
        self.round_num = 0
        self.wins_since_council = 0
        self.delta_since_council = 0.0
        self.session_start = time.time()
        self.reports: list[dict] = []

    def generate_report(self) -> str:
        """Generate a human-readable progress report."""
        elapsed = time.time() - self.session_start
        win_rate = self.total_wins / max(self.total_wins + self.total_losses, 1)
        gain = self.best_ever - self.baseline_score

        report = (
            f"\n{'=' * 60}\n"
            f"AUTORESEARCH REPORT | {datetime.now().strftime('%Y-%m-%d %H:%M CST')}\n"
            f"{'=' * 60}\n"
            f"Round:        {self.round_num}\n"
            f"Experiments:  {self.total_experiments}\n"
            f"Win rate:     {win_rate * 100:.1f}% ({self.total_wins}W / {self.total_losses}L / {self.total_fails}F)\n"
            f"Baseline:     {self.baseline_score:.4f}\n"
            f"Best ever:    {self.best_ever:.4f} (+{gain:.4f})\n"
            f"Elapsed:      {elapsed / 60:.1f} min\n"
            f"Speed:        {self.total_experiments / max(elapsed, 1) * 60:.0f} exp/min\n"
            f"Council sub:  {self.wins_since_council} wins pending\n"
            f"{'=' * 60}\n"
        )

        self.reports.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "round": self.round_num,
            "experiments": self.total_experiments,
            "win_rate": win_rate,
            "best_score": self.best_ever,
            "gain": gain,
        })

        return report

    def run(self):
        """Main continuous loop."""
        from liv_hana.loop import (
            _init_frozen_hashes, _log_experiment, run_experiment,
            run_parallel_loop, run_serial_loop,
        )

        log.info("RVOS CONTINUOUS MODE STARTING")
        log.info(f"   Strategy: {self.strategy}")
        log.info(f"   Parallel: {self.parallel}")
        log.info(f"   Batch size: {self.batch_size}")
        log.info(f"   Council gate: {self.use_council}")
        log.info(f"   Cloud sync: {self.sync_cloud}")

        _init_frozen_hashes()

        # Baseline
        log.info("Running baseline evaluation...")
        baseline = run_experiment("baseline", self.timeout)
        if baseline["status"] == "failed":
            log.error(f"Baseline failed: {baseline.get('error')}")
            sys.exit(1)

        self.baseline_score = baseline["score"]
        self.best_ever = self.baseline_score
        log.info(f"Baseline: {self.baseline_score:.4f}")

        _log_experiment({
            "experiment_id": "baseline",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "continuous_runner",
            "change_summary": "continuous mode baseline",
            "score_before": None, "score_after": self.baseline_score, "delta": 0.0,
            "ttfa_ms": baseline.get("ttfa_ms"), "ralph_pass": baseline.get("ralph_pass"),
            "status": "improved", "notes": "continuous mode start",
        })

        # Optional cloud sync
        if self.sync_cloud:
            self._sync_from_cloud()

        # Main loop
        while True:
            self.round_num += 1
            log.info(f"\n{'#' * 60}")
            log.info(f"ROUND {self.round_num} | Best: {self.best_ever:.4f} | Total: {self.total_experiments}")

            # Build args object for loop functions
            args = argparse.Namespace(
                experiments=self.batch_size,
                timeout=self.timeout,
                strategy=self.strategy,
                parallel=self.parallel,
            )

            # Run batch
            if self.parallel > 1:
                best, _, w, l, f = run_parallel_loop(args, self.best_ever)
            else:
                best, _, w, l, f = run_serial_loop(args, self.best_ever)

            # Update state
            round_improved = best > self.best_ever
            if round_improved:
                self.best_ever = best
                self.delta_since_council += best - self.best_ever

            self.total_wins += w
            self.total_losses += l
            self.total_fails += f
            self.total_experiments += self.batch_size
            self.wins_since_council += w

            # Progress report
            if self.total_experiments % self.report_interval < self.batch_size:
                report = self.generate_report()
                print(report)

            # Council gate check
            if self.use_council and self._should_submit_council():
                self._submit_to_council()

            # Cloud sync between rounds
            if self.sync_cloud and self.round_num % 5 == 0:
                self._sync_from_cloud()

            # Adaptive cooling
            if w == 0:
                cool_time = 30
                log.info(f"No wins — cooling {cool_time}s")
            elif w < 2:
                cool_time = 10
            else:
                cool_time = 2

            time.sleep(cool_time)

    def _should_submit_council(self) -> bool:
        """Check if we should submit to Council."""
        try:
            from liv_hana.council_gate import CouncilGate
            gate = CouncilGate()
            return gate.should_submit(self.wins_since_council, self.delta_since_council)
        except ImportError:
            return False

    def _submit_to_council(self):
        """Submit winning config to Council for review."""
        try:
            from liv_hana.council_gate import CouncilGate

            # Load current config
            sys.path.insert(0, str(MUTABLE_FILE.parent))
            import importlib
            import liv_hana.voice_optimizer as vo
            importlib.reload(vo)
            config = vo.get_config()

            gate = CouncilGate()
            win_rate = self.total_wins / max(self.total_wins + self.total_losses, 1)

            verdict = gate.submit_for_review(
                config=config,
                score=self.best_ever,
                delta=self.best_ever - self.baseline_score,
                baseline_score=self.baseline_score,
                experiments_run=self.total_experiments,
                win_rate=win_rate,
            )

            log.info(f"Council verdict: {verdict.status}")

            if verdict.status == "APPROVED":
                self._promote_to_dspy(config)
            elif verdict.status == "HITL_REQUIRED":
                log.warning("HITL REQUIRED — awaiting CEO decision")

            # Reset counters
            self.wins_since_council = 0
            self.delta_since_council = 0.0

        except Exception as e:
            log.error(f"Council submission failed: {e}")

    def _promote_to_dspy(self, config: dict):
        """Promote approved config to DSPy Brain."""
        try:
            from liv_hana.dspy_bridge import DSPyBridge
            bridge = DSPyBridge()
            bridge.promote_config(config, self.best_ever, "APPROVED")
            bridge.trigger_retrain(config, self.best_ever)
            log.info("Config promoted to DSPy Brain and retrain triggered")
        except Exception as e:
            log.error(f"DSPy promotion failed: {e}")

    def _sync_from_cloud(self):
        """Sync local config from cloud."""
        try:
            from liv_hana.dspy_bridge import DSPyBridge
            bridge = DSPyBridge()
            cloud_config = bridge.pull_latest_config(MUTABLE_FILE)
            if cloud_config:
                log.info(f"Synced {len(cloud_config)} params from cloud")
        except Exception as e:
            log.warning(f"Cloud sync failed (non-fatal): {e}")


def main():
    parser = argparse.ArgumentParser(description="RVOS Continuous Autonomous Runner")
    parser.add_argument("--strategy", choices=["random", "bayesian", "online"], default="bayesian")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--report-interval", type=int, default=50)
    parser.add_argument("--council-gate", action="store_true", default=False)
    parser.add_argument("--sync-cloud", action="store_true", default=False)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [RVOS] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    runner = ContinuousRunner(
        strategy=args.strategy,
        parallel=args.parallel,
        batch_size=args.batch_size,
        timeout=args.timeout,
        report_interval=args.report_interval,
        council_gate=args.council_gate,
        sync_cloud=args.sync_cloud,
    )
    runner.run()


if __name__ == "__main__":
    main()
