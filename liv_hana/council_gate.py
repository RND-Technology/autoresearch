"""
Liv Hana SI — LLM Council Gate

Submits winning voice optimizer configs to the LLM Council for approval
before promotion to production. MANDATORY gate for production deployment.

Verdicts:
  APPROVED    -> Proceed to DSPy Brain promotion
  REJECTED    -> Revert, expand exploration radius
  HITL_REQUIRED -> Halt, await CEO decision

This file is NEW — added as part of RVOS (Recursive Voice Optimization System).
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("liv_hana.council_gate")

# Default endpoints — override via environment
COUNCIL_ENDPOINT = "https://integration-service-plad5efvha-uc.a.run.app/api/v1/council/agent-review"
ORIGIN_HEADER = "https://herbitrage.com"


@dataclass
class CouncilVerdict:
    """Result from LLM Council review."""
    status: str  # "APPROVED", "REJECTED", "HITL_REQUIRED", "ERROR"
    confidence: float = 0.0
    reasoning: str = ""
    council_id: str = ""
    raw_response: dict | None = None


class CouncilGate:
    """
    Submits voice optimizer configs to the LLM Council for validation.

    The Council evaluates:
    1. Are the parameter values within safe operational bounds?
    2. Does the improvement justify production deployment?
    3. Are there any RALPH compliance risks?
    """

    def __init__(self, endpoint: str | None = None, origin: str | None = None):
        import os
        self.endpoint = endpoint or os.environ.get("COUNCIL_ENDPOINT", COUNCIL_ENDPOINT)
        self.origin = origin or os.environ.get("COUNCIL_ORIGIN", ORIGIN_HEADER)

    def submit_for_review(
        self,
        config: dict,
        score: float,
        delta: float,
        baseline_score: float,
        experiments_run: int,
        win_rate: float,
        evidence: dict | None = None,
    ) -> CouncilVerdict:
        """
        Submit a winning config to the LLM Council for review.

        Args:
            config: The voice optimizer config dict (11 params)
            score: Current composite score
            delta: Improvement over baseline
            baseline_score: Starting score
            experiments_run: Total experiments in this session
            win_rate: Percentage of experiments that improved
            evidence: Additional evidence (TTFA, barge-in accuracy, etc.)

        Returns:
            CouncilVerdict with approval status
        """
        import urllib.request
        import urllib.error

        payload = {
            "agentId": "autoresearch-voice-optimizer",
            "taskType": "voice_config_promotion",
            "output": {
                "proposed_config": config,
                "score": round(score, 6),
                "delta": round(delta, 6),
                "baseline_score": round(baseline_score, 6),
                "experiments_run": experiments_run,
                "win_rate": round(win_rate, 4),
                "evidence": evidence or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

        log.info(f"Submitting config to Council | Score: {score:.4f} | Delta: +{delta:.4f}")

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.origin,
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            verdict_str = body.get("verdict", body.get("status", "ERROR")).upper()
            confidence = body.get("confidence", body.get("score", 0.0))
            reasoning = body.get("reasoning", body.get("summary", ""))
            council_id = body.get("council_id", body.get("id", ""))

            verdict = CouncilVerdict(
                status=verdict_str,
                confidence=float(confidence) if confidence else 0.0,
                reasoning=str(reasoning),
                council_id=str(council_id),
                raw_response=body,
            )

            log.info(f"Council verdict: {verdict.status} (confidence: {verdict.confidence:.2f})")
            if verdict.reasoning:
                log.info(f"Council reasoning: {verdict.reasoning[:200]}")

            return verdict

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            log.error(f"Council HTTP error {e.code}: {error_body[:200]}")
            return CouncilVerdict(status="ERROR", reasoning=f"HTTP {e.code}: {error_body[:200]}")
        except urllib.error.URLError as e:
            log.error(f"Council connection error: {e}")
            return CouncilVerdict(status="ERROR", reasoning=f"Connection error: {e}")
        except Exception as e:
            log.error(f"Council submission failed: {e}")
            return CouncilVerdict(status="ERROR", reasoning=str(e))

    def should_submit(
        self,
        wins_since_last: int,
        delta_since_last: float,
        min_wins: int = 3,
        min_delta: float = 0.01,
    ) -> bool:
        """
        Determine if we have enough improvement to warrant Council review.

        Triggers when EITHER:
        - 3+ improvements since last submission
        - Total delta > 0.01 since last submission
        """
        return wins_since_last >= min_wins or delta_since_last >= min_delta
