"""
Liv Hana SI — Voice Optimizer (MUTABLE GENOME)
This is the ONLY file the agent is allowed to modify.

This maps to Karpathy's train.py — the single mutable surface.
All parameters here are bounded, validated, and safe to mutate.

NEVER add:
  - Network calls (requests, socket, urllib)
  - Shell execution (os.system, subprocess)
  - File system writes outside this directory
  - eval(), exec(), compile()

The AST firewall in loop.py will BLOCK any such change before it runs.
"""

from dataclasses import dataclass, asdict

# ---------------------------------------------------------------------------
# VAD / Barge-In Parameters (Deepgram equivalent)
# ---------------------------------------------------------------------------

BARGE_IN_THRESHOLD: float = 0.050   # Voice activity detection confidence threshold
                                      # Range: [0.010, 0.150]
                                      # Lower = more sensitive (more interruptions)
                                      # Higher = less sensitive (misses real barge-ins)

SILENCE_TIMEOUT_MS: int = 600        # Milliseconds of silence before end-of-utterance
                                      # Range: [200, 1200]

REDEMPTION_FRAMES: int = 8           # Frames to wait before confirming barge-in
                                      # Range: [2, 20]

PAUSE_TOLERANCE_MS: int = 400        # How long to wait mid-sentence before cutting
                                      # Range: [100, 800]

# ---------------------------------------------------------------------------
# LLM Cascade Hyperparameters
# ---------------------------------------------------------------------------

TEMPERATURE: float = 0.7             # Generation temperature
                                      # Range: [0.0, 1.5]

TOP_P: float = 0.9                   # Nucleus sampling threshold
                                      # Range: [0.5, 1.0]

STREAM_CHUNK_TOKENS: int = 3         # Tokens to buffer before streaming to TTS
                                      # Range: [1, 10]
                                      # Lower = lower TTFA, higher = better prosody

MAX_TOKENS: int = 150                # Max response tokens
                                      # Range: [50, 400]

# ---------------------------------------------------------------------------
# Infrastructure Knobs
# ---------------------------------------------------------------------------

DB_POOL_SIZE: int = 10               # AlloyDB connection pool size
                                      # Range: [2, 50]

HTTP_TIMEOUT_MS: int = 8000          # Fetch timeout for LLM API calls (ms)
                                      # Range: [2000, 15000]

JWT_CACHE_TTL_S: int = 240           # JWT cache TTL in seconds
                                      # Range: [60, 600]

# ---------------------------------------------------------------------------
# Config snapshot (for logging/DSPy bridge)
# ---------------------------------------------------------------------------

@dataclass
class VoiceOptimizerConfig:
    barge_in_threshold: float = BARGE_IN_THRESHOLD
    silence_timeout_ms: int = SILENCE_TIMEOUT_MS
    redemption_frames: int = REDEMPTION_FRAMES
    pause_tolerance_ms: int = PAUSE_TOLERANCE_MS
    temperature: float = TEMPERATURE
    top_p: float = TOP_P
    stream_chunk_tokens: int = STREAM_CHUNK_TOKENS
    max_tokens: int = MAX_TOKENS
    db_pool_size: int = DB_POOL_SIZE
    http_timeout_ms: int = HTTP_TIMEOUT_MS
    jwt_cache_ttl_s: int = JWT_CACHE_TTL_S


def get_config() -> dict:
    """Return current config as dict for logging and DSPy bridge."""
    return asdict(VoiceOptimizerConfig())


def validate_bounds() -> list[str]:
    """Validate all parameters are within safe bounds. Returns list of violations."""
    violations = []
    checks = [
        ("BARGE_IN_THRESHOLD", BARGE_IN_THRESHOLD, 0.010, 0.150),
        ("SILENCE_TIMEOUT_MS", SILENCE_TIMEOUT_MS, 200, 1200),
        ("REDEMPTION_FRAMES", REDEMPTION_FRAMES, 2, 20),
        ("PAUSE_TOLERANCE_MS", PAUSE_TOLERANCE_MS, 100, 800),
        ("TEMPERATURE", TEMPERATURE, 0.0, 1.5),
        ("TOP_P", TOP_P, 0.5, 1.0),
        ("STREAM_CHUNK_TOKENS", STREAM_CHUNK_TOKENS, 1, 10),
        ("MAX_TOKENS", MAX_TOKENS, 50, 400),
        ("DB_POOL_SIZE", DB_POOL_SIZE, 2, 50),
        ("HTTP_TIMEOUT_MS", HTTP_TIMEOUT_MS, 2000, 15000),
        ("JWT_CACHE_TTL_S", JWT_CACHE_TTL_S, 60, 600),
    ]
    for name, val, lo, hi in checks:
        if not (lo <= val <= hi):
            violations.append(f"{name}={val} out of bounds [{lo}, {hi}]")
    return violations


if __name__ == "__main__":
    import json
    violations = validate_bounds()
    if violations:
        print(f"BOUNDS_VIOLATIONS: {violations}")
        raise SystemExit(1)
    print(json.dumps(get_config(), indent=2))
