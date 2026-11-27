"""Persist standard token rates in USD per token."""
import math
from threading import Lock

import httpx

from .database import connect, now, transaction

CATALOG_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
REFRESH_LOCK = Lock()


def status(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='model_prices_updated_at'").fetchone()
    return {
        "updated_at": row["value"] if row else None,
        "model_count": conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0],
    }


def refresh(*, initial=False):
    with REFRESH_LOCK:
        if initial:
            with connect() as conn:
                saved = status(conn)
            if saved["updated_at"]:
                return saved
        response = httpx.get(CATALOG_URL, timeout=10, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid model pricing catalog")
        rows = {}
        for model, entry in data.items():
            if not isinstance(entry, dict) or entry.get("mode") not in ("chat", "completion", "responses"):
                continue

            def rate(key):
                value = entry.get(key)
                return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

            incoming, outgoing = rate("input_cost_per_token"), rate("output_cost_per_token")
            if incoming is None or outgoing is None:
                continue
            cached = rate("cache_read_input_token_cost")
            if cached is None:
                cached = rate("input_cost_per_token_cache_hit")
            rows[model.strip().lower()] = (incoming, cached, rate("cache_creation_input_token_cost"), outgoing)
        if not rows:
            raise ValueError("Empty model pricing catalog")
        with transaction() as conn:
            conn.execute("DELETE FROM model_prices")
            conn.executemany("INSERT INTO model_prices VALUES (?, ?, ?, ?, ?)", ((key, *row) for key, row in rows.items()))
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('model_prices_updated_at', ?)", (now(),))
            return status(conn)


def estimate(conn, launch):
    if launch["model_mixed"] or launch["model"] is None:
        return None
    model = launch["model"].strip().lower()
    if launch["cli"] == "kimi" and model.startswith("kimi-code/"):
        model = "moonshot/kimi-" + model.removeprefix("kimi-code/").removeprefix("kimi-")
    if model in {"grok 4.7", "grok-4.7", "grok-4.7-low", "grok-4.7-medium", "grok-4.7-high", "grok-4.7-xhigh"}:
        model = "xai/grok-4.7"
    rates = conn.execute("SELECT * FROM model_prices WHERE model=?", (model,)).fetchone()
    if not rates:
        return None
    writes = launch["cache_write_tokens"]
    factors = (
        (launch["input_tokens"] - writes, rates["input_rate"]),
        (launch["cache_tokens"], rates["cache_rate"]),
        (writes, rates["cache_write_rate"]),
        (launch["output_tokens"], rates["output_rate"]),
    )
    if any(tokens and value is None for tokens, value in factors):
        return None
    if launch["usage_partial"] and not any(tokens for tokens, _ in factors):
        return None
    return sum(tokens * (value or 0) for tokens, value in factors)
