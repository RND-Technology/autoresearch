"""
Liv Hana SI — Autonomous Research Loop
Maps directly to Karpathy's autoresearch pattern.

The ONLY job of this file: run the hypothesis→mutate→evaluate→keep/discard loop forever.
This file is NOT modified by the agent. It is the arena runner.
The agent modifies: voice_optimizer.py (the mutable genome)
The evaluator reads: evaluator.py (frozen scoring — NEVER modified by agent)

Usage:
    python liv_hana/loop.py --experiments 100 --timeout 300
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
            log.info(f"🔒 Frozen hash recorded: {f.name} = {_FROZEN_HASHES[str(f)][:12]}...")


def _verify_frozen_files():
    """Halt immediately if any frozen file has been modified."""
    for path_str, expected_hash in _FROZEN_HASHES.items():
        actual = _sha256(Path(path_str))
        if actual != expected_hash:
            log.critical(f"🚨 SOPHIA GATE: Frozen file tampered: {path_str}")
            log.critical(f"Expected: {expected_hash[:12]}... Got: {actual[:12]}...")
            sys.exit(99)


def _ast_security_scan(code: str) -> list[str]:
    """
    AST-level firewall — scans proposed code for banned patterns.
    Returns list of violations. Empty list = PASS.
    """
    violations = []
    BANNED_IMPORTS = {"os", "subprocess", "socket", "requests", "urllib", "shutil", "ftplib", "smtplib"}
    BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open"}

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        violations.append(f"SYNTAX_ERROR: {e}")
        return violations

    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                root_module = (name or "").split(".")[0]
                if root_module in BANNED_IMPORTS:
                    violations.append(f"BANNED_IMPORT: {name}")
        # Check function calls
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name in BANNED_CALLS:
                violations.append(f"BANNED_CALL: {func_name}()")

    # String-level checks for things AST misses
    BANNED_STRINGS = ["/etc/", "passwd", "authorized_keys", "id_rsa", "AWS_SECRET", "API_KEY"]
    for bs in BANNED_STRINGS:
        if bs.lower() in code.lower():
            violations.append(f"BANNED_STRING: {bs}")

    return violations


def _log_experiment(exp: dict):
    with open(EXPERIMENTS_LOG, "a") as f:
        f.write(json.dumps(exp) + "\n")


def _get_last_score() -> float | None:
    """Read the last improved experiment's score from log."""
    if not EXPERIMENTS_LOG.exists():
        return None
    improved = []
    with open(EXPERIMENTS_LOG) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            try:
                e = json.loads(line)
                if e.get("status") == "improved":
                    improved.append(e)
            except json.JSONDecodeError:
                pass
    if improved:
        return improved[-1].get("score_after")
    return None


def run_experiment(exp_id: str, timeout: int) -> dict:
    """Run a single evaluation experiment against the current voice_optimizer.py."""
    log.info(f"🧪 Running experiment {exp_id} (timeout={timeout}s)")
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

        # Parse SCORE line from evaluator output
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
            log.warning(f"Evaluator exit code {result.returncode}: {result.stderr[:200]}")
            return {"status": "failed", "error": result.stderr[:200], "elapsed_s": elapsed}

        if score is None:
            log.warning(f"No SCORE line found in evaluator output")
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
        log.warning(f"Experiment {exp_id} timed out after {timeout}s")
        return {"status": "failed", "error": "timeout", "elapsed_s": timeout}
    except Exception as e:
        return {"status": "failed", "error": str(e), "elapsed_s": time.time() - t0}


def propose_mutation(exp_id: str, score_history: list[float]) -> str | None:
    """
    Ask the agent (Claude/Codex via CLI) to propose a mutation to voice_optimizer.py.
    Returns the proposed new file content, or None if proposal failed.

    In offline/synthetic mode (no agent), applies a simple deterministic mutation
    to validate the loop plumbing.
    """
    offline_mode = os.environ.get("LIV_HANA_OFFLINE", "1") == "1"

    if offline_mode:
        log.info(f"📦 OFFLINE MODE: Applying synthetic mutation for {exp_id}")
        current = MUTABLE_FILE.read_text()
        import re, random as _rng

        # All mutable params with their bounds and type-annotated regex patterns
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

        # Pick a random param to mutate (explore all dimensions)
        param_name, ptype, lo, hi, step = _rng.choice(PARAMS)
        # Match with optional type annotation: NAME: type = VALUE or NAME = VALUE
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
            # Replace the full match preserving type annotation
            new_code = current[:m.start()] + m.group(0)[:m.start(2)-m.start()] + val_str + current[m.end(2):]
            log.info(f"   {param_name}: {current_val} → {new_val} (step={direction*step})")
            return new_code
        log.warning(f"   Could not find {param_name} in voice_optimizer.py")
        return None

    # Online mode: invoke Claude Code CLI as the mutation agent
    agent_cmd = os.environ.get("LIV_HANA_AGENT_CMD", "claude")
    current_code = MUTABLE_FILE.read_text()
    prompt = f"""You are the Mayor of Optimization for Liv Hana SI.
Your job: propose ONE bounded change to voice_optimizer.py to improve the composite SCORE metric.
Score formula: TTFA(40%) + RALPH(35%) + Barge-In(15%) + TokenVelocity(10%)

Current score history (last 10): {score_history[-10:]}

Mutable parameters and bounds:
- BARGE_IN_THRESHOLD [0.010, 0.150] — sweet spot ~0.045
- SILENCE_TIMEOUT_MS [200, 1200]
- REDEMPTION_FRAMES [2, 20] — sweet spot ~8
- PAUSE_TOLERANCE_MS [100, 800]
- TEMPERATURE [0.0, 1.2] — >1.2 = RALPH violation
- TOP_P [0.5, 1.0]
- STREAM_CHUNK_TOKENS [1, 10] — lower = faster TTFA
- MAX_TOKENS [50, 400]
- DB_POOL_SIZE [2, 50] — diminishing returns >20
- HTTP_TIMEOUT_MS [2000, 15000]
- JWT_CACHE_TTL_S [60, 600]

Rules:
- Change EXACTLY ONE parameter per experiment
- Stay within bounds
- NO new imports (os, subprocess, socket, requests, urllib)
- NO eval(), exec(), open() calls
- Return ONLY the complete new voice_optimizer.py content, nothing else

Current file:
```python
{current_code}
```"""

    try:
        import re
        result = subprocess.run(
            [agent_cmd, "--print", prompt],
            capture_output=True, text=True, timeout=180,
            cwd=str(ROOT),
            env={**os.environ, "CLAUDE_MODEL": "haiku"}
        )
        if result.returncode == 0 and result.stdout.strip():
            code = result.stdout
            # Extract code block if wrapped in ```python ... ```
            m = re.search(r"```python\n(.+?)```", code, re.DOTALL)
            if m:
                code = m.group(1)
            # Basic sanity: must contain the dataclass and validate_bounds
            if "VoiceOptimizerConfig" in code and "validate_bounds" in code:
                return code
            log.warning("Agent output missing required structures, discarding")
    except FileNotFoundError:
        log.warning(f"Agent CLI '{agent_cmd}' not found — falling back to offline mode")
        os.environ["LIV_HANA_OFFLINE"] = "1"
        return propose_mutation(exp_id, score_history)
    except subprocess.TimeoutExpired:
        log.warning("Agent CLI timed out after 180s")
    except Exception as e:
        log.warning(f"Agent call failed: {e}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Liv Hana Autonomous Research Loop")
    parser.add_argument("--experiments", type=int, default=100, help="Max experiments to run")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per experiment")
    parser.add_argument("--offline", action="store_true", help="Run in offline/synthetic mode")
    args = parser.parse_args()

    if args.offline:
        os.environ["LIV_HANA_OFFLINE"] = "1"

    log.info("🚀 LIV HANA AUTONOMOUS RESEARCH LOOP STARTING")
    log.info(f"   Max experiments: {args.experiments}")
    log.info(f"   Timeout per run: {args.timeout}s")
    log.info(f"   Mode: {'OFFLINE/SYNTHETIC' if os.environ.get('LIV_HANA_OFFLINE','1')=='1' else 'ONLINE/AGENT'}")

    _init_frozen_hashes()

    # Get baseline score
    log.info("📏 Running baseline evaluation...")
    baseline = run_experiment("baseline", args.timeout)
    if baseline["status"] == "failed":
        log.error(f"❌ Baseline failed: {baseline.get('error')}. Fix evaluator.py first.")
        sys.exit(1)

    baseline_score = baseline["score"]
    best_score = baseline_score
    score_history = [baseline_score]
    wins = 0
    losses = 0
    fails = 0

    log.info(f"📊 Baseline SCORE: {baseline_score:.4f} | TTFA: {baseline.get('ttfa_ms')}ms | RALPH: {baseline.get('ralph_pass')}")

    _log_experiment({
        "experiment_id": "baseline",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "baseline",
        "change_summary": "initial baseline",
        "score_before": None,
        "score_after": baseline_score,
        "delta": 0.0,
        "ttfa_ms": baseline.get("ttfa_ms"),
        "ralph_pass": baseline.get("ralph_pass"),
        "status": "improved",
        "notes": "initial baseline run",
    })

    for i in range(1, args.experiments + 1):
        exp_id = f"exp_{i:04d}"
        log.info(f"\n{'='*60}")
        log.info(f"🔬 EXPERIMENT {exp_id} | Best score: {best_score:.4f} | Wins: {wins} | Losses: {losses} | Fails: {fails}")

        # Verify frozen files haven't been tampered with
        _verify_frozen_files()

        # Backup current mutable file
        shutil.copy2(MUTABLE_FILE, BACKUP_FILE)

        # Propose mutation
        proposed_code = propose_mutation(exp_id, score_history)
        if proposed_code is None:
            log.warning(f"⚠️  No mutation proposed for {exp_id}, skipping")
            fails += 1
            continue

        # AST security scan
        violations = _ast_security_scan(proposed_code)
        if violations:
            log.warning(f"🚨 SECURITY GATE BLOCKED {exp_id}: {violations}")
            _log_experiment({
                "experiment_id": exp_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "security_gate",
                "change_summary": "BLOCKED by AST security scan",
                "score_before": best_score,
                "score_after": None,
                "delta": None,
                "ttfa_ms": None,
                "ralph_pass": None,
                "status": "blocked",
                "notes": str(violations),
            })
            fails += 1
            continue

        # Write proposed mutation
        MUTABLE_FILE.write_text(proposed_code)

        # Run evaluation
        result = run_experiment(exp_id, args.timeout)

        if result["status"] == "failed":
            log.warning(f"❌ {exp_id} FAILED: {result.get('error')} — reverting")
            shutil.copy2(BACKUP_FILE, MUTABLE_FILE)
            fails += 1
            _log_experiment({
                "experiment_id": exp_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": os.environ.get("LIV_HANA_AGENT_CMD", "offline"),
                "change_summary": "evaluation failed",
                "score_before": best_score,
                "score_after": None,
                "delta": None,
                "ttfa_ms": None,
                "ralph_pass": None,
                "status": "failed",
                "notes": result.get("error", ""),
            })
            continue

        new_score = result["score"]
        delta = new_score - best_score
        ralph_ok = result.get("ralph_pass", True)

        # RALPH is a hard constraint — NEVER sacrifice compliance for speed
        if not ralph_ok:
            log.warning(f"🚨 {exp_id} RALPH VIOLATION — reverting regardless of score")
            shutil.copy2(BACKUP_FILE, MUTABLE_FILE)
            status = "ralph_violation"
            losses += 1
        elif new_score > best_score:
            # Higher score = better (SCORE is a composite of latency + accuracy)
            log.info(f"✅ {exp_id} IMPROVED: {best_score:.4f} → {new_score:.4f} (Δ+{delta:.4f}) — KEEPING")
            best_score = new_score
            score_history.append(new_score)
            wins += 1
            status = "improved"
        else:
            log.info(f"📉 {exp_id} REGRESSED: {best_score:.4f} → {new_score:.4f} (Δ{delta:.4f}) — REVERTING")
            shutil.copy2(BACKUP_FILE, MUTABLE_FILE)
            score_history.append(new_score)
            losses += 1
            status = "regressed"

        _log_experiment({
            "experiment_id": exp_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": os.environ.get("LIV_HANA_AGENT_CMD", "offline"),
            "change_summary": f"mutation attempt {i}",
            "score_before": best_score if status != "improved" else best_score - delta,
            "score_after": new_score,
            "delta": delta,
            "ttfa_ms": result.get("ttfa_ms"),
            "ralph_pass": ralph_ok,
            "status": status,
            "notes": result.get("stdout", "")[-200:],
        })

    log.info(f"\n🏁 RESEARCH SESSION COMPLETE")
    log.info(f"   Baseline score: {baseline_score:.4f}")
    log.info(f"   Best score:     {best_score:.4f}")
    log.info(f"   Total gain:     {best_score - baseline_score:+.4f}")
    log.info(f"   Wins: {wins} | Losses: {losses} | Fails: {fails}")
    log.info(f"   Results in: {EXPERIMENTS_LOG}")
    log.info("🌿 People → Plant → Profit")


if __name__ == "__main__":
    main()
