"""
Liv Hana SI — Bayesian Mutation Strategy (Thompson Sampling)

Replaces random walk with intelligent parameter selection using experiment history.
Each parameter gets a Beta(wins+1, losses+1) distribution. Thompson Sampling picks
the parameter most likely to yield improvement.

This file is NEW — added as part of RVOS (Recursive Voice Optimization System).
"""

import json
import logging
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("liv_hana.mutation_strategy")

# All mutable parameters with their bounds and step sizes
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

PARAM_NAMES = [p[0] for p in PARAMS]
PARAM_MAP = {p[0]: p for p in PARAMS}


@dataclass
class ParamStats:
    """Per-parameter statistics derived from experiment history."""
    name: str
    wins: int = 0
    losses: int = 0
    total_attempts: int = 0
    mean_positive_delta: float = 0.0
    best_known_value: float | None = None
    last_direction: int = 0  # +1 or -1


@dataclass
class MetaParams:
    """Meta-parameters for the mutation strategy itself (Phase 7: recursive self-improvement)."""
    base_step_multiplier: float = 1.0       # Scale all step sizes [0.25, 4.0]
    multi_param_probability: float = 0.2    # Prob of 2-param joint mutation [0.0, 0.5]
    multi_param_threshold: int = 20         # Min experiments before allowing multi-param [10, 50]
    exploration_bonus: float = 0.1          # Extra Thompson bonus for under-explored params [0.0, 0.5]
    step_decay_rate: float = 0.95           # Shrink step toward optimum [0.8, 1.0]


class HistoryAnalyzer:
    """Reads experiments_log.jsonl and builds per-parameter sensitivity model."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.experiments: list[dict] = []
        self.param_stats: dict[str, ParamStats] = {name: ParamStats(name=name) for name in PARAM_NAMES}
        self.total_experiments = 0
        self.total_wins = 0

    def load(self):
        """Load and analyze experiment history."""
        if not self.log_path.exists():
            log.info("No experiment history found — starting fresh")
            return

        self.experiments = []
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    exp = json.loads(line)
                    if exp.get("experiment_id", "").startswith("exp_"):
                        self.experiments.append(exp)
                except json.JSONDecodeError:
                    pass

        self.total_experiments = len(self.experiments)
        self._analyze_params()
        log.info(f"Loaded {self.total_experiments} experiments from history")

    def _analyze_params(self):
        """Build per-parameter win/loss statistics."""
        # We need to infer which parameter changed per experiment
        # by comparing consecutive configs in the notes field
        prev_config = None

        for exp in self.experiments:
            status = exp.get("status", "")
            notes = exp.get("notes", "")

            # Try to extract config from notes
            current_config = self._extract_config(notes)
            if current_config and prev_config:
                changed_param = self._find_changed_param(prev_config, current_config)
                if changed_param:
                    stats = self.param_stats[changed_param]
                    stats.total_attempts += 1
                    if status == "improved":
                        stats.wins += 1
                        self.total_wins += 1
                        delta = exp.get("delta", 0)
                        if delta and delta > 0:
                            # Running average of positive deltas
                            n = stats.wins
                            stats.mean_positive_delta = (
                                stats.mean_positive_delta * (n - 1) + delta
                            ) / n
                        if current_config.get(changed_param) is not None:
                            stats.best_known_value = current_config[changed_param]
                    else:
                        stats.losses += 1

            if current_config:
                prev_config = current_config

    def _extract_config(self, notes: str) -> dict | None:
        """Extract config dict from experiment notes."""
        # Notes contain truncated JSON with config values
        config = {}
        for name in PARAM_NAMES:
            key = name.lower()
            pattern = rf'"{key}"\s*:\s*([0-9.]+)'
            m = re.search(pattern, notes)
            if m:
                config[name] = float(m.group(1))
        return config if config else None

    def _find_changed_param(self, prev: dict, curr: dict) -> str | None:
        """Find which parameter changed between two configs."""
        for name in PARAM_NAMES:
            if name in prev and name in curr:
                if abs(prev[name] - curr[name]) > 1e-6:
                    return name
        return None

    def get_win_rate(self, param_name: str) -> float:
        """Get win rate for a parameter."""
        stats = self.param_stats.get(param_name)
        if not stats or stats.total_attempts == 0:
            return 0.5  # Prior: assume 50% for unexplored
        return stats.wins / stats.total_attempts

    def get_overall_win_rate(self) -> float:
        """Get overall win rate across all experiments."""
        if self.total_experiments == 0:
            return 0.0
        return self.total_wins / self.total_experiments


class BayesianMutator:
    """
    Thompson Sampling-based parameter mutation.

    Each parameter has a Beta(wins+1, losses+1) distribution.
    We sample from each, pick the parameter with the highest sample,
    then apply an adaptive step in the best-known direction.
    """

    def __init__(self, analyzer: HistoryAnalyzer, meta: MetaParams | None = None):
        self.analyzer = analyzer
        self.meta = meta or MetaParams()

    def select_parameter(self) -> str:
        """Use Thompson Sampling to select which parameter to mutate."""
        best_sample = -1.0
        best_param = PARAM_NAMES[0]

        for name in PARAM_NAMES:
            stats = self.analyzer.param_stats[name]
            alpha = stats.wins + 1
            beta_param = stats.losses + 1

            # Thompson sample from Beta distribution
            sample = random.betavariate(alpha, beta_param)

            # Exploration bonus for under-explored params
            if stats.total_attempts < 3:
                sample += self.meta.exploration_bonus

            if sample > best_sample:
                best_sample = sample
                best_param = name

        return best_param

    def compute_step(self, param_name: str, current_value: float) -> float:
        """Compute adaptive step size for the selected parameter."""
        _, ptype, lo, hi, base_step = PARAM_MAP[param_name]
        stats = self.analyzer.param_stats[param_name]

        step = base_step * self.meta.base_step_multiplier

        # Shrink step when near known optima
        if stats.best_known_value is not None:
            distance = abs(current_value - stats.best_known_value) / (hi - lo)
            if distance < 0.1:
                step *= self.meta.step_decay_rate

        # Direction: prefer direction of last improvement, with randomness
        if stats.last_direction != 0 and random.random() < 0.65:
            direction = stats.last_direction
        else:
            direction = random.choice([-1, 1])

        return direction * step

    def propose(self, mutable_file: Path) -> tuple[str | None, dict]:
        """
        Propose a mutation to voice_optimizer.py.

        Returns (new_code, metadata) where metadata contains
        the mutation details for logging.
        """
        current = mutable_file.read_text()

        # Decide if we do multi-param mutation
        do_multi = (
            self.analyzer.total_experiments >= self.meta.multi_param_threshold
            and random.random() < self.meta.multi_param_probability
        )

        num_params = 2 if do_multi else 1
        mutations = []

        for _ in range(num_params):
            param_name = self.select_parameter()

            # Avoid mutating same param twice in multi-param
            while do_multi and any(m["param"] == param_name for m in mutations):
                param_name = random.choice(PARAM_NAMES)

            _, ptype, lo, hi, base_step = PARAM_MAP[param_name]

            # Find current value in code
            pattern = rf"({param_name})\s*(?::\s*\w+\s*)?\s*=\s*([0-9.]+)"
            m = re.search(pattern, current)
            if not m:
                log.warning(f"Could not find {param_name} in voice_optimizer.py")
                return None, {"error": f"param_not_found: {param_name}"}

            current_val = ptype(float(m.group(2)))
            step = self.compute_step(param_name, current_val)

            if ptype == float:
                new_val = round(max(lo, min(hi, current_val + step)), 4)
                val_str = f"{new_val:.3f}"
            else:
                new_val = max(int(lo), min(int(hi), int(current_val + step)))
                val_str = str(new_val)

            # Apply mutation to code
            new_code = current[:m.start()] + m.group(0)[:m.start(2) - m.start()] + val_str + current[m.end(2):]
            current = new_code  # For multi-param, chain mutations

            mutations.append({
                "param": param_name,
                "old_value": current_val,
                "new_value": new_val if ptype == int else round(new_val, 4),
                "step": round(step, 4) if ptype == float else int(step),
                "thompson_alpha": self.analyzer.param_stats[param_name].wins + 1,
                "thompson_beta": self.analyzer.param_stats[param_name].losses + 1,
            })

            log.info(f"   {param_name}: {current_val} → {new_val} (Bayesian step={step:.4f})")

        metadata = {
            "strategy": "bayesian_thompson",
            "multi_param": do_multi,
            "mutations": mutations,
        }

        return current, metadata


class MetaOptimizer:
    """
    Recursive self-improvement of the mutation strategy.
    Tunes Thompson Sampling hyperparameters using experiment history.
    Runs every meta_interval experiments.
    """

    META_BOUNDS = {
        "base_step_multiplier": (0.25, 4.0, 0.25),
        "multi_param_probability": (0.0, 0.5, 0.05),
        "exploration_bonus": (0.0, 0.5, 0.05),
        "step_decay_rate": (0.8, 1.0, 0.02),
    }

    def __init__(self, meta: MetaParams, interval: int = 50):
        self.meta = meta
        self.interval = interval
        self.last_check = 0
        self.history: list[dict] = []  # (meta_config, win_rate) pairs

    def should_optimize(self, total_experiments: int) -> bool:
        return total_experiments - self.last_check >= self.interval

    def optimize(self, analyzer: HistoryAnalyzer) -> dict | None:
        """
        Adjust meta-parameters based on recent experiment performance.
        Returns metadata dict if adjustments were made, None otherwise.
        """
        self.last_check = analyzer.total_experiments
        current_win_rate = analyzer.get_overall_win_rate()

        self.history.append({
            "experiments": analyzer.total_experiments,
            "win_rate": current_win_rate,
            "meta": {
                "base_step_multiplier": self.meta.base_step_multiplier,
                "multi_param_probability": self.meta.multi_param_probability,
                "exploration_bonus": self.meta.exploration_bonus,
                "step_decay_rate": self.meta.step_decay_rate,
            },
        })

        if len(self.history) < 2:
            return None

        prev_rate = self.history[-2]["win_rate"]
        adjustments = {}

        # If win rate dropped, try smaller steps (exploit more)
        if current_win_rate < prev_rate - 0.05:
            old = self.meta.base_step_multiplier
            lo, hi, step = self.META_BOUNDS["base_step_multiplier"]
            self.meta.base_step_multiplier = max(lo, old - step)
            adjustments["base_step_multiplier"] = (old, self.meta.base_step_multiplier)

        # If win rate improved, try larger exploration
        elif current_win_rate > prev_rate + 0.05:
            old = self.meta.exploration_bonus
            lo, hi, step = self.META_BOUNDS["exploration_bonus"]
            self.meta.exploration_bonus = min(hi, old + step)
            adjustments["exploration_bonus"] = (old, self.meta.exploration_bonus)

        # If plateaued (similar win rate), try multi-param mutations
        else:
            old = self.meta.multi_param_probability
            lo, hi, step = self.META_BOUNDS["multi_param_probability"]
            self.meta.multi_param_probability = min(hi, old + step)
            adjustments["multi_param_probability"] = (old, self.meta.multi_param_probability)

        if adjustments:
            log.info(f"🧬 META-OPTIMIZATION: {adjustments}")
            return {
                "type": "meta_mutation",
                "win_rate": current_win_rate,
                "prev_win_rate": prev_rate,
                "adjustments": {k: {"old": v[0], "new": v[1]} for k, v in adjustments.items()},
            }

        return None
