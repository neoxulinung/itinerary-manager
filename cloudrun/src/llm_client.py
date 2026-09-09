import asyncio
import time
import uuid

import anthropic
import openai

# USD per 1M tokens, first-party pricing (Anthropic + OpenAI). Update this table if pricing
# changes. Checked 2026-09: gpt-5/gpt-5-mini (both OpenAI's own general-purpose line) are
# superseded by the gpt-5.6 tier below - dropped from the picker menu rather than left in as a
# stale, no-longer-current option. GPT-6 Astra and GPT-5.5/5.4 exist too (per OpenAI's own
# pricing docs) but aren't listed here yet - no public source gives their exact API model ID
# string, and guessing one risks a runtime failure the first time someone actually picks it.
MODEL_PRICES = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "gpt-5.6-sol": {"input": 5.00, "output": 30.00},
    "gpt-5.6-terra": {"input": 2.00, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
}

# These are just the fallback defaults now - per-trip overrides live in trips.organize_model/
# answer_model/fact_check_model (NULL = use these), set from the LIFF page. Previously a global
# `settings`-table override changed only via the admin-gated /模型 chat command - replaced with
# a per-trip, UI-driven picker (same design line-team-recorder's sibling project settled on),
# so any global override set through the old /模型 command no longer applies; every trip starts
# back at these defaults until its own model is explicitly picked from the LIFF page.
ORGANIZE_MODEL = "claude-sonnet-5"
ANSWER_MODEL = "claude-haiku-4-5"
FACT_CHECK_MODEL = "claude-haiku-4-5"


async def call_llm(
    env, purpose: str, trip_id: str | None, model: str, system: str, user_content: str, max_tokens: int = 8000,
) -> str:
    # No connect/read timeout split, no httpx2, no max_retries=0 fight against a platform-level
    # kill: Cloud Run has no uncatchable ceiling like Cloudflare's ~30s ctx.waitUntil() budget
    # this originally guarded against (see worker/src/llm_client.py for the full history), so a
    # single generous, genuinely-catchable timeout is enough - same simplification already
    # validated on line-team-recorder's sibling Cloud Run service.
    if model.startswith("gpt-"):
        text, input_tokens, output_tokens = await _call_openai(env, model, system, user_content, max_tokens)
    else:
        text, input_tokens, output_tokens = await _call_anthropic(env, model, system, user_content, max_tokens)
    await _log_usage(env, purpose, trip_id, model, input_tokens, output_tokens)
    return text


async def _call_anthropic(env, model: str, system: str, user_content: str, max_tokens: int) -> tuple[str, int, int]:
    # max_retries=0 kept anyway even without the Cloudflare budget pressure - one clean attempt,
    # one clean failure, no reason to let a retry loop run long just because it can.
    client = anthropic.AsyncAnthropic(api_key=env.anthropic_api_key, timeout=120.0, max_retries=0)
    # thinking explicitly disabled: observed live - claude-sonnet-5 sometimes emits a 'thinking'
    # block unprompted (no thinking param was ever set), consuming most of the output budget
    # before the actual doc rewrite starts. Model behavior, not platform-specific - still applies.
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": user_content}],
    )
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break
    return text, response.usage.input_tokens, response.usage.output_tokens


async def _call_openai(env, model: str, system: str, user_content: str, max_tokens: int) -> tuple[str, int, int]:
    client = openai.AsyncOpenAI(api_key=env.openai_api_key, timeout=120.0, max_retries=0)
    response = await client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        # Same reasoning as thinking={"type":"disabled"} above - this is a reasoning model and
        # would otherwise spend part of the token budget on hidden reasoning by default. "none"
        # not "minimal": confirmed live against gpt-5.6 that "minimal" is no longer an accepted
        # value (400 Unsupported value) - the accepted range is now none/low/medium/high/xhigh.
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    text = response.choices[0].message.content or ""
    return text, response.usage.prompt_tokens, response.usage.completion_tokens


async def _log_usage(env, purpose, trip_id, model, input_tokens, output_tokens) -> None:
    prices = MODEL_PRICES.get(model, {"input": 0, "output": 0})
    cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
    await env.db.query(
        "INSERT INTO llm_usage (id, trip_id, purpose, model, input_tokens, output_tokens, estimated_cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), trip_id, purpose, model, input_tokens, output_tokens, cost, int(time.time())],
    )


async def usage_summary(env, trip_id: str) -> str:
    query = (
        "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
        "COALESCE(SUM(estimated_cost_usd),0) c FROM llm_usage"
    )
    trip_row, total_row = await asyncio.gather(
        env.db.query(query + " WHERE trip_id = ?", [trip_id]),
        env.db.query(query),
    )
    t, a = trip_row.results[0], total_row.results[0]
    return (
        f"🤖 這趟旅程：{t['n']}次AI呼叫，約 US${t['c']:.4f}（輸入{t['i']}／輸出{t['o']} tokens）\n"
        f"📊 全部旅程累積：{a['n']}次AI呼叫，約 US${a['c']:.4f}"
    )
