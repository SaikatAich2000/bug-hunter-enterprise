"""Sleuth, the application's built-in assistant.

Layers by cost: nlu (regex) -> classifier (TF-IDF) -> llm (local llama.cpp)
-> cloud_llm (Groq primary, OpenRouter fallback; opt-in, read-only, redacted).
Reads are SQL SELECTs (executor); writes are confirmed, audited (actions).
"""

__all__ = [
    "nlu", "executor", "actions", "memory", "classifier", "llm",
    "cloud_llm", "rag", "redaction", "excel", "router",
]
