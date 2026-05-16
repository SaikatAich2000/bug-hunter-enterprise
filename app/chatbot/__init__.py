"""Sleuth — Bug Hunter's built-in AI assistant.

Sleuth is the in-app conversational assistant. Users can ask questions
in natural language ("show open bugs assigned to alice") AND request
actions ("close bug 5", "comment on #12: works for me"). Every write
goes through an explicit Yes/Cancel confirmation prompt and is recorded
in the same audit log the REST API uses.

Architecture — three layers, ordered by cost:

  message ──► nlu (rules)  ──► executor (read)  ──► blocks
                  │                       │
                  ├─► classifier (TF-IDF)  ├─► actions (write, audited)
                  │                       │
                  └─► llm (optional)       └─► memory (per-user context)

  - nlu.py        Layer 1: pure-Python regex parser. Microseconds.
                  Catches ~80% of typical queries deterministically.
  - classifier.py Layer 2: TF-IDF + cosine similarity over a hand-curated
                  corpus. ~1 ms. Catches paraphrases the rules miss.
  - llm.py        Layer 3: OPTIONAL local llama.cpp inference. Lazy-loaded
                  if a GGUF model is dropped into models/. NEVER calls an
                  external API. Refuses to load when the container is too
                  small to fit the model (see memory_budget()).
  - executor.py   Read intents (list/count/detail/stats/export) → SQL
                  SELECTs only. Never writes.
  - actions.py    Write intents (assign/close/comment/create/...) →
                  permission-checked, audited, atomic mutations.
  - memory.py     Per-user conversation context with TTL. Resolves
                  pronouns ("it", "that bug") and stages pending
                  confirmations.
  - excel.py      In-memory Excel rendering with openpyxl.
  - router.py     FastAPI router exposing /api/chat/ask and the
                  download endpoint for generated files.

Database safety guarantee: Sleuth adds NO new tables. Read intents only
issue SELECTs. Writes are atomic and roll back fully on error or
permission denial. See tests/test_sleuth_safety.py for the verified
properties.

Privacy: Sleuth makes NO outbound HTTP calls. No data leaves the box.
The optional Layer 3 LLM runs locally via llama.cpp. There are no API
keys to configure.
"""

__all__ = [
    "nlu", "executor", "actions", "memory", "classifier", "llm",
    "excel", "router",
]
