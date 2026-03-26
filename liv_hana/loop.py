"""
Liv Hana SI — Autonomous Research Loop v2 (RVOS)
Maps directly to Karpathy's autoresearch pattern + Bayesian Thompson Sampling + Parallel execution.

The ONLY job of this file: run the hypothesis→mutate→evaluate→keep/discard loop.
The agent modifies: voice_optimizer.py (the mutable genome)
The evaluator reads: evaluator.py (frozen scoring — NEVER modified by agent)

Usage:
    # Legacy serial random (backward compat)
    python liv_hana/loop.py --experiments 100 --timeout 300

    # Bayesian Thompson Sampling (default)
    python liv_hana/loop.py --experiments 100 --strategy bayesian

    # Parallel Bayesian on M4 Max (6 workers)
    python liv_hana/loop.py --experiments 100 --strategy bayesian --parallel 6

    # Continuous mode (runs forever)
    python liv_hana/loop.py --continuous --strategy bayesian --parallel 6

    # Online agent mode (Claude CLI)
    python liv_hana/loop.py --strategy online --experiments 20
"""

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LIV-LOOP] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("liv_hana.loop")

ROOT = Path(__file__).parent.parent

# Ensure autoresearch root is on sys.path for liv_hana imports
_root_str = str(ROOT)
if _root_str not in sys.path:
    sys.path.insert(0, _root_str)
MUTABLE_FILE = ROOT / "liv_hana" / "voice_optimizer.py"
EVALUATOR_FILE = ROOT / "liv_hana" / "evaluator.py"
PROGRAM_MD = ROOT / "program.md"
EXPERIMENTS_LOG = ROOT / "experiments_log.jsonl"
BACKUP_FILE = ROOT / "liv_hana" / "voice_optimizer.py.bak"

# Frozen file hashes — computed on first run, verified every experiment
# Any tampering halts the loop immediately (Sophia Gate)
_FROZEN_HASHES: dict[str, str] = {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _init_frozen_hashes():
    global _FROZEN_HASHES
    for f in [EVALUATOR_FILE, PROGRAM_MD]:
        if f.exists():
            _FROZEN_HASHES[str(f)] = _sha256(f)
            log.info(f"Frozen hash recorded: {f.name} = {_FROZEN_HASHES[str(f)][:12]}...")


def _verify_frozen_files():
    """Halt immediately if any frozen file has been modified."""
    for path_str, expected_hash in _FROZEN_HASHES.items():
        actual = _sha256(Path(path_str))
        if actual != expected_hash:
            log.critical(f"SOPHIA GATE: Frozen file tampered: {path_str}")
            log.critical(f"Expected: {expected_hash[:12]}... Got: {actual[:12]}...")
            sys.exit(99)


def _ast_security_scan(code: str) -> list[str]:
    """AST-level firewall — scans proposed code for banned patterns."""
    violations = []
    BANNED_IMPORTS = {"os", "subprocess", "socket", "requests", "urllib", "shutil", "ftplib", "smtplib"}
    BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open"}

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        violations.append(f"SYNTAX_ERROR: {e}")
        return violations

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                root_module = (name or "").split(".")[0]
                if root_module in BANNED_IMPORTS:
                    violations.append(f"BANNED_IMPORT: {name}")
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name in BANNED_CALLS:
                violations.append(f"BANNED_CALL: {func_name}()")

    BANNED_STRINGS = ["/etc/", "passwd", "authorized_keys", "id_rsa", "AWS_SECRET", "API_KEY"]
    for bs in BANNED_STRINGS:
        if bs.lower() in code.lower():
            violations.append(f"BANNED_STRING: {bs}")

    return violations


def _log_experiment(exp: dict):
    with open(EXPERIMENTS_LOG, "a") as f:
        f.write(json.dumps(exp) + "\n")


def run_experiment(exp_id: str, timeout: int) -> dict:
    """Run a single evaluation experiment against the current voice_optimizer.py."""
    log.info(f"Running experiment {exp_id} (timeout={timeout}s)")
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(EVALUATOR_FILE)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
            env={**os.environ, "LIV_HANA_EVAL_MODE": "1"},
        )
        elapsed = time.time() - t0
        stdout = result.stdout

        score = None
        ttfa_ms = None
        ralph_pass = None
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

        if result.returncode != 0:
            return {"status": "failed", "error": result.stderr[:200], "elapsed_s": elapsed}
        if score is None:
            return {"status": "failed", "error": "no_score_line", "elapsed_s": elapsed}

        return {
            "status": "evaluated",
            "score": score,
            "ttfa_ms": ttfa_ms,
            "ralph_pass": ralph_pass,
            "elapsed_s": elapsed,
            "stdout": stdout[-500:],
        }

    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "timeout", "elapsed_s": timeout}
    except Exception as e:
        return {"status": "failed", "error": str(e), "elapsed_s": time.time() - t0}


def propose_mutation_random(exp_id: str) -> tuple[str | None, dict]:
    """Legacy random mutation (backward compat)."""
    current = MUTABLE_FILE.read_text()
    import random as _rng

    PARAMS = [
        ("BARGE_IN_THRESHOLD", float, 0.010, 0.150, 0.005),
        ("SILENCE_TIMEOUT_MS", int, 200, 1200, 50),
        ("REDEMPTION_FRAMES", int, 2, 20, 1),
        ("PAUSE_TOLERANCE_MS", int, 100, 800, 50),
        ("TEMPERATURE", float, 0.0, 1.2, 0.05),
        ("TOP_P", float, 0.5, 1.0, 0.05),
        ("STREAM_CHUNK_TOKENS", int, 1, 10, 1),
        ("MAX_TOKENS", int, 50, 400, 25),
        ("DB_POOL_SIZE", int, 2, 50, 5),
        ("HTTP_TIMEOUT_MS", int, 2000, 15000, 500),
        ("JWT_CACHE_TTL_S", int, 60, 600, 30),
    ]

    param_name, ptype, lo, hi, step = _rng.choice(PARAMS)
    pattern = rf"({param_name})\s*(?::\s*\w+\s*)?\s*=\s*([0-9.]+)"
    m = re.search(pattern, current)
    if m:
        current_val = ptype(float(m.group(2)))
        direction = _rng.choice([-1, 1])
        if ptype == float:
            new_val = round(max(lo, min(hi, current_val + direction * step)), 4)
            val_str = f"{new_val:.3f}"
        else:
            new_val = max(int(lo), min(int(hi), int(current_val + direction * step)))
            val_str = str(new_val)
        new_code = current[:m.start()] + m.group(0)[:m.start(2) - m.start()] + val_str + current[m.end(2):]
        metadata = {"strategy": "random", "mutations": [{"param": param_name, "old": current_val, "new": new_val}]}
        return new_code, metadata
    return None, {"error": f"param_not_found: {param_name}"}


def propose_mutation_online(exp_id: str, score_history: list[float]) -> tuple[str | None, dict]:
    """Online agent mutation via Claude CLI."""
    agent_cmd = os.environ.get("LIV_HANA_AGENT_CMD", "claude")
    current_code = MUTABLE_FILE.read_text()
    prompt = f"""You are the Mayor of Optimization for Liv Hana SI.
Propose ONE bounded change to voice_optimizer.py to improve the composite SCORE.
Score formula: TTFA(40%) + RALPH(35%) + Barge-In(15%) + TokenVelocity(10%)
Score history (last 10): {score_history[-10:]}
Rules: Change ONE parameter, stay within bounds, NO new imports, return ONLY the complete file.
Current file:
```python
{current_code}
```"""

    try:
        result = subprocess.run(
            [agent_cmd, "--print", prompt],
            capture_output=True, text=True, timeout=180,
            cwd=str(ROOT),
            env={**os.environ, "CLAUDE_MODEL": "haiku"}
        )
        if result.returncode == 0 and result.stdout.strip():
            code = result.stdout
            m = re.search(r"```python\n(.+?)```", code, re.DOTALL)
            if m:
                code = m.group(1)
            if "VoiceOptimizerConfig" in code and "validate_bounds" in code:
                return code, {"strategy": "online_agent", "agent": agent_cmd}
    except FileNotFoundError:
        log.warning(f"Agent CLI '{agent_cmd}' not found")
    except subprocess.TimeoutExpired:
        log.warning("Agent CLI timed out")
    except Exception as e:
        log.warning(f"Agent call failed: {e}")
    return None, {"strategy": "online_agent", "error": "agent_failed"}


def run_serial_loop(args, baseline_score: float):
    """Original serial experiment loop with strategy selection."""
    from liv_hana.mutation_strategy import BayesianMutator, HistoryAnalyzer, MetaOptimizer, MetaParams

    best_score = baseline_score
    score_history = [baseline_score]
    wins = 0
    losses = 0
    fails = 0

    # Initialize Bayesian strategy
    analyzer = HistoryAnalyzer(EXPERIMENTS_LOG)
    analyzer.load()
    meta = MetaParams()
    mutator = BayesianMutator(analyzer, meta)
    meta_optimizer = MetaOptimizer(meta, interval=50)

    for i in range(1, args.experiments + 1):
        exp_id = f"exp_{i:04d}"
        log.info(f"\n{'=' * 60}")
        log.info(f"EXPERIMENT {exp_id} | Best: {best_score:.4f} | W:{wins} L:{losses} F:{fails} | Strategy: {args.strategy}")

        _verify_frozen_files()
        shutil.copy2(MUTABLE_FILE, BACKUP_FILE)

        # Propose mutation based on strategy
        mutation_metadata = {}
        if args.strategy == "bayesian":
            proposed_code, mutation_metadata = mutator.propose(MUTABLE_FILE)
        elif args.strategy == "online":
            proposed_code, mutation_metadata = propose_mutation_online(exp_id, score_history)
        else:  # random
            proposed_code, mutation_metadata = propose_mutation_random(exp_id)

        if proposed_code is None:
            log.warning(f"No mutation proposed for {exp_id}, skipping")
            fails += 1
            continue

        # AST security scan
        violations = _ast_security_scan(proposed_code)
        if violations:
            log.warning(f"SECURITY GATE BLOCKED {exp_id}: {violations}")
            _log_experiment({
                "experiment_id": exp_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "security_gate",
                "change_summary": "BLOCKED by AST security scan",
                "score_before": best_score, "score_after": None, "delta": None,
                "ttfa_ms": None, "ralph_pass": None,
                "status": "blocked", "notes": str(violations),
                "mutation": mutation_metadata,
            })
            fails += 1
            continue

        MUTABLE_FILE.write_text(proposed_code)
        result = run_experiment(exp_id, args.timeout)

        if result["status"] == "failed":
            log.warning(f"{exp_id} FAILED: {result.get('error')} — reverting")
            shutil.copy2(BACKUP_FILE, MUTABLE_FILE)
            fails += 1
            _log_experiment({
                "experiment_id": exp_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": args.strategy,
                "change_summary": "evaluation failed",
                "score_before": best_score, "score_after": None, "delta": None,
                "ttfa_ms": None, "ralph_pass": None,
                "status": "failed", "notes": result.get("error", ""),
                "mutation": mutation_metadata,
            })
            continue

        new_score = result["score"]
        delta = new_score - best_score
        ralph_ok = result.get("ralph_pass", True)

        if not ralph_ok:
            log.warning(f"{exp_id} RALPH VIOLATION — reverting")
            shutil.copy2(BACKUP_FILE, MUTABLE_FILE)
            status = "ralph_violation"
            losses += 1
        elif new_score > best_score:
            log.info(f"IMPROVED: {best_score:.4f} -> {new_score:.4f} (+{delta:.4f}) — KEEPING")
            best_score = new_score
            score_history.append(new_score)
            wins += 1
            status = "improved"
        else:
            log.info(f"REGRESSED: {best_score:.4f} -> {new_score:.4f} ({delta:.4f}) — REVERTING")
            shutil.copy2(BACKUP_FILE, MUTABLE_FILE)
            score_history.append(new_score)
            losses += 1
            status = "regressed"

        _log_experiment({
            "experiment_id": exp_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": args.strategy,
            "change_summary": f"mutation attempt {i}",
            "score_before": best_score if status != "improved" else best_score - delta,
            "score_after": new_score,
            "delta": delta,
            "ttfa_ms": result.get("ttfa_ms"),
            "ralph_pass": ralph_ok,
            "status": status,
            "notes": result.get("stdout", "")[-200:],
            "mutation": mutation_metadata,
        })

        # Meta-optimization check
        if args.strategy == "bayesian" and meta_optimizer.should_optimize(i):
            analyzer.load()  # Reload fresh history
            meta_result = meta_optimizer.optimize(analyzer)
            if meta_result:
                _log_experiment({
                    "experiment_id": f"meta_{i:04d}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent": "meta_optimizer",
                    "change_summary": "meta-parameter adjustment",
                    "score_before": best_score, "score_after": best_score,
                    "delta": 0, "ttfa_ms": None, "ralph_pass": None,
                    "status": "meta_mutation",
                    "notes": json.dumps(meta_result),
                })

    return best_score, baseline_score, wins, losses, fails


def run_parallel_loop(args, baseline_score: float):
    """Parallel experiment loop using ProcessPoolExecutor + Bayesian mutations."""
    from liv_hana.mutation_strategy import BayesianMutator, HistoryAnalyzer, MetaOptimizer, MetaParams
    from liv_hana.parallel_runner import ParallelExperimentRunner

    best_score = baseline_score
    wins = 0
    losses = 0
    fails = 0
    total_experiments = 0

    analyzer = HistoryAnalyzer(EXPERIMENTS_LOG)
    analyzer.load()
    meta = MetaParams()
    mutator = BayesianMutator(analyzer, meta)
    meta_optimizer = MetaOptimizer(meta, interval=50)
    runner = ParallelExperimentRunner(workers=args.parallel, timeout=args.timeout)

    batch_size = args.parallel
    num_batches = (args.experiments + batch_size - 1) // batch_size

    for batch_idx in range(num_batches):
        remaining = args.experiments - total_experiments
        if remaining <= 0:
            break
        current_batch_size = min(batch_size, remaining)

        log.info(f"\n{'=' * 60}")
        log.info(
            f"BATCH {batch_idx + 1}/{num_batches} | {current_batch_size} parallel experiments | "
            f"Best: {best_score:.4f} | W:{wins} L:{losses} F:{fails}"
        )

        _verify_frozen_files()

        # Propose N mutations
        proposals = []
        for j in range(current_batch_size):
            exp_id = f"exp_{total_experiments + j + 1:04d}"
            proposed_code, metadata = mutator.propose(MUTABLE_FILE)
            if proposed_code is None:
                fails += 1
                continue

            violations = _ast_security_scan(proposed_code)
            if violations:
                log.warning(f"SECURITY GATE BLOCKED {exp_id}: {violations}")
                _log_experiment({
                    "experiment_id": exp_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent": "security_gate",
                    "change_summary": "BLOCKED", "score_before": best_score,
                    "score_after": None, "delta": None,
                    "ttfa_ms": None, "ralph_pass": None,
                    "status": "blocked", "notes": str(violations),
                })
                fails += 1
                continue

            proposals.append((exp_id, proposed_code, metadata))

        if not proposals:
            total_experiments += current_batch_size
            continue

        # Run batch in parallel
        results = runner.run_batch(proposals, best_score)

        # Log all results
        for r in results:
            _log_experiment({
                "experiment_id": r.exp_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "bayesian_parallel",
                "change_summary": f"parallel mutation",
                "score_before": best_score if r.status != "improved" else (best_score - (r.delta or 0)),
                "score_after": r.score,
                "delta": r.delta,
                "ttfa_ms": r.ttfa_ms,
                "ralph_pass": r.ralph_pass,
                "status": r.status,
                "notes": json.dumps(r.mutation_metadata) if r.mutation_metadata else "",
                "mutation": r.mutation_metadata,
            })

            if r.status == "improved":
                wins += 1
            elif r.status in ("regressed", "ralph_violation"):
                losses += 1
            else:
                fails += 1

        # Promote best winner
        winner = runner.pick_best_winner(results)
        if winner:
            if runner.promote_winner(winner):
                best_score = winner.score
                log.info(f"BATCH WINNER: {winner.exp_id} score={winner.score:.4f} delta=+{winner.delta:.4f}")

        total_experiments += current_batch_size

        # Meta-optimization
        if meta_optimizer.should_optimize(total_experiments):
            analyzer.load()
            meta_result = meta_optimizer.optimize(analyzer)
            if meta_result:
                log.info(f"META-OPTIMIZATION: {json.dumps(meta_result)}")
                _log_experiment({
                    "experiment_id": f"meta_{total_experiments:04d}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent": "meta_optimizer",
                    "change_summary": "meta-parameter adjustment",
                    "score_before": best_score, "score_after": best_score,
                    "delta": 0, "status": "meta_mutation",
                    "notes": json.dumps(meta_result),
                })

    return best_score, baseline_score, wins, losses, fails


def main():
    parser = argparse.ArgumentParser(description="Liv Hana Autonomous Research Loop v2 (RVOS)")
    parser.add_argument("--experiments", type=int, default=100, help="Max experiments to run")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per experiment")
    parser.add_argument("--strategy", choices=["random", "bayesian", "online"], default="bayesian",
                        help="Mutation strategy (default: bayesian)")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Number of parallel workers (default: 1 = serial)")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuously (loop forever)")
    parser.add_argument("--report-interval", type=int, default=50,
                        help="Print summary every N experiments")
    parser.add_argument("--offline", action="store_true", help="Force offline mode (legacy)")
    args = parser.parse_args()

    if args.offline:
        os.environ["LIV_HANA_OFFLINE"] = "1"

    log.info("RVOS — Recursive Voice Optimization System v2")
    log.info(f"   Strategy: {args.strategy}")
    log.info(f"   Parallel workers: {args.parallel}")
    log.info(f"   Max experiments: {'CONTINUOUS' if args.continuous else args.experiments}")
    log.info(f"   Timeout per run: {args.timeout}s")

    _init_frozen_hashes()

    # Get baseline score
    log.info("Running baseline evaluation...")
    baseline = run_experiment("baseline", args.timeout)
    if baseline["status"] == "failed":
        log.error(f"Baseline failed: {baseline.get('error')}. Fix evaluator.py first.")
        sys.exit(1)

    baseline_score = baseline["score"]
    log.info(f"Baseline SCORE: {baseline_score:.4f} | TTFA: {baseline.get('ttfa_ms')}ms | RALPH: {baseline.get('ralph_pass')}")

    _log_experiment({
        "experiment_id": "baseline",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "baseline",
        "change_summary": "initial baseline",
        "score_before": None, "score_after": baseline_score, "delta": 0.0,
        "ttfa_ms": baseline.get("ttfa_ms"), "ralph_pass": baseline.get("ralph_pass"),
        "status": "improved", "notes": "initial baseline run",
    })

    session_start = time.time()

    if args.continuous:
        # Continuous mode: loop forever
        round_num = 0
        cumulative_wins = 0
        cumulative_losses = 0
        cumulative_fails = 0
        best_ever = baseline_score

        while True:
            round_num += 1
            log.info(f"\n{'#' * 60}")
            log.info(f"CONTINUOUS ROUND {round_num} | Best ever: {best_ever:.4f}")

            if args.parallel > 1:
                best, _, w, l, f = run_parallel_loop(args, best_ever)
            else:
                best, _, w, l, f = run_serial_loop(args, best_ever)

            if best > best_ever:
                best_ever = best
            cumulative_wins += w
            cumulative_losses += l
            cumulative_fails += f

            elapsed = time.time() - session_start
            log.info(f"\nROUND {round_num} COMPLETE | Best: {best_ever:.4f} | "
                     f"Total W:{cumulative_wins} L:{cumulative_losses} F:{cumulative_fails} | "
                     f"Elapsed: {elapsed / 60:.1f}min")

            # Cooling: if no wins this round, pause briefly
            if w == 0:
                log.info("No improvements this round — cooling 10s before next round")
                time.sleep(10)
    else:
        # Single run
        if args.parallel > 1:
            best, base, wins, losses, fails = run_parallel_loop(args, baseline_score)
        else:
            best, base, wins, losses, fails = run_serial_loop(args, baseline_score)

        elapsed = time.time() - session_start
        log.info(f"\nRESEARCH SESSION COMPLETE")
        log.info(f"   Baseline score: {base:.4f}")
        log.info(f"   Best score:     {best:.4f}")
        log.info(f"   Total gain:     {best - base:+.4f}")
        log.info(f"   Win rate:       {wins / max(wins + losses, 1) * 100:.1f}%")
        log.info(f"   Wins: {wins} | Losses: {losses} | Fails: {fails}")
        log.info(f"   Elapsed: {elapsed:.1f}s")
        log.info(f"   Results in: {EXPERIMENTS_LOG}")


if __name__ == "__main__":
    main()
