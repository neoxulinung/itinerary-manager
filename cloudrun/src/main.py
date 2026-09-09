import asyncio
import hmac
import json
import math
import time

from fastapi import FastAPI, Request, Response

from config import Env, get_env
from expenses import (
    add_expense,
    default_participants,
    delete_expense,
    format_settlement,
    list_expenses,
    resolve_mentions,
    settlement_summary,
    update_expense,
)
from fact_check import fact_check_summary, fact_check_trip
from icons import OK, WARN
from liff_page import render as render_liff_page
from line_client import get_display_name, reply_messages, verify_signature
from llm_client import ANSWER_MODEL, FACT_CHECK_MODEL, MODEL_PRICES, ORGANIZE_MODEL, usage_summary
from messages import capture_message
from organize import (
    extract_undecided_section,
    get_doc_content,
    organize_trip,
    resync_display_names,
    save_doc_revision,
    to_line_plaintext,
)
from polls import (
    add_option,
    end_poll,
    get_active_or_last_poll,
    get_active_poll,
    get_options_with_votes,
    start_poll,
    poll_results_text,
    toggle_vote,
)
from qa import answer_question
from trips import end_trip, get_active_or_last_trip, get_active_trip, start_trip

app = FastAPI()

# LINE's basic ID for your channel - not a secret (it's the same public ID anyone finds by
# searching for your bot in LINE), but it does point at your specific running bot, so it isn't
# filled in here. Get yours with:
#   curl -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" https://api.line.me/v2/bot/info
# and hardcode the result below - it's effectively permanent for a given channel, not worth a
# deploy-config round trip to change.
LINE_ADD_FRIEND_URL = "https://line.me/R/ti/p/@<your-bot-basic-id>"

ADD_FRIEND_NUDGE = {
    "type": "template",
    "altText": f"記得加我好友：{LINE_ADD_FRIEND_URL}",
    "template": {
        "type": "buttons",
        # get_display_name (line_client.py) can only resolve a real name for people who've
        # added the bot as a friend - anyone who hasn't shows up as a raw LINE user ID in the
        # doc/replies instead of their name. Surfacing this at /旅程 開始 time is cheaper than
        # everyone finding out later from a document full of "U6a4d51e9..." citations.
        "text": "還沒加我好友的人記得加一下，不然之後訊息裡你的名字會顯示成一串英數字",
        "actions": [{"type": "uri", "label": "➕ 加好友", "uri": LINE_ADD_FRIEND_URL}],
    },
}

HELP_TEXT = """🤖 可用指令：

🧳 旅程管理
/旅程 開始 <名稱>：開始一趟新旅程，開始記錄群組討論
/旅程 結束：結束目前旅程（只有開始的人能結束，會自動附上記帳結算）

🔍 查詢
/問 <問題>：問Bot任何跟旅程有關的問題
/未定事項：查看目前還沒決定的事項
/懶人包：取得旅程時間軸網頁連結（時間軸、記帳、投票都在這裡）
/花費：查看目前AI使用花費
/結算：查看目前記帳結算狀況

💰 記帳
/記帳 <金額> <說明> [@人1 @人2...]：記一筆代墊款項，我是付款人；@人就那些人分攤，不@就旅程期間發言過的所有人一起分攤。編輯/刪除帳目請到LIFF頁面操作。

🗳️ 投票
/投票 開始 <題目>：開一個新投票（同時只能一個進行中）
/投票 新增 <選項>：加入候選項目
/投票 結果：查看目前得票狀況
/投票 結束：鎖定結果，任何人都能結束
投票/取消投票請到LIFF頁面操作，可複選。

⚙️ 其他
/整理：手動整理目前累積的討論（平常會自動整理，不用手動點）
/檢查：查看AI整理時有沒有查到可疑或沒根據的內容（每小時自動檢查一次）
/懶人包：可以在LIFF頁面調整這趟旅程各階段要用哪個AI模型
/說明：顯示這則說明

Bot只會在旅程進行中被動記錄訊息，其餘時間不會插話，所有回覆都要靠上面的指令觸發。"""


@app.post("/webhook")
async def webhook(request: Request):
    env = get_env()
    body = await request.body()
    body_text = body.decode("utf-8")
    signature = request.headers.get("x-line-signature")
    if not verify_signature(body_text, signature, env.line_channel_secret):
        return Response(content="invalid signature", status_code=403)

    payload = json.loads(body_text)
    for event in payload.get("events", []):
        # Processed synchronously, NOT via Cloudflare's ctx.waitUntil() (see worker/src/entry.py
        # for the original) - the whole reason for this migration is that Cloud Run has no
        # equivalent guarantee that background work survives past the response, and no ~30s
        # ceiling in the first place that would have required deferring it. Just await the real
        # work before replying - same fix already validated on line-team-recorder.
        await _handle_event(env, event)
    return Response(content="OK", status_code=200)


async def _handle_unsend(env: Env, event: dict) -> None:
    line_message_id = (event.get("unsend") or {}).get("messageId")
    if not line_message_id:
        return

    row = await env.db.query(
        "SELECT id, trip_id, msg_type, unsent_at FROM messages WHERE line_message_id = ?", [line_message_id]
    )
    if not row.results:
        # The "unsend" event and the original "message" event are two independent webhook
        # deliveries (separate HTTP requests) - a fast enough recall can have this SELECT run
        # before capture_message's INSERT lands. One retry after a short wait covers that race
        # for both the message row itself and (since capture_message inserts the message row
        # before awaiting _ensure_attachment) its attachment row.
        await asyncio.sleep(1.5)
        row = await env.db.query(
            "SELECT id, trip_id, msg_type, unsent_at FROM messages WHERE line_message_id = ?", [line_message_id]
        )
    if not row.results:
        return  # never captured (e.g. sent before any trip was active) - nothing to retract
    msg = row.results[0]
    if msg["unsent_at"]:
        return  # redelivered/duplicate unsend event - already processed, avoid a redundant reset

    # DB state first, R2 cleanup best-effort after: if the R2 delete throws (transient error),
    # the retraction itself must not be lost just because storage cleanup failed - there's no
    # LINE-level retry once this webhook has already returned 200.
    await env.db.query(
        "UPDATE messages SET unsent_at = ?, organized_at = NULL WHERE id = ?",
        [int(time.time()), msg["id"]],
    )

    if msg["msg_type"] == "image":
        try:
            atts = await env.db.query("SELECT r2_key FROM attachments WHERE message_id = ?", [msg["id"]])
            for a in atts.results:
                await env.r2.delete(a["r2_key"])
            await env.db.query("DELETE FROM attachments WHERE message_id = ?", [msg["id"]])
        except Exception as e:
            print(f"[_handle_unsend attachment cleanup error] {type(e).__name__}: {e}")

    trip = await env.db.query("SELECT status FROM trips WHERE id = ?", [msg["trip_id"]])
    if trip.results and trip.results[0]["status"] == "ended":
        # An ended trip is never revisited by the scheduled-organize sweep (active trips only)
        # or /整理 (requires an active trip), so organized_at=NULL here would otherwise sit
        # unprocessed forever - catch up immediately instead of leaving it stuck.
        try:
            await organize_trip(env, msg["trip_id"])
        except Exception as e:
            print(f"[_handle_unsend ended-trip catch-up error] {type(e).__name__}: {e}")


async def _handle_event(env: Env, event: dict) -> None:
    try:
        if event.get("type") == "unsend":
            await _handle_unsend(env, event)
            return
        if event.get("type") != "message":
            return

        message = event.get("message")
        if not message:
            return  # malformed/unexpected event shape (e.g. a LINE console "Verify" probe)

        source = event.get("source", {})
        group_id = source.get("groupId")
        reply_token = event.get("replyToken")
        is_command = message["type"] == "text" and message["text"].strip().startswith("/")

        if not group_id:
            # Bot only supports group chats (not a 1:1 DM or a LINE "room"). Only push back on
            # an explicit command attempt — stay quiet for ordinary chat sent straight to the bot.
            if is_command and reply_token:
                await reply_messages(
                    env.line_channel_access_token, reply_token,
                    [{"type": "text", "text": WARN + "這個功能只能在群組裡使用，請把我加入群組後再試一次。"}],
                )
            return

        if is_command:
            user_id = source.get("userId")
            try:
                reply = await _dispatch_command(env, message["text"].strip(), message, group_id, user_id)
            except Exception as e:
                print(f"[dispatch_command error] {type(e).__name__}: {e}")
                reply = WARN + "處理指令時發生錯誤，請稍後再試一次。"
            if reply and reply_token:
                messages = reply if isinstance(reply, list) else [{"type": "text", "text": reply}]
                await reply_messages(env.line_channel_access_token, reply_token, messages)
            return

        # any non-command message: passive capture, only if a trip is active
        await capture_message(env, group_id, event)
    except Exception as e:
        # Kept as one outer catch spanning the whole function (not just around dispatch, as the
        # Cloudflare version had it): /webhook now awaits every event in the payload in a plain
        # loop rather than firing each into its own ctx.waitUntil() task, so one malformed event
        # raising here must not abort the rest of the batch.
        print(f"[_handle_event error] {type(e).__name__}: {e}")


async def _dispatch_command(env: Env, text: str, message: dict, group_id: str, user_id: str) -> str | list[dict] | None:
    parts = text.split(maxsplit=2)
    cmd = parts[0]

    if cmd == "/說明":
        return HELP_TEXT

    if cmd == "/旅程":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "開始":
            name = parts[2] if len(parts) > 2 else ""
            reply, new_trip_id = await start_trip(env, group_id, user_id, name)
            if not new_trip_id:
                return reply
            return [{"type": "text", "text": reply}, ADD_FRIEND_NUDGE]
        if sub == "結束":
            reply, ended_trip_id = await end_trip(env, group_id, user_id)
            if ended_trip_id:
                settlement = await settlement_summary(env, ended_trip_id)
                return [{"type": "text", "text": reply}, {"type": "text", "text": settlement}]
            return reply
        return WARN + "指令格式：/旅程 開始 <名稱> 或 /旅程 結束"

    if cmd == "/記帳":
        rest = text[len("/記帳"):].strip()
        fields = rest.split(maxsplit=1)
        if not fields:
            return WARN + "格式：/記帳 <金額> <說明> [@人1 @人2...]"
        try:
            amount = float(fields[0])
        except ValueError:
            return WARN + "金額格式錯誤。格式：/記帳 <金額> <說明> [@人1 @人2...]"
        if amount <= 0 or not math.isfinite(amount):
            return WARN + "金額要大於0"
        description = fields[1] if len(fields) > 1 else "（無說明）"

        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"

        payer_display_name = await get_display_name(env.line_channel_access_token, user_id)

        mentionees = (message.get("mention") or {}).get("mentionees", [])
        if mentionees:
            participants = await resolve_mentions(env, mentionees)
            # LINE doesn't let you @mention yourself, so there's no way to type your
            # way into the split - always include the payer instead of requiring it.
            if not any(p["line_user_id"] == user_id for p in participants):
                participants.append({"line_user_id": user_id, "display_name": payer_display_name})
        else:
            participants = await default_participants(env, group_id)

        return await add_expense(env, trip["id"], user_id, payer_display_name, amount, description, participants)

    if cmd == "/結算":
        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"
        return await settlement_summary(env, trip["id"])

    if cmd == "/投票":
        sub = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "開始":
            trip = await get_active_trip(env, group_id)
            if not trip:
                return WARN + "請先 /旅程 開始 才能開投票"
            return await start_poll(env, trip["id"], user_id, arg)

        if sub == "新增":
            trip = await get_active_trip(env, group_id)
            if not trip:
                return WARN + "目前沒有進行中的旅程"
            poll = await get_active_poll(env, trip["id"])
            if not poll:
                return WARN + "目前沒有進行中的投票，請先 /投票 開始 <題目>"
            return await add_option(env, poll["id"], arg)

        if sub == "結果":
            trip = await get_active_or_last_trip(env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            return await poll_results_text(env, trip["id"])

        if sub == "結束":
            trip = await get_active_trip(env, group_id)
            if not trip:
                return WARN + "目前沒有進行中的投票"
            return await end_poll(env, trip["id"])

        return WARN + "指令格式：/投票 開始 <題目>｜/投票 新增 <選項>｜/投票 結果｜/投票 結束"

    if cmd == "/整理":
        trip = await get_active_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有進行中的旅程"
        count = (await organize_trip(env, trip["id"])).count
        return (OK + f"整理完成，處理了 {count} 則訊息。") if count else (WARN + "目前沒有新訊息可整理")

    if cmd == "/檢查":
        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"
        return await fact_check_summary(env, trip["id"])

    if cmd == "/問":
        question = text[len("/問"):].strip()  # not parts[1:]: the question itself may contain spaces
        if not question:
            return WARN + "請在 /問 後面接你的問題，例如：/問 我們住哪間飯店"
        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"
        return await answer_question(env, trip["id"], question)

    if cmd == "/未定事項":
        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"
        doc = await get_doc_content(env, trip["id"])
        section = extract_undecided_section(doc) if doc else ""
        return (f"❓ 未定事項：\n{to_line_plaintext(section)}") if section else (OK + "目前沒有待決定事項")

    if cmd == "/花費":
        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"
        return await usage_summary(env, trip["id"])

    if cmd == "/懶人包":
        trip = await get_active_or_last_trip(env, group_id)
        if not trip:
            return WARN + "目前沒有旅程資料"
        liff_url = f"https://liff.line.me/{env.liff_id}?tripId={trip['id']}"
        return [{
            "type": "template",
            "altText": f"🧳「{trip['name']}」旅程懶人包：{liff_url}",
            "template": {
                "type": "buttons",
                "text": f"🧳「{trip['name']}」時間軸・記帳・投票",
                "actions": [{"type": "uri", "label": "📖 開啟懶人包", "uri": liff_url}],
            },
        }]

    return None


@app.get("/liff")
async def liff():
    env = get_env()
    return Response(content=render_liff_page(env.liff_id), media_type="text/html; charset=utf-8")


@app.get("/api/trips/{trip_id}")
async def get_trip_api(trip_id: str):
    env = get_env()
    row = await env.db.query(
        "SELECT t.name AS name, t.status AS status, t.line_group_id AS line_group_id, "
        "t.organize_model AS organize_model, t.answer_model AS answer_model, "
        "t.fact_check_model AS fact_check_model, d.content_md AS content_md "
        "FROM trips t LEFT JOIN trip_docs d ON d.trip_id = t.id WHERE t.id = ?",
        [trip_id],
    )
    if not row.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    r = row.results[0]
    expenses = await list_expenses(env, trip_id)
    settlement = format_settlement(expenses)
    participants = await default_participants(env, r["line_group_id"])

    poll = await get_active_or_last_poll(env, trip_id)
    poll_data = None
    if poll:
        poll_data = {
            "id": poll["id"],
            "topic": poll["topic"],
            "status": poll["status"],
            "options": await get_options_with_votes(env, poll["id"]),
        }

    return {
        "name": r["name"],
        "status": r["status"],
        "content_md": r["content_md"] or "",
        # Raw nullable, not resolved against the llm_client.py default - the model picker needs
        # to tell "no override" apart from "override happens to equal today's default" so it
        # can pre-select 使用預設 instead of silently pinning the current default as an explicit
        # override the first time someone opens and saves the form without changing anything.
        "organize_model": r["organize_model"],
        "answer_model": r["answer_model"],
        "fact_check_model": r["fact_check_model"],
        "expenses": expenses,
        "settlement": settlement,
        "participants": participants,
        "poll": poll_data,
    }


@app.patch("/api/trips/{trip_id}/doc")
async def update_doc_api(trip_id: str, request: Request):
    env = get_env()
    body = json.loads(await request.body())
    content_md = body.get("content_md")
    user_id = body.get("userId")
    if not content_md or not user_id:
        return Response(content=json.dumps({"error": "missing content_md or userId"}), status_code=400, media_type="application/json")
    display_name = body.get("displayName") or user_id
    try:
        await save_doc_revision(env, trip_id, content_md, edited_by_user_id=user_id, edited_by_display_name=display_name)
    except ValueError as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")
    return {"ok": True}


@app.get("/api/trips/{trip_id}/doc/revisions")
async def list_doc_revisions_api(trip_id: str):
    env = get_env()
    # Full content_md per row, not a diff/summary - same "return everything in one shot" shape
    # the rest of this API already uses (expenses, poll options). LIMIT 30 bounds it to a
    # trip's recent history rather than every organize tick since the trip began.
    rows = await env.db.query(
        "SELECT id, content_md, edited_by_display_name, created_at FROM trip_doc_revisions "
        "WHERE trip_id = ? ORDER BY created_at DESC LIMIT 30",
        [trip_id],
    )
    revisions = [
        {
            "id": r["id"],
            "content_md": r["content_md"],
            "editor": r["edited_by_display_name"] or "🤖 AI整理",
            "created_at": r["created_at"],
        }
        for r in rows.results
    ]
    return {"revisions": revisions}


@app.post("/api/trips/{trip_id}/doc/revisions/{revision_id}/restore")
async def restore_doc_revision_api(trip_id: str, revision_id: str, request: Request):
    env = get_env()
    row = await env.db.query(
        "SELECT content_md FROM trip_doc_revisions WHERE id = ? AND trip_id = ?", [revision_id, trip_id]
    )
    if not row.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    body = json.loads(await request.body())
    user_id = body.get("userId")
    if not user_id:
        return Response(content=json.dumps({"error": "missing userId"}), status_code=400, media_type="application/json")
    display_name = body.get("displayName") or user_id
    # A restore is just another edit, recorded as its own new revision - never deletes or
    # rewrites history, so restoring an even-older version afterwards is always possible.
    try:
        await save_doc_revision(
            env, trip_id, row.results[0]["content_md"], edited_by_user_id=user_id, edited_by_display_name=display_name
        )
    except ValueError as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")
    return {"ok": True}


@app.post("/api/trips/{trip_id}/resync-names")
async def resync_names_api(trip_id: str):
    env = get_env()
    exists = await env.db.query("SELECT 1 FROM trips WHERE id = ?", [trip_id])
    if not exists.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    changed = await resync_display_names(env, trip_id)
    return {"changed": changed}


@app.get("/api/model-options")
async def get_model_options_api():
    # Real model IDs, not itineraryManager's old /模型 chat-command aliases (sonnet/haiku/...) -
    # those existed to keep a typed command short, a <select> in the UI doesn't need that.
    return {
        "models": list(MODEL_PRICES),
        "defaults": {"organize": ORGANIZE_MODEL, "answer": ANSWER_MODEL, "fact_check": FACT_CHECK_MODEL},
    }


@app.patch("/api/trips/{trip_id}/model")
async def update_model_api(trip_id: str, request: Request):
    # No admin gate, unlike the old /模型 command - matches every other LIFF write endpoint on
    # this page (doc edit, prompt-equivalent settings) already having none. Empty/missing value
    # per purpose = reset to the llm_client.py default (stored back as NULL, not the resolved
    # default itself, so a future redeploy that changes the default constant still takes effect
    # for anyone who never explicitly picked a model).
    env = get_env()
    body = json.loads(await request.body())
    exists = await env.db.query("SELECT 1 FROM trips WHERE id = ?", [trip_id])
    if not exists.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")

    updates = {}
    for column in ("organize_model", "answer_model", "fact_check_model"):
        if column not in body:
            continue
        value = (body.get(column) or "").strip()
        if value and value not in MODEL_PRICES:
            return Response(
                content=json.dumps({"error": f"unknown model for {column}: {value}"}), status_code=400, media_type="application/json"
            )
        updates[column] = value or None
    if not updates:
        return {"ok": True}

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    await env.db.query(f"UPDATE trips SET {set_clause} WHERE id = ?", [*updates.values(), trip_id])
    return {"ok": True}


async def _expense_belongs_to_trip(env: Env, expense_id: str, trip_id: str) -> bool:
    row = await env.db.query("SELECT 1 FROM expenses WHERE id = ? AND trip_id = ?", [expense_id, trip_id])
    return bool(row.results)


def _validate_expense_body(body: dict, require_payer: bool = True) -> str | None:
    if require_payer and not body.get("userId"):
        return "missing userId"
    try:
        amount = float(body.get("amount", 0))
        if amount <= 0 or not math.isfinite(amount):
            return "amount must be positive"
    except (TypeError, ValueError):
        return "invalid amount"
    if not (body.get("description") or "").strip():
        return "missing description"
    if not body.get("participants"):
        return "missing participants"
    return None


@app.post("/api/trips/{trip_id}/expenses")
async def create_expense_api(trip_id: str, request: Request):
    env = get_env()
    body = json.loads(await request.body())
    error = _validate_expense_body(body)
    if error:
        return Response(content=json.dumps({"error": error}), status_code=400, media_type="application/json")
    reply = await add_expense(
        env, trip_id, body["userId"], body.get("displayName", body["userId"]),
        float(body["amount"]), body["description"].strip(), body["participants"],
    )
    return {"message": reply}


@app.patch("/api/trips/{trip_id}/expenses/{expense_id}")
async def update_expense_api(trip_id: str, expense_id: str, request: Request):
    env = get_env()
    if not await _expense_belongs_to_trip(env, expense_id, trip_id):
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    body = json.loads(await request.body())
    error = _validate_expense_body(body, require_payer=False)
    if error:
        return Response(content=json.dumps({"error": error}), status_code=400, media_type="application/json")
    await update_expense(env, expense_id, float(body["amount"]), body["description"].strip(), body["participants"])
    return {"ok": True}


@app.delete("/api/trips/{trip_id}/expenses/{expense_id}")
async def delete_expense_api(trip_id: str, expense_id: str):
    env = get_env()
    if not await _expense_belongs_to_trip(env, expense_id, trip_id):
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    await delete_expense(env, expense_id)
    return {"ok": True}


async def _get_poll_scoped(env: Env, poll_id: str, trip_id: str) -> dict | None:
    # ended polls are supposed to be locked (/投票 結束 promises this) - callers use this to
    # reject writes against a poll that isn't 'active', not just check scoping.
    row = await env.db.query("SELECT status FROM polls WHERE id = ? AND trip_id = ?", [poll_id, trip_id])
    return row.results[0] if row.results else None


@app.post("/api/trips/{trip_id}/polls/{poll_id}/options")
async def add_poll_option_api(trip_id: str, poll_id: str, request: Request):
    env = get_env()
    poll = await _get_poll_scoped(env, poll_id, trip_id)
    if not poll:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    if poll["status"] != "active":
        return Response(content=json.dumps({"error": "poll is not active"}), status_code=400, media_type="application/json")
    body = json.loads(await request.body())
    text = (body.get("text") or "").strip()
    if not text:
        return Response(content=json.dumps({"error": "missing text"}), status_code=400, media_type="application/json")
    message = await add_option(env, poll_id, text)
    return {"message": message}


@app.post("/api/trips/{trip_id}/polls/{poll_id}/options/{option_id}/vote")
async def toggle_vote_api(trip_id: str, poll_id: str, option_id: str, request: Request):
    env = get_env()
    poll = await _get_poll_scoped(env, poll_id, trip_id)
    if not poll:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    if poll["status"] != "active":
        return Response(content=json.dumps({"error": "poll is not active"}), status_code=400, media_type="application/json")
    option_row = await env.db.query("SELECT 1 FROM poll_options WHERE id = ? AND poll_id = ?", [option_id, poll_id])
    if not option_row.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    body = json.loads(await request.body())
    user_id = body.get("userId")
    if not user_id:
        return Response(content=json.dumps({"error": "missing userId"}), status_code=400, media_type="application/json")
    display_name = body.get("displayName") or user_id
    voted = await toggle_vote(env, option_id, user_id, display_name)
    return {"voted": voted}


@app.post("/internal/scheduled-organize")
async def scheduled_organize(request: Request):
    # Hit by Cloud Scheduler on an interval - takes the place of the Cloudflare Cron Trigger's
    # scheduled() handler (worker/src/entry.py). Shared-secret header, not IAM auth: simplest
    # thing that works at this app's scale, same pattern already validated on line-team-recorder.
    env = get_env()
    sent_secret = request.headers.get("x-scheduler-secret") or ""
    if not env.scheduler_secret or not hmac.compare_digest(sent_secret, env.scheduler_secret):
        return Response(status_code=403)

    active = await env.db.query("SELECT id FROM trips WHERE status = 'active'")
    for row in active.results:
        try:
            result = await organize_trip(env, row["id"])  # no-ops cheaply if nothing pending
        except Exception as e:
            print(f"[scheduled_organize organize_trip error] trip={row['id']} {type(e).__name__}: {e}")
            continue
        if result.new_doc:
            # best-effort second pass, only on this cron path (not manual /整理) so it never
            # competes with a chat command's own latency for time - same split the Cloudflare
            # version had, kept even though the platform reason for the split no longer applies.
            try:
                await fact_check_trip(env, row["id"], result.new_doc, result.batch_text, result.fact_check_model)
            except Exception as e:
                print(f"[scheduled_organize fact_check_trip error] trip={row['id']} {type(e).__name__}: {e}")
    return {"ok": True}


@app.get("/")
async def root():
    return Response(content="itinerary-manager", status_code=200)
