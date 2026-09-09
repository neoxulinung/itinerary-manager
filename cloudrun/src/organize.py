import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from line_client import refresh_display_name
from llm_client import FACT_CHECK_MODEL, ORGANIZE_MODEL, call_llm

# How many already-organized messages to show as context before the new batch, so the LLM
# can resolve references/follow-ups that span a cron boundary (e.g. "還沒定" answering a
# question asked in a previous hour's batch, not repeated verbatim in the distilled doc).
CONTEXT_MESSAGE_COUNT = 15

# One batch-size cap, not two: the Cloudflare version needed a tiny cap for its webhook path
# (~30s ctx.waitUntil() budget) and a much larger one for its cron path (~15min budget) - see
# worker/src/organize.py for the full history. Cloud Run has no such split, the same generous
# timeout applies everywhere, so one cap is enough. Set to the old cron value (the generous one,
# not the tight webhook one) since Cloud Run's /整理 can now safely clear backlogs just as fast
# as cron always could.
MAX_BATCH_CHARS = 8000
# Flat per-message char estimate for images when computing the MAX_BATCH_CHARS budget - their
# real "[傳送照片] 圖片連結: <url>" line isn't known until image_urls is resolved, which happens
# after batching, so a fixed stand-in (typical R2 public URL + prefix length) is used instead.
IMAGE_CHAR_ESTIMATE = 150
# Sanity ceiling on the SQL fetch itself - must comfortably exceed what MAX_BATCH_CHARS could
# need if messages run short (plenty do - one-word replies), or the fetch would silently cap the
# batch below the intended char budget before the accumulation loop even runs.
MAX_FETCH_ROWS = 1000

TW_TZ = timezone(timedelta(hours=8))

EMPTY_DOC_TEMPLATE = "# {name}\n\n## 時間軸\n\n## 未定事項\n\n## 其他資訊\n"

SYSTEM_PROMPT = """你是旅行規劃助手的整理引擎。你會收到一份目前的旅程markdown文件、一段近期對話（僅供參考），以及一批新的LINE群組討論訊息。
你的工作是把「新增的討論內容」中「旅遊規劃相關」的內容整合進文件裡，回傳完整的、更新後的整份markdown文件。

規則：
1. 只有旅遊規劃相關的內容才需要整理（行程、住宿、交通、餐廳、票券、集合時間等）。閒聊、貼圖、無關對話請忽略，不要為它們新增任何內容。
2. 只修改受「新增的討論內容」影響的部分，其餘既有內容原封不動保留，不要整篇重寫或改寫語氣。「近期對話」只是提供上下文幫助你理解「新增的討論內容」，不需要為「近期對話」本身新增或修改文件內容，也不要重複引用「近期對話」裡的訊息。
3. 每個新增或修改的結論，旁邊用一行小字附上來源引用，格式類似：_(依 王小明 8/20 14:02 提及)_，日期用訊息的實際日期。
4. 文件維持這個章節骨架：
   ## 時間軸 —— 依日期(Day1, Day2...)列出已經確定的行程，日期還不確定就寫「日期未定」
   ## 未定事項 —— 還在討論、尚未拍板的事情，用checkbox列表 `- [ ] ...`
   ## 其他資訊 —— 機票、訂房確認信、重要連結等不屬於時間軸的資訊
   「## 未定事項」這個標題必須完全保留（會被程式抓取），不要更名或拿掉。
5. 如果訊息讓某件事從未定變成已定（或反過來被推翻），把它從對應章節移過去，不要兩邊同時留著重複內容。
6. 如果訊息附了照片且與內容相關，用markdown圖片語法 `![說明](圖片URL)` 直接嵌入該段落。
7. 如果訊息內容本身包含網址連結（訂房連結、票券連結、地圖連結等），且與旅遊規劃相關，請把該網址原文保留在對應的段落裡，不要只用文字描述取代連結、也不要省略掉。
8. 不要憑空捏造內容、不要猜測日期，資料沒有明確提到就不要寫。
9. 對話中常有一人提問、另一人（甚至是自己）在後續幾則訊息才回答的情況（例如「機票訂了嗎」→ 幾則之後「13號」）。請先通盤讀過「新增的討論內容」，把問句和對應的回答串起來理解事情的全貌，不要只因為某則訊息單獨看起來像片段、太簡短，或跟前一句話中間隔了幾則其他訊息，就忽略它或誤判成閒聊。
10. 直接輸出完整更新後的markdown全文，不要加任何額外說明、不要用程式碼區塊包起來。
11. 如果「新增的討論內容」裡有標記「已收回訊息」的項目，代表發送者事後收回了那則訊息。請檢查文件裡有沒有根據那則訊息新增的內容，如果有，把它移除或修正（例如靠這則訊息才確定的行程要移回未定事項，或整段移除）；如果那則訊息從未被寫進文件裡，直接忽略即可，不需要新增任何內容。"""


async def get_doc_content(env, trip_id: str) -> str | None:
    doc_row = await env.db.query("SELECT content_md FROM trip_docs WHERE trip_id = ?", [trip_id])
    return doc_row.results[0]["content_md"] if doc_row.results else None


async def resync_display_names(env, trip_id: str) -> bool:
    # organize_trip's self-heal above only reaches text the LLM writes in its *next* pass, and
    # the organize prompt is told to preserve existing content - so a citation already written
    # against a stale UID (before that person friended the bot) never gets touched again by
    # /整理 alone, even once the DB itself is fixed. This is the on-demand version: scan every
    # speaker this trip has ever seen, refresh anyone still stale, and patch the literal UID
    # substring directly into the existing document text. UIDs are long unique tokens, so a
    # plain string replace is safe. Powers the LIFF page's 🔄 修正名稱顯示 button.
    current_doc = await get_doc_content(env, trip_id)
    if not current_doc:
        return False

    rows = await env.db.query(
        "SELECT DISTINCT line_user_id, user_display_name FROM messages WHERE trip_id = ?", [trip_id]
    )
    names = {row["line_user_id"]: row["user_display_name"] for row in rows.results}
    names.update(await _resolve_stale_names(env, list(names.items())))

    new_doc = current_doc
    for uid, name in names.items():
        if name != uid and uid in new_doc:
            new_doc = new_doc.replace(uid, name)
    if new_doc == current_doc:
        return False
    await save_doc_revision(env, trip_id, new_doc, edited_by_display_name="🤖 自動修正名稱顯示")
    return True


UNDECIDED_HEADING = "## 未定事項"


def validate_doc(doc: str) -> None:
    # Found live: the LLM can drop "# <trip name>" or "## 未定事項" entirely while leaving
    # everything else intact - no error, just a doc other features quietly can't parse anymore
    # (extract_undecided_section, and anything reading UNDECIDED_HEADING verbatim). A manual
    # edit from the LIFF page carries the same risk from one stray keystroke. Shared by every
    # writer of trip_docs (see save_doc_revision) so nothing - LLM output, a manual edit, or a
    # restore - can ever persist a doc the rest of the app can no longer parse.
    if not doc.strip().startswith("# "):
        raise ValueError("文件開頭必須是「# 旅程名稱」")
    if UNDECIDED_HEADING not in doc:
        raise ValueError(f"文件必須包含「{UNDECIDED_HEADING}」這個標題，不能刪除或改名")


async def save_doc_revision(
    env, trip_id: str, content_md: str, *,
    triggered_by_message_id: str | None = None,
    edited_by_user_id: str | None = None,
    edited_by_display_name: str | None = None,
) -> None:
    validate_doc(content_md)
    now = int(time.time())
    await env.db.query(
        "INSERT INTO trip_docs (trip_id, content_md, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(trip_id) DO UPDATE SET content_md = excluded.content_md, updated_at = excluded.updated_at",
        [trip_id, content_md, now],
    )
    await env.db.query(
        "INSERT INTO trip_doc_revisions "
        "(id, trip_id, content_md, triggered_by_message_id, edited_by_user_id, edited_by_display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), trip_id, content_md, triggered_by_message_id, edited_by_user_id, edited_by_display_name, now],
    )


def extract_undecided_section(doc: str) -> str:
    idx = doc.find(UNDECIDED_HEADING)
    if idx == -1:
        return ""
    rest = doc[idx + len(UNDECIDED_HEADING):]
    next_heading = rest.find("\n## ")
    section = rest if next_heading == -1 else rest[:next_heading]
    return section.strip()


def to_line_plaintext(md: str) -> str:
    # LINE chat bubbles don't render markdown - our own syntax (checkboxes, the
    # _(...)_ citation wrapper) would otherwise show up as literal noise.
    text = re.sub(r"_\(([^)]*)\)_", r"(\1)", md)
    text = re.sub(r"^- \[ \] ", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^- \[x\] ", "✓ ", text, flags=re.MULTILINE)
    text = re.sub(r"^- ", "• ", text, flags=re.MULTILINE)
    text = text.replace("**", "")
    return text


async def _resolve_stale_names(env, pairs: list[tuple[str, str]]) -> dict[str, str]:
    # pairs: (line_user_id, user_display_name). Returns {uid: fresh_name}, only for uids whose
    # stored name is still just the raw id - see line_client.refresh_display_name.
    stale_ids = list({uid for uid, name in pairs if name == uid})
    if not stale_ids:
        return {}
    # Concurrent, not sequential - /webhook awaits this whole call before replying to LINE (no
    # background task), so N sequential profile-API round trips would add straight to that
    # reply latency.
    fresh_names = await asyncio.gather(*(refresh_display_name(env, uid, uid) for uid in stale_ids))
    return {uid: fresh for uid, fresh in zip(stale_ids, fresh_names) if fresh != uid}


def _format_line(row, image_url: str | None) -> str:
    if row["msg_type"] not in ("text", "image"):
        return ""  # sticker/video/etc: nothing useful to organize from, skip (unsent or not)

    ts = datetime.fromtimestamp(row["sent_at"], tz=TW_TZ).strftime("%m/%d %H:%M")
    who = row["user_display_name"]
    if row["unsent_at"]:
        # The attachment (if any) is already deleted by the time this runs (see
        # main.py._handle_unsend), so there's never an image_url to show here regardless of
        # msg_type - just flag the retraction itself per SYSTEM_PROMPT rule 10.
        content = "（已收回一則訊息）" if row["msg_type"] == "image" else f"（已收回訊息，原內容：{row['text']}）"
    elif row["msg_type"] == "image":
        content = f"[傳送照片] 圖片連結: {image_url}" if image_url else "[傳送照片]（備份失敗，無法取得連結）"
    else:
        content = row["text"]
    return f"[{ts}] {who}: {content}"


class OrganizeResult(NamedTuple):
    count: int
    new_doc: str | None  # set only when the doc actually changed - the /檢查 fact-check pass needs it
    batch_text: str | None  # the raw "新增的討論內容" text used this run, same condition as new_doc
    # Effective fact_check_model for this trip - only meaningful when new_doc is set (the only
    # case main.py actually calls fact_check_trip), carried here so it doesn't need its own
    # SELECT for a trip row this function already fetched moments earlier.
    fact_check_model: str | None


async def organize_trip(env, trip_id: str, max_tokens: int = 8000) -> OrganizeResult:
    # ponytail: no lock around this read-modify-write of trip_docs. Three triggers can call
    # this for the same trip_id (hourly cron, manual /整理, auto on /旅程 結束) — a genuine
    # overlap would silently lose one side's edit. Matches the plan's accepted stance on
    # concurrency at this app's scale (friend-group chat cadence, not a real race in practice);
    # add a lock (e.g. an `organizing_at` guard column) if trips start showing dropped edits.
    pending = await env.db.query(
        "SELECT id, line_user_id, user_display_name, msg_type, text, sent_at, unsent_at FROM messages "
        "WHERE trip_id = ? AND organized_at IS NULL ORDER BY sent_at LIMIT ?",
        [trip_id, MAX_FETCH_ROWS],
    )
    all_pending = pending.results
    if not all_pending:
        return OrganizeResult(0, None, None, None)

    rows = []
    batch_chars = 0
    for row in all_pending:
        # text is NULL for non-text messages (messages.py never stores it for images), so an
        # image row would otherwise count as 0 chars here despite _format_line() embedding a
        # full R2 URL for it - letting an unbounded run of photos slip past MAX_BATCH_CHARS
        # entirely. Image URLs aren't resolved until after batching, so charge a flat estimate
        # instead (typical "[傳送照片] 圖片連結: <R2 url>" line length) rather than the real 0.
        row_chars = len(row["text"] or "") if row["msg_type"] == "text" else IMAGE_CHAR_ESTIMATE
        if rows and batch_chars + row_chars > MAX_BATCH_CHARS:
            break
        rows.append(row)
        batch_chars += row_chars

    context_rows_desc = await env.db.query(
        "SELECT id, line_user_id, user_display_name, msg_type, text, sent_at, unsent_at FROM messages "
        "WHERE trip_id = ? AND organized_at IS NOT NULL ORDER BY sent_at DESC LIMIT ?",
        [trip_id, CONTEXT_MESSAGE_COUNT],
    )
    context_rows = list(reversed(context_rows_desc.results))

    # Resolve any speaker whose stored name is still just their raw LINE userId (captured
    # before they'd friended the bot) - see line_client.refresh_display_name. This is the one
    # universal surface (every trip goes through organize_trip, unlike the opt-in-adjacent
    # expenses/polls paths that already did this) so it's the right place to self-heal it.
    resolved = await _resolve_stale_names(env, [(row["line_user_id"], row["user_display_name"]) for row in rows + context_rows])
    for row in rows + context_rows:
        if row["line_user_id"] in resolved:
            row["user_display_name"] = resolved[row["line_user_id"]]

    # code review caught: a context message can be an image too (organized_at gets set on
    # every row in a batch, images included) - look its URL up for real instead of hardcoding
    # "backup failed" for anything in context, which would misinform the LLM about a working URL.
    image_ids = [row["id"] for row in rows + context_rows if row["msg_type"] == "image"]
    image_urls: dict[str, str] = {}
    if image_ids:
        placeholder = ", ".join("?" for _ in image_ids)
        atts = await env.db.query(
            f"SELECT message_id, r2_key FROM attachments WHERE message_id IN ({placeholder})",
            image_ids,
        )
        image_urls = {a["message_id"]: env.r2.public_url(a["r2_key"]) for a in atts.results}

    lines = [line for row in rows if (line := _format_line(row, image_urls.get(row["id"])))]
    context_lines = [line for row in context_rows if (line := _format_line(row, image_urls.get(row["id"])))]

    trip_row = await env.db.query("SELECT name, organize_model, fact_check_model FROM trips WHERE id = ?", [trip_id])
    trip = trip_row.results[0]

    current_doc = await get_doc_content(env, trip_id)
    if current_doc is None:
        current_doc = EMPTY_DOC_TEMPLATE.format(name=trip["name"])

    batch_text = "\n".join(lines)
    context_block = (
        f"\n\n---\n\n近期對話紀錄（僅供參考，用來理解下方新訊息的上下文，不需要為這段內容本身更新文件）：\n"
        + "\n".join(context_lines)
        if context_lines else ""
    )
    user_content = (
        f"目前的旅程文件：\n{current_doc}"
        + context_block
        + f"\n\n---\n\n這是新增的討論內容（依時間排序）：\n{batch_text}"
    )

    model = trip["organize_model"] or ORGANIZE_MODEL
    new_doc = await call_llm(env, "organize", trip_id, model, SYSTEM_PROMPT, user_content, max_tokens=max_tokens)
    new_doc = new_doc.strip() or current_doc

    # validate_doc raises (and this batch's organized_at stays unset, so the next run - cron or
    # manual /整理 - retries it) if the LLM dropped the "# <trip name>" title or "## 未定事項"
    # heading - found live, both have happened while the rest of the doc stayed intact.
    last_message_id = rows[-1]["id"]
    changed = new_doc != current_doc
    if changed:
        await save_doc_revision(env, trip_id, new_doc, triggered_by_message_id=last_message_id)

    now = int(time.time())
    ids_placeholder = ", ".join("?" for _ in rows)
    await env.db.query(
        f"UPDATE messages SET organized_at = ? WHERE id IN ({ids_placeholder})",
        [now, *[row["id"] for row in rows]],
    )

    return OrganizeResult(
        len(rows),
        new_doc if changed else None,
        batch_text if changed else None,
        (trip["fact_check_model"] or FACT_CHECK_MODEL) if changed else None,
    )
