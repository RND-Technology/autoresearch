"""
Liv Hana SI — Live Evaluator (AlloyDB Voice Metrics)

Evaluates voice optimizer config against REAL production data from AlloyDB.
Uses the same composite SCORE formula as the frozen evaluator.py:
  SCORE = TTFA(40%) + RALPH(35%) + Barge-In(15%) + TokenVelocity(10%)

Outputs the same format (SCORE/TTFA_MS/RALPH_PASS) for loop.py regex compatibility.

This file is NEW — it does NOT replace evaluator.py (which remains frozen).
The live evaluator is used when --eval-mode live|hybrid is specified.

Requirements:
  - Cloud SQL Auth Proxy running locally (for AlloyDB access)
  - Or running on Cloud Run with VPC connector
  - asyncpg or psycopg2 installed

This file is NEW — added as part of RVOS (Recursive Voice Optimization System).
"""

import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("liv_hana.live_evaluator")

# Scoring weights (MUST match evaluator.py — frozen sacred values)
W_TTFA = 0.40
W_RALPH = 0.35
W_BARGE = 0.15
W_TOKEN = 0.10

TTFA_TARGET_MS = 300.0
TTFA_MAX_MS = 2000.0
TPS_TARGET = 80.0
TPS_MIN = 10.0

MIN_SAMPLE_COUNT = 50  # Minimum voice sessions for valid live eval
DEFAULT_WINDOW_MINUTES = 60


def _score_ttfa(ttfa_ms: float) -> float:
    if ttfa_ms <= TTFA_TARGET_MS:
        return 1.0
    if ttfa_ms >= TTFA_MAX_MS:
        return 0.0
    return 1.0 - (ttfa_ms - TTFA_TARGET_MS) / (TTFA_MAX_MS - TTFA_TARGET_MS)


def _score_ralph(ralph_pass: bool) -> float:
    return 1.0 if ralph_pass else 0.0


def _score_barge(accuracy: float) -> float:
    return max(0.0, min(1.0, accuracy))


def _score_token_velocity(tps: float) -> float:
    if tps >= TPS_TARGET:
        return 1.0
    if tps <= TPS_MIN:
        return 0.0
    return (tps - TPS_MIN) / (TPS_TARGET - TPS_MIN)


def evaluate_live(window_minutes: int = DEFAULT_WINDOW_MINUTES) -> dict:
    """
    Evaluate using real production data from AlloyDB.

    Queries voice_session_metrics and transcript_quality_scores
    for the specified time window.
    """
    database_url = os.environ.get("ALLOYDB_URI") or os.environ.get("DATABASE_URL")
    if not database_url:
        return {
            "status": "failed",
            "error": "No ALLOYDB_URI or DATABASE_URL set",
            "score": 0.0,
        }

    try:
        import psycopg2
    except ImportError:
        return {
            "status": "failed",
            "error": "psycopg2 not installed. Run: pip install psycopg2-binary",
            "score": 0.0,
        }

    try:
        conn = psycopg2.connect(database_url, connect_timeout=10)
        cur = conn.cursor()

        # Query voice session metrics
        cur.execute(f"""
            SELECT
                AVG(ttfa_ms) as avg_ttfa,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ttfa_ms) as p95_ttfa,
                COUNT(*) as sample_count,
                AVG(tokens_per_second) as avg_tps
            FROM voice_session_metrics
            WHERE created_at >= NOW() - INTERVAL '{window_minutes} minutes'
              AND ttfa_ms IS NOT NULL
              AND ttfa_ms > 0
        """)
        row = cur.fetchone()
        avg_ttfa = row[0] or 999.0
        p95_ttfa = row[1] or 999.0
        sample_count = row[2] or 0
        avg_tps = row[3] or 0.0

        if sample_count < MIN_SAMPLE_COUNT:
            cur.close()
            conn.close()
            return {
                "status": "insufficient_data",
                "error": f"Only {sample_count} sessions in window (need {MIN_SAMPLE_COUNT})",
                "sample_count": sample_count,
                "score": None,
            }

        # Query barge-in accuracy from transcript quality
        cur.execute(f"""
            SELECT
                AVG(CASE WHEN barge_in_correct THEN 1.0 ELSE 0.0 END) as barge_accuracy,
                COUNT(*) as barge_count
            FROM transcript_quality_scores
            WHERE created_at >= NOW() - INTERVAL '{window_minutes} minutes'
              AND barge_in_correct IS NOT NULL
        """)
        barge_row = cur.fetchone()
        barge_accuracy = barge_row[0] or 0.85  # Default if no data
        barge_count = barge_row[1] or 0

        # Check RALPH compliance
        cur.execute("""
            SELECT COUNT(*) FROM ralph_hook_results
            WHERE created_at >= NOW() - INTERVAL '24 hours'
              AND status = 'FAIL'
        """)
        ralph_failures = cur.fetchone()[0] or 0
        ralph_pass = ralph_failures == 0

        cur.close()
        conn.close()

        # Compute composite score using P95 TTFA (more conservative than avg)
        s_ttfa = _score_ttfa(p95_ttfa)
        s_ralph = _score_ralph(ralph_pass)
        s_barge = _score_barge(barge_accuracy)
        s_token = _score_token_velocity(avg_tps)

        composite = W_TTFA * s_ttfa + W_RALPH * s_ralph + W_BARGE * s_barge + W_TOKEN * s_token

        return {
            "status": "ok",
            "score": round(composite, 6),
            "ttfa_ms": round(p95_ttfa, 1),
            "avg_ttfa_ms": round(avg_ttfa, 1),
            "ralph_pass": ralph_pass,
            "ralph_failures": ralph_failures,
            "tps": round(avg_tps, 1),
            "barge_accuracy": round(barge_accuracy, 4),
            "barge_count": barge_count,
            "sample_count": sample_count,
            "window_minutes": window_minutes,
            "subscores": {
                "ttfa": round(s_ttfa, 4),
                "ralph": round(s_ralph, 4),
                "barge": round(s_barge, 4),
                "token": round(s_token, 4),
            },
        }

    except Exception as e:
        log.error(f"Live evaluation failed: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "score": 0.0,
        }


def evaluate_hybrid(window_minutes: int = DEFAULT_WINDOW_MINUTES) -> dict:
    """
    Hybrid evaluation: try live first, fall back to synthetic if insufficient data.
    """
    live_result = evaluate_live(window_minutes)

    if live_result["status"] == "ok":
        live_result["eval_mode"] = "live"
        return live_result

    if live_result["status"] == "insufficient_data":
        log.info(f"Insufficient live data ({live_result.get('sample_count', 0)} sessions) — falling back to synthetic")
    else:
        log.warning(f"Live eval failed: {live_result.get('error')} — falling back to synthetic")

    # Fall back to synthetic
    sys.path.insert(0, str(Path(__file__).parent))
    from evaluator import evaluate_synthetic
    result = evaluate_synthetic()
    result["eval_mode"] = "synthetic_fallback"
    return result


def main():
    """Standalone entry point — outputs same format as evaluator.py."""
    eval_mode = os.environ.get("LIV_HANA_EVAL_MODE", "hybrid")
    window = int(os.environ.get("LIV_HANA_EVAL_WINDOW", str(DEFAULT_WINDOW_MINUTES)))

    if eval_mode == "live":
        result = evaluate_live(window)
    elif eval_mode == "hybrid":
        result = evaluate_hybrid(window)
    else:
        # Synthetic fallback
        sys.path.insert(0, str(Path(__file__).parent))
        from evaluator import evaluate_synthetic
        result = evaluate_synthetic()

    if result["status"] == "failed":
        print(f"SCORE: 0.0")
        print(f"TTFA_MS: 9999.0")
        print(f"RALPH_PASS: False")
        print(f"ERROR: {result.get('error')}")
        sys.exit(1)

    print(f"SCORE: {result.get('score', 0.0)}")
    print(f"TTFA_MS: {result.get('ttfa_ms', 0.0)}")
    print(f"RALPH_PASS: {result.get('ralph_pass', False)}")
    print(f"TPS: {result.get('tps', 0)}")
    print(f"BARGE_ACCURACY: {result.get('barge_accuracy', 0)}")
    print(f"EVAL_MODE: {result.get('eval_mode', eval_mode)}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
