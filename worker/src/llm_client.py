import asyncio
import time
import uuid

import anthropic
import httpx
import httpx2
import openai

# USD per 1M tokens, first-party pricing (Anthropic + OpenAI). Update this table if pricing changes.
MODEL_PRICES = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
}

# Default model per LLM stage. Overridable at runtime via the `settings` table (get_model /
# set_model below, changed with /模型) - takes effect on the very next call, no redeploy
# needed. These constants are just the fallback when no override row exists.
DEFAULT_ORGANIZE_MODEL = "claude-sonnet-5"
DEFAULT_ANSWER_MODEL = "claude-haiku-4-5"
DEFAULT_FACT_CHECK_MODEL = "claude-haiku-4-5"

_DEFAULT_MODEL = {
    "organize": DEFAULT_ORGANIZE_MODEL,
    "answer": DEFAULT_ANSWER_MODEL,
    "fact_check": DEFAULT_FACT_CHECK_MODEL,
}

# Short names /模型 accepts, mapped to real model IDs. Deliberately limited to models
# MODEL_PRICES actually has pricing for - selecting anything else would silently track cost
# as $0 rather than the real amount. Provider is inferred from the model ID prefix (call_llm
# below), not stored separately - "gpt-" -> OpenAI, everything else -> Anthropic.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "gpt5": "gpt-5",
    "gpt5mini": "gpt-5-mini",
}


async def get_model(env, purpose: str) -> str:
    row = await env.DB.prepare("SELECT value FROM settings WHERE key = ?").bind(f"{purpose}_model").all()
    return row.results[0]["value"] if row.results else _DEFAULT_MODEL[purpose]


async def set_model(env, purpose: str, model: str) -> None:
    await env.DB.prepare(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
    ).bind(f"{purpose}_model", model, int(time.time())).run()


PURPOSE_LABELS = {"organize": "整理", "answer": "問答", "fact_check": "查核"}


async def model_settings_text(env) -> str:
    current = await asyncio.gather(*(get_model(env, p) for p in PURPOSE_LABELS))
    lines = [f"{label}：{model}" for label, model in zip(PURPOSE_LABELS.values(), current)]
    aliases = "、".join(MODEL_ALIASES)
    return (
        "🤖 目前模型設定：\n" + "\n".join(lines) + f"\n\n可選：{aliases}\n"
        f"設定方式（僅限管理員）：/模型 " + "｜".join(PURPOSE_LABELS.values()) + f" <{aliases}>"
    )


async def call_llm(
    env, purpose: str, trip_id: str | None, model: str, system: str, user_content: str,
    max_tokens: int = 8000, read_timeout: float = 20.0, connect_timeout: float = 30.0,
) -> str:
    # ponytail: repeated APITimeoutError on /整理 traced to httpx2_jsfetch's AbortController
    # firing at the default 5s *connect* timeout, not a slow response - Cloudflare's Worker-to-
    # Anthropic TLS handshake occasionally takes longer than that. Widen just the connect leg.
    # read/write/pool default to 20.0 (NOT the SDK's 600s default): code review caught that the
    # positional arg to httpx2.Timeout sets read/write/pool too, and 600s directly contradicts
    # the reason this override exists - Cloudflare kills /整理 and /問's ctx.waitUntil() ~30s
    # after the webhook responds, so a client-side timeout that can't fire until minutes in
    # guarantees the platform silently kills the task first (no reply at all) instead of us
    # failing cleanly.
    # read_timeout AND connect_timeout are both caller-supplied, NOT hardcoded, because the safe
    # value is completely different depending on who's calling: the webhook path (/整理, /問)
    # shares a single ~30s ctx.waitUntil() budget across the whole request - connect can't
    # safely exceed that default 30s regardless, there's no budget left over for it - but
    # scheduled()'s cron sweep gets its own ~15-minute wall-clock budget with no such ceiling.
    # Found live: even 600s of read_timeout didn't fix a week-long stuck backlog, because the
    # actual failure was ConnectTimeout (confirmed via e.__cause__), not a slow response -
    # raising read_timeout alone can never help a connect-phase failure. Hardcoding either one
    # once broke the hourly auto-organize outright: a real backlog (116 pending messages after
    # several missed cron ticks) legitimately needed more than a tight default to fold in, so
    # every cron attempt kept failing the same way and the backlog kept growing - a
    # self-reinforcing spiral. scheduled() now passes larger values for both; the tight defaults
    # stay for the webhook path where they're actually required. Same reasoning applies to the
    # OpenAI path below - it's a Cloudflare ctx.waitUntil()/cron budget problem, not an
    # Anthropic-specific one.
    # max_retries=0: both SDKs retry timeout/connection errors by default (up to 2x), which
    # would let 3 attempts at connect_timeout each add up to 3x that - blowing past Cloudflare's
    # 30s ctx.waitUntil() budget for /整理 and /問 and getting the whole task killed with no
    # reply at all, the exact "worse than the original bug" failure already hit once this
    # session with a manual retry wrapper. One clean attempt, one clean failure.
    if model.startswith("gpt-"):
        text, input_tokens, output_tokens = await _call_openai(
            env, model, system, user_content, max_tokens, read_timeout, connect_timeout
        )
    else:
        text, input_tokens, output_tokens = await _call_anthropic(
            env, model, system, user_content, max_tokens, read_timeout, connect_timeout
        )
    await _log_usage(env, purpose, trip_id, model, input_tokens, output_tokens)
    return text


async def _call_anthropic(
    env, model: str, system: str, user_content: str, max_tokens: int, read_timeout: float, connect_timeout: float,
) -> tuple[str, int, int]:
    client = anthropic.AsyncAnthropic(
        api_key=env.ANTHROPIC_API_KEY,
        timeout=httpx2.Timeout(read_timeout, connect=connect_timeout),
        max_retries=0,
    )
    # thinking explicitly disabled: observed live - claude-sonnet-5 sometimes emits a 'thinking'
    # block unprompted (no thinking param was ever set), consuming most of the output budget
    # before the actual doc rewrite starts. Lowering max_tokens to "starve" it (tried first) just
    # truncated the real output instead. This task (rewrite a markdown doc per fixed rules)
    # doesn't need reasoning; disabling it outright is the actual fix.
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


async def _call_openai(
    env, model: str, system: str, user_content: str, max_tokens: int, read_timeout: float, connect_timeout: float,
) -> tuple[str, int, int]:
    client = openai.AsyncOpenAI(
        api_key=env.OPENAI_API_KEY,
        timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
        max_retries=0,
    )
    response = await client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        # ponytail: reasoning_effort="minimal" is the OpenAI equivalent of the
        # thinking={"type": "disabled"} fix above - gpt-5 is a reasoning model and defaults to
        # spending part of max_completion_tokens on hidden reasoning tokens before the visible
        # answer, same failure shape already hit once with Claude's unprompted thinking blocks.
        # This task never needs reasoning, so skip it outright rather than re-discover the same
        # bug live.
        reasoning_effort="minimal",
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
    await env.DB.prepare(
        "INSERT INTO llm_usage (id, trip_id, purpose, model, input_tokens, output_tokens, estimated_cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    ).bind(str(uuid.uuid4()), trip_id, purpose, model, input_tokens, output_tokens, cost, int(time.time())).run()


async def usage_summary(env, trip_id: str) -> str:
    query = (
        "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
        "COALESCE(SUM(estimated_cost_usd),0) c FROM llm_usage"
    )
    trip_row, total_row = await asyncio.gather(
        env.DB.prepare(query + " WHERE trip_id = ?").bind(trip_id).all(),
        env.DB.prepare(query).all(),
    )
    t, a = trip_row.results[0], total_row.results[0]
    return (
        f"🤖 這趟旅程：{t['n']}次AI呼叫，約 US${t['c']:.4f}（輸入{t['i']}／輸出{t['o']} tokens）\n"
        f"📊 全部旅程累積：{a['n']}次AI呼叫，約 US${a['c']:.4f}"
    )
