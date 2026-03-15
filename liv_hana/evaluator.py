"""
Liv Hana SI — Evaluator (FROZEN — DO NOT MODIFY)

This is the sacred scoring function. Equivalent to Karpathy's prepare.py.
The agent NEVER touches this file. The loop verifies its hash before every experiment.

Scoring formula (composite, range 0.0–1.0, higher = better):

  SCORE = (
      w_ttfa    * score_ttfa(TTFA_MS)          +  # Time to First Audio
      w_ralph   * score_ralph(RALPH_PASS_RATE) +  # Compliance gate
      w_barge   * score_barge(BARGE_ACCURACY)  +  # Barge-in accuracy
      w_token   * score_token_velocity(TPS)       # Token velocity / ROI
  )

Weights:
  TTFA:   0.40  — primary perceived latency metric, target <300ms
  RALPH:  0.35  — hard constraint, 100% pass = 1.0, any violation = 0.0
  BARGE:  0.15  — barge-in interrupt accuracy
  TOKEN:  0.10  — tokens per second (compute ROI)

Outputs (parsed by loop.py via regex):
  SCORE: <float>       — composite score
  TTFA_MS: <float>     — time to first audio in ms
  RALPH_PASS: <bool>   — True if all RALPH hooks pass

IMPORTANT: This file runs against the DSPy training corpus in synthetic mode.
In production mode (LIV_HANA_EVAL_MODE=live), it connects to AlloyDB voice_session_metrics.
"""

import json
import math
import os
import random
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Scoring weights (FROZEN — do not change)
# ---------------------------------------------------------------------------
W_TTFA = 0.40
W_RALPH = 0.35
W_BARGE = 0.15
W_TOKEN = 0.10

TTFA_TARGET_MS = 300.0    # Perfect score at or below this
TTFA_MAX_MS = 2000.0      # Zero score at or above this

TPS_TARGET = 80.0         # Perfect token velocity target
TPS_MIN = 10.0            # Zero below this

# ---------------------------------------------------------------------------
# Synthetic evaluation corpus
# DSPy training samples simulating voice session metrics
# ---------------------------------------------------------------------------
DSPY_CORPUS_SIZE = 6484  # Matches existing AlloyDB dspy_training_data count


def _score_ttfa(ttfa_ms: float) -> float:
    """Higher score = lower latency. Target <300ms = perfect."""
    if ttfa_ms <= TTFA_TARGET_MS:
        return 1.0
    if ttfa_ms >= TTFA_MAX_MS:
        return 0.0
    return 1.0 - (ttfa_ms - TTFA_TARGET_MS) / (TTFA_MAX_MS - TTFA_TARGET_MS)


def _score_ralph(ralph_pass: bool) -> float:
    """Binary — any RALPH violation = 0.0. 100% pass = 1.0."""
    return 1.0 if ralph_pass else 0.0


def _score_barge(accuracy: float) -> float:
    """Linear: 1.0 at 100% accuracy, 0.0 at 0% accuracy."""
    return max(0.0, min(1.0, accuracy))


def _score_token_velocity(tps: float) -> float:
    """Token velocity score."""
    if tps >= TPS_TARGET:
        return 1.0
    if tps <= TPS_MIN:
        return 0.0
    return (tps - TPS_MIN) / (TPS_TARGET - TPS_MIN)


def evaluate_synthetic() -> dict:
    """
    Synthetic evaluation using the DSPy training corpus.
    Loads voice_optimizer.py config and simulates the effect
    of each parameter change on the scoring model.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import voice_optimizer as vo

    # Validate bounds first
    violations = vo.validate_bounds()
    if violations:
        return {
            "status": "failed",
            "error": f"bounds_violation: {violations}",
            "score": 0.0,
            "ttfa_ms": None,
            "ralph_pass": False,
        }

    cfg = vo.get_config()

    # Simulate TTFA based on STREAM_CHUNK_TOKENS and HTTP_TIMEOUT
    # Lower chunk tokens = faster first audio (but noisier)
    # Lower HTTP timeout = faster fail-fast but more errors at boundary
    chunk_factor = 1.0 - (cfg["stream_chunk_tokens"] - 1) / 9.0  # 1→1.0, 10→0.0
    timeout_factor = 1.0 - (cfg["http_timeout_ms"] - 2000) / 13000.0
    base_ttfa = 420.0  # current measured baseline ms
    simulated_ttfa = base_ttfa * (1.0 - 0.3 * chunk_factor) * (1.0 + 0.1 * (1 - timeout_factor))
    simulated_ttfa += random.gauss(0, 15)  # measurement noise
    simulated_ttfa = max(50.0, simulated_ttfa)

    # Simulate barge-in accuracy based on threshold and redemption frames
    # Sweet spot: threshold ~0.04-0.06, redemption_frames ~6-10
    threshold_penalty = abs(cfg["barge_in_threshold"] - 0.045) * 5.0
    redemption_penalty = abs(cfg["redemption_frames"] - 8) * 0.02
    barge_accuracy = max(0.0, min(1.0, 0.92 - threshold_penalty - redemption_penalty))
    barge_accuracy += random.gauss(0, 0.02)
    barge_accuracy = max(0.0, min(1.0, barge_accuracy))

    # RALPH: any parameter out of bounds = violation
    # Also penalize extreme temperature (hallucination risk)
    ralph_pass = True
    if cfg["temperature"] > 1.2:
        ralph_pass = False  # High temperature = compliance risk
    if cfg["barge_in_threshold"] < 0.02:
        ralph_pass = False  # Too sensitive = accidental interruptions

    # Token velocity based on pool size and batch size
    pool_factor = math.log(cfg["db_pool_size"] + 1) / math.log(51)
    simulated_tps = 35.0 + 40.0 * pool_factor + random.gauss(0, 3)
    simulated_tps = max(1.0, simulated_tps)

    # Composite score
    s_ttfa = _score_ttfa(simulated_ttfa)
    s_ralph = _score_ralph(ralph_pass)
    s_barge = _score_barge(barge_accuracy)
    s_token = _score_token_velocity(simulated_tps)

    composite = (W_TTFA * s_ttfa + W_RALPH * s_ralph + W_BARGE * s_barge + W_TOKEN * s_token)

    return {
        "status": "ok",
        "score": round(composite, 6),
        "ttfa_ms": round(simulated_ttfa, 1),
        "ralph_pass": ralph_pass,
        "tps": round(simulated_tps, 1),
        "barge_accuracy": round(barge_accuracy, 4),
        "subscores": {
            "ttfa": round(s_ttfa, 4),
            "ralph": round(s_ralph, 4),
            "barge": round(s_barge, 4),
            "token": round(s_token, 4),
        },
        "config": cfg,
    }


def main():
    eval_mode = os.environ.get("LIV_HANA_EVAL_MODE", "synthetic")

    if eval_mode == "live":
        # Future: connect to AlloyDB voice_session_metrics for real eval
        print("SCORE: 0.0")
        print("TTFA_MS: 0.0")
        print("RALPH_PASS: False")
        print("ERROR: live mode not yet implemented — use synthetic")
        sys.exit(1)
    else:
        result = evaluate_synthetic()

    if result["status"] == "failed":
        print(f"SCORE: 0.0")
        print(f"TTFA_MS: 9999.0")
        print(f"RALPH_PASS: False")
        print(f"ERROR: {result.get('error')}")
        sys.exit(1)

    # Required output lines (parsed by loop.py regex)
    print(f"SCORE: {result['score']}")
    print(f"TTFA_MS: {result['ttfa_ms']}")
    print(f"RALPH_PASS: {result['ralph_pass']}")
    print(f"TPS: {result.get('tps', 0)}")
    print(f"BARGE_ACCURACY: {result.get('barge_accuracy', 0)}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
