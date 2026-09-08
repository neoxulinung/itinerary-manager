import json
import re
import time
import uuid

from claude_client import call_claude, get_model
from icons import OK
from log import log_error

SYSTEM_PROMPT = """你是旅行規劃助手的內容查核員。你會收到一份旅程文件的最新版本，以及這次用來更新文件的原始LINE訊息。
文件裡的結論通常會附來源引用，例如 _(依 王小明 8/20 14:02 提及)_。

請檢查文件裡「因為這批原始訊息而新增或修改」的內容，是否真的能在這批原始訊息中找到根據——有沒有幻覺捏造、
張冠李戴（人名/日期對錯）、或跟原始訊息矛盾的地方。不用管文件裡跟這批訊息無關的舊內容。

只用JSON陣列回傳，不要有其他文字、不要用程式碼區塊包起來：
- 沒發現問題就回傳 []
- 有問題就回傳 [{"claim": "文件裡有問題的那句話", "reason": "簡短說明問題在哪"}, ...]"""


def _strip_code_fence(text: str) -> str:
    # ponytail: haiku wraps JSON in ```json ... ``` even when told not to (confirmed live) -
    # strip it rather than fight the model on prompt wording alone. Regex instead of
    # split("\n", 1): a fence with no embedded newline (entire fenced block on one line) made
    # the old split-based version discard the whole payload instead of just the fence markers.
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


async def fact_check_trip(env, trip_id: str, new_doc: str, batch_text: str) -> None:
    user_content = f"旅程文件（最新版）：\n{new_doc}\n\n---\n\n這批用來更新文件的原始訊息：\n{batch_text}"
    # max_tokens=4096, not 1024: a busy batch can legitimately flag several claims, and a JSON
    # array cut off mid-object fails to parse entirely (json.loads has no partial-recovery), so
    # too tight a cap silently drops every flag for the run, not just the ones that didn't fit.
    # this only ever runs from scheduled() (cron, ~15min budget) - never from the webhook path
    # (~30s budget) - so it's safe to always use the same generous timeout, no need to thread
    # a parameter through like organize_trip does.
    model = await get_model(env, "fact_check")
    raw = await call_claude(
        env, "fact_check", trip_id, model, SYSTEM_PROMPT, user_content, max_tokens=4096, read_timeout=120.0
    )
    try:
        issues = json.loads(_strip_code_fence(raw))
    except ValueError as e:
        log_error("fact_check_trip parse error", e)
        return  # ponytail: malformed JSON from a cheap model - skip this run, not worth retry machinery at this scale
    if not isinstance(issues, list):
        return

    now = int(time.time())
    for issue in issues:
        # defensive per-issue: haiku's JSON shape isn't guaranteed (already true enough to need
        # _strip_code_fence above) - a non-string claim/reason would otherwise raise .strip() on
        # something like an int, aborting the loop and silently dropping every flag after it.
        try:
            if not isinstance(issue, dict):
                continue
            claim = issue.get("claim")
            reason = issue.get("reason")
            if not isinstance(claim, str) or not isinstance(reason, str):
                continue
            claim, reason = claim.strip(), reason.strip()
            if not claim:
                continue
            await env.DB.prepare(
                "INSERT INTO doc_fact_check_flags (id, trip_id, claim, reason, created_at) VALUES (?, ?, ?, ?, ?)"
            ).bind(str(uuid.uuid4()), trip_id, claim, reason, now).run()
        except Exception as e:
            log_error("fact_check_trip issue error", e)


async def fact_check_summary(env, trip_id: str) -> str:
    rows = await env.DB.prepare(
        "SELECT claim, reason, created_at FROM doc_fact_check_flags WHERE trip_id = ? ORDER BY created_at DESC LIMIT 10"
    ).bind(trip_id).all()
    if not rows.results:
        return OK + "目前沒有發現可疑內容"
    lines = ["🔍 可能需要覆核的內容："]
    for r in rows.results:
        lines.append(f"・{r['claim']}\n   → {r['reason']}")
    return "\n".join(lines)
