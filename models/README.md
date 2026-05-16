# Local LLM models for Sleuth

Sleuth's third intelligence layer is an **optional** local LLM. If you
drop a GGUF model file into this directory and install
`llama-cpp-python`, Sleuth will use it as a fallback for queries the
rule parser and statistical classifier can't make sense of.

If you skip this, **Sleuth still works** — the rule parser handles ~80%
of typical queries and the classifier (Layer 2) catches another 10-15%.
The LLM is only consulted when both of those fail.

## Hardware reality

The deployment target is **1 CPU core, 2 GB RAM, no GPU**. That's tight.
The model has to fit in RAM alongside FastAPI, Postgres connections, and
the OS. Pick a small model.

Recommended:

| Model | Size on disk | RAM | Speed (tok/s, 1 CPU) | Quality |
|---|---|---|---|---|
| Qwen 2.5 0.5B Instruct Q4_K_M | ~370 MB | ~450 MB | 8–15 | good for intent JSON |
| SmolLM 360M Instruct Q4_K_M | ~230 MB | ~290 MB | 12–20 | acceptable |
| TinyLlama 1.1B Chat Q4_K_M | ~640 MB | ~750 MB | 4–8 | better, slower |

Anything bigger than ~750 MB **will not fit** alongside Postgres on a 2
GB box. Don't use Phi-3 mini, Llama-3 8B, etc. on this hardware.

## Install

```bash
pip install llama-cpp-python
```

(CPU build only — no CUDA / Metal flags needed.)

## Get a model

Pick one. The Qwen 0.5B is the best balance for our use case.

```bash
# From the bug-hunter root:
cd models

# Option 1: Qwen 2.5 0.5B Instruct (recommended)
wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -O sleuth.gguf

# Option 2: SmolLM 360M
wget https://huggingface.co/HuggingFaceTB/SmolLM-360M-Instruct-GGUF/resolve/main/smollm-360m-instruct-q4_k_m.gguf -O sleuth.gguf

# Option 3: TinyLlama 1.1B
wget https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf -O sleuth.gguf
```

The file MUST be named `sleuth.gguf` (or set `SLEUTH_LLM_MODEL_PATH` in
your environment to point elsewhere).

## Verify

Restart the app. The first time someone asks a question that falls
through to the LLM, it'll log:

```
Loading Sleuth LLM from /app/models/sleuth.gguf (this takes a few seconds)
Sleuth LLM loaded in 3.42s
```

After that, inference is on the order of 5–15 s per fallback query. If
you don't want LLM fallback at all, just leave this directory empty and
no model will load.

## Tuning

These environment variables control the LLM layer:

| Env var | Default | Purpose |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | `models/sleuth.gguf` | absolute path to the GGUF file |
| `SLEUTH_LLM_TIMEOUT_S` | `12` | inference budget in seconds |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `600` | unload model after N seconds idle |
| `SLEUTH_LLM_MAX_TOKENS` | `120` | cap on generated tokens per call |
| `SLEUTH_LLM_CTX_LEN` | `1024` | context window (lower = less RAM) |
| `SLEUTH_LLM_THREADS` | `1` | CPU threads (set to your core count) |

## Privacy

The LLM runs entirely on this server. No data leaves the box. Sleuth
never calls a hosted API, never shells out to any external endpoint,
and never sends user messages anywhere except through this local
inference path.
