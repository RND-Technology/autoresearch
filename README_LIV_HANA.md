# LIV HANA Customizations — autoresearch Fork

This is `RND-Technology/autoresearch`, a hardened fork of [karpathy/autoresearch](https://github.com/karpathy/autoresearch) customized for **Liv Hana SI** — the autonomous AI operating system powering Reggie & Dro R&D.

---

## What Changed From Upstream

| File | Change |
|------|--------|
| `train.py` | Added `SAFETY_GATE`, OOM handling, structured logging, latency benchmark, input validation |
| `program.md` | Replaced generic instructions with Liv Hana mission, Cloud Run constraints, compliance gate |
| `experiments_log.jsonl` | New — structured experiment ledger |
| `.env.example` | New — environment config template |
| `README_LIV_HANA.md` | This file |

`prepare.py` is **NOT modified** — per Karpathy's design intent.

---

## How This Connects to DSPy Brain

After a successful overnight research session, the best `train.py` config can be submitted to the DSPy brain for integration into voice prompt optimization:

```bash
# After overnight run, extract best config
BEST=$(python3 -c "
import json
with open('experiments_log.jsonl') as f:
    exps = [json.loads(l) for l in f if l.strip()]
improved = [e for e in exps if e.get('status') == 'improved']
best = min(improved, key=lambda x: x['val_bpb_after']) if improved else None
if best: print(json.dumps(best))
")

# Submit to DSPy brain
curl -X POST "$DSPY_BRAIN_URL/api/v1/learning/council-retrain" \\
  -H "Content-Type: application/json" \\
  -d "{\"source\": \"autoresearch\", \"experiment\": $BEST}"
```

---

## Running on Cloud Run / CPU (No GPU)

For CPU-only inference (Cloud Run), use these conservative defaults in `train.py`:

```python
DEPTH = 4           # Shallow = fast
MAX_SEQ_LEN = 256   # Short retail conversations
TOTAL_BATCH_SIZE = 2**14  # ~16K tokens
WINDOW_PATTERN = "L"  # Local attention, CPU-friendly
```

**Note:** Full training still requires an NVIDIA GPU. These settings are for inference config extraction only.

---

## Safety Gates

`train.py` includes a `SAFETY_GATE` function that rejects agent-proposed diffs containing:
- `os.system`, `subprocess` — no shell execution
- Network calls (`requests`, `socket`, `urllib`) — no exfiltration
- Absolute file paths (`open('/'`) — no filesystem escape
- Hardcoded secrets or API keys

---

## License
MIT (same as upstream)
