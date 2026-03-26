"""
Liv Hana SI — DSPy Brain Bridge

Bidirectional sync between local autoresearch loop and cloud DSPy Brain.
- promote_config(): Push winning local configs to cloud autoresearch_config table
- pull_latest_config(): Sync local voice_optimizer.py from cloud's current best
- trigger_retrain(): Kick off MIPROv2 prompt optimization with new config context

This file is NEW — added as part of RVOS (Recursive Voice Optimization System).
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("liv_hana.dspy_bridge")

# Default endpoints — override via environment
DSPY_BRAIN_URL = "https://dspy-brain-plad5efvha-uc.a.run.app"
INTEGRATION_SERVICE_URL = "https://integration-service-plad5efvha-uc.a.run.app"
ORIGIN_HEADER = "https://herbitrage.com"

# Mapping: local param name -> cloud autoresearch_config param_name
# barge_in_threshold and pause_tolerance_ms already exist from M352/M364
LOCAL_TO_CLOUD_MAP = {
    "BARGE_IN_THRESHOLD": "barge_in_threshold",
    "SILENCE_TIMEOUT_MS": "silence_timeout_ms",
    "REDEMPTION_FRAMES": "redemption_frames",
    "PAUSE_TOLERANCE_MS": "pause_tolerance_ms",
    "TEMPERATURE": "temperature",
    "TOP_P": "top_p",
    "STREAM_CHUNK_TOKENS": "stream_chunk_tokens",
    "MAX_TOKENS": "max_tokens",
    "DB_POOL_SIZE": "db_pool_size",
    "HTTP_TIMEOUT_MS": "http_timeout_ms",
    "JWT_CACHE_TTL_S": "jwt_cache_ttl_s",
}


class DSPyBridge:
    """Bidirectional bridge between local autoresearch and cloud DSPy Brain."""

    def __init__(
        self,
        dspy_url: str | None = None,
        integration_url: str | None = None,
        origin: str | None = None,
    ):
        import os
        self.dspy_url = dspy_url or os.environ.get("DSPY_BRAIN_URL", DSPY_BRAIN_URL)
        self.integration_url = integration_url or os.environ.get("INTEGRATION_SERVICE_URL", INTEGRATION_SERVICE_URL)
        self.origin = origin or os.environ.get("DSPY_ORIGIN", ORIGIN_HEADER)

    def _post(self, url: str, payload: dict) -> dict:
        """HTTP POST with standard headers."""
        import urllib.request
        import urllib.error

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, url: str) -> dict:
        """HTTP GET with standard headers."""
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={"Origin": self.origin},
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def promote_config(
        self,
        config: dict,
        score: float,
        council_verdict: str,
        experiment_id: str = "",
    ) -> dict:
        """
        Push winning local config to cloud autoresearch_config table.

        Uses the integration service's run-sql endpoint to upsert params.
        """
        log.info(f"Promoting config to cloud | Score: {score:.4f} | Verdict: {council_verdict}")

        results = {}
        for local_name, cloud_name in LOCAL_TO_CLOUD_MAP.items():
            if local_name not in config:
                continue

            value = config[local_name]
            sql = (
                f"INSERT INTO autoresearch_config (param_name, param_value, param_min, param_max, step_size, frozen) "
                f"VALUES ('{cloud_name}', {value}, 0, 99999, 1, FALSE) "
                f"ON CONFLICT (param_name) DO UPDATE SET "
                f"param_value = {value}, "
                f"updated_at = NOW()"
            )

            try:
                resp = self._post(
                    f"{self.integration_url}/api/v1/db/migrate",
                    {"sql_content": sql},
                )
                results[cloud_name] = {"status": "ok", "response": resp}
            except Exception as e:
                log.error(f"Failed to promote {cloud_name}: {e}")
                results[cloud_name] = {"status": "error", "error": str(e)}

        log.info(f"Promoted {sum(1 for r in results.values() if r['status'] == 'ok')}/{len(results)} params to cloud")
        return results

    def trigger_retrain(self, config: dict, score: float) -> dict:
        """
        Trigger MIPROv2 prompt optimization on DSPy Brain using new config context.
        """
        log.info("Triggering DSPy Brain retrain with new voice config")

        try:
            resp = self._post(
                f"{self.dspy_url}/api/v1/learning/council-retrain",
                {
                    "source": "autoresearch-local",
                    "experiment": {
                        "config": config,
                        "score": score,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
            log.info(f"DSPy retrain triggered: {resp.get('status', 'unknown')}")
            return resp
        except Exception as e:
            log.error(f"DSPy retrain failed: {e}")
            return {"status": "error", "error": str(e)}

    def pull_latest_config(self, mutable_file: Path) -> dict | None:
        """
        Sync local voice_optimizer.py from cloud's current best config.

        Reads autoresearch_config table and updates local parameter values.
        Returns the cloud config dict, or None on failure.
        """
        log.info("Pulling latest config from cloud autoresearch_config...")

        try:
            # Query all voice optimizer params from cloud
            sql = "SELECT param_name, param_value FROM autoresearch_config WHERE param_name IN ("
            sql += ", ".join(f"'{v}'" for v in LOCAL_TO_CLOUD_MAP.values())
            sql += ")"

            resp = self._post(
                f"{self.integration_url}/api/v1/db/migrate",
                {"sql_content": sql},
            )

            rows = resp.get("rows", resp.get("result", []))
            if not rows:
                log.info("No cloud config found — keeping local values")
                return None

            # Build cloud config
            cloud_config = {}
            cloud_to_local = {v: k for k, v in LOCAL_TO_CLOUD_MAP.items()}
            for row in rows:
                cloud_name = row.get("param_name", row[0] if isinstance(row, (list, tuple)) else "")
                value = row.get("param_value", row[1] if isinstance(row, (list, tuple)) else None)
                if cloud_name in cloud_to_local and value is not None:
                    cloud_config[cloud_to_local[cloud_name]] = float(value)

            if not cloud_config:
                log.info("Cloud config empty — keeping local values")
                return None

            # Update local file
            code = mutable_file.read_text()
            updates = 0
            for param_name, value in cloud_config.items():
                pattern = rf"({param_name})\s*(?::\s*\w+\s*)?\s*=\s*([0-9.]+)"
                m = re.search(pattern, code)
                if m:
                    old_val = float(m.group(2))
                    if abs(old_val - value) > 1e-6:
                        if isinstance(value, float) and value != int(value):
                            val_str = f"{value:.3f}"
                        else:
                            val_str = str(int(value))
                        code = code[:m.start()] + m.group(0)[:m.start(2) - m.start()] + val_str + code[m.end(2):]
                        updates += 1
                        log.info(f"   {param_name}: {old_val} -> {value} (from cloud)")

            if updates > 0:
                mutable_file.write_text(code)
                log.info(f"Updated {updates} params from cloud config")
            else:
                log.info("Local config already matches cloud")

            return cloud_config

        except Exception as e:
            log.error(f"Failed to pull cloud config: {e}")
            return None
