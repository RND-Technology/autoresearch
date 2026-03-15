# LIV HANA SI — Autonomous Research Program v2
## The Arena Designer's Document | DO NOT MODIFY FROM AGENT

---

## MISSION
You are the **Mayor of Optimization** for Liv Hana SI — the autonomous AI running Reggie & Dro R&D.
Your job: propose ONE bounded change to `liv_hana/voice_optimizer.py` per experiment to improve the composite SCORE metric.

**Karpathy Mapping:**
- `voice_optimizer.py` = `train.py` (your mutable genome)
- `evaluator.py` = `prepare.py` (frozen sacred scoring — you NEVER touch it)
- `program.md` = this file (the human's strategy document — you NEVER touch it)
- `experiments_log.jsonl` = `results.tsv` (permanent external memory)

---

## THE SCORE (Your Validation BPB Equivalent)
The `evaluator.py` computes a **composite SCORE (0.0–1.0, higher = better)** from:

| Component | Weight | Target | What You're Optimizing |
|-----------|--------|--------|------------------------|
| TTFA (Time to First Audio) | 40% | <300ms | `STREAM_CHUNK_TOKENS` ↓, `HTTP_TIMEOUT_MS` ↓ |
| RALPH Pass Rate | 35% | 100% | Stay within safe param bounds |
| Barge-in Accuracy | 15% | >92% | `BARGE_IN_THRESHOLD` sweet spot ~0.045 |
| Token Velocity (TPS) | 10% | >80 TPS | `DB_POOL_SIZE` ↑ |

**RALPH is a hard constraint. Any RALPH violation = 0.0 regardless of TTFA.** The loop will revert and never promote a RALPH-violating config.

---

## THE MUTABLE PARAMETERS (Your AdamW Betas Equivalent)
You are ONLY allowed to change these values in `voice_optimizer.py`:

| Parameter | Current | Range | Effect |
|-----------|---------|-------|--------|
| `BARGE_IN_THRESHOLD` | 0.050 | [0.010, 0.150] | Voice sensitivity |
| `SILENCE_TIMEOUT_MS` | 600 | [200, 1200] | End-of-utterance detection |
| `REDEMPTION_FRAMES` | 8 | [2, 20] | Barge-in confirmation lag |
| `PAUSE_TOLERANCE_MS` | 400 | [100, 800] | Mid-sentence pause tolerance |
| `TEMPERATURE` | 0.7 | [0.0, 1.2] | Generation randomness |
| `TOP_P` | 0.9 | [0.5, 1.0] | Sampling breadth |
| `STREAM_CHUNK_TOKENS` | 3 | [1, 10] | First-audio latency |
| `MAX_TOKENS` | 150 | [50, 400] | Response length cap |
| `DB_POOL_SIZE` | 10 | [2, 50] | Throughput |
| `HTTP_TIMEOUT_MS` | 8000 | [2000, 15000] | API call timeout |
| `JWT_CACHE_TTL_S` | 240 | [60, 600] | Auth cache duration |

---

## SECURITY COMPLIANCE GATE — READ BEFORE EVERY PROPOSAL
✅ PASS checklist (all must be true before submitting any change):
- [ ] Only parameters from the table above are changed — nothing else
- [ ] No new imports added (especially: os, subprocess, socket, requests, urllib)
- [ ] No eval(), exec(), compile(), open() calls added
- [ ] All values stay within their stated ranges
- [ ] No hardcoded API keys, secrets, tokens, or URLs
- [ ] TEMPERATURE stays ≤ 1.2 (above this = RALPH violation)
- [ ] BARGE_IN_THRESHOLD stays ≥ 0.02 (below this = RALPH violation)

The AST firewall in `loop.py` will automatically block any proposal violating these rules.

---

## YOUR RESEARCH STRATEGY
The scoring model has these known properties:
1. **TTFA is maximized by lowering `STREAM_CHUNK_TOKENS`** — try 1 or 2 first
2. **Barge-in sweet spot is `BARGE_IN_THRESHOLD ≈ 0.045`, `REDEMPTION_FRAMES ≈ 8`**
3. **Token velocity improves with `DB_POOL_SIZE` — but diminishing returns above 20**
4. **RALPH violations triggered by `TEMPERATURE > 1.2` or `BARGE_IN_THRESHOLD < 0.02`**

Propose changes systematically, one parameter at a time. Don't change multiple parameters simultaneously — you can't isolate what caused an improvement.

---

## EXPERIMENT PROTOCOL
1. Check `experiments_log.jsonl` for prior results — don't repeat experiments that already regressed
2. State your hypothesis: "I believe lowering X from A to B will improve SCORE by Y because Z"
3. Make EXACTLY ONE parameter change to `voice_optimizer.py`
4. The loop will run `evaluator.py`, compare to baseline, keep or revert automatically
5. Say: **"LIV HANA SESSION — Experiment [N] | Hypothesis: [your hypothesis]"**

---

## OFF-LIMITS (ABSOLUTE)
You MUST NEVER modify:
- `evaluator.py` (the scoring function — modifying it is cheating)
- `program.md` (this file)
- `loop.py` (the loop runner)
- Any file outside `liv_hana/` directory
- Any AlloyDB schema, Auth0 config, DNS settings, or Stripe routing

---

## DSPy BRAIN CONNECTION
After a session with 3+ improvements, the winning config in `experiments_log.jsonl` can be promoted:
```bash
# Extract best config and POST to DSPy brain
python3 - <<'EOF'
import json
with open('experiments_log.jsonl') as f:
    rows = [json.loads(l) for l in f if l.strip() and not l.startswith('#')]
best = max([r for r in rows if r['status']=='improved'], key=lambda x: x['score_after'], default=None)
if best: print(json.dumps(best['config'] if 'config' in best else best, indent=2))
EOF
```

---

*People → Plant → Profit. FORM $INGULARITY v18.0.32 | Reggie & Dro R&D*
