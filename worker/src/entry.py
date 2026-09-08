import asyncio
import json
import math
import time
from urllib.parse import urlparse

from workers import Response, WorkerEntrypoint

from llm_client import MODEL_ALIASES, PURPOSE_LABELS, model_settings_text, set_model, usage_summary
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
from log import log_error
from messages import capture_message
from organize import (
    CRON_MAX_BATCH_CHARS,
    extract_undecided_section,
    get_doc_content,
    organize_trip,
    save_doc_revision,
    to_line_plaintext,
)
from polls import (
    add_option,
    end_poll,
    get_active_or_last_poll,
    get_active_poll,
    get_options_with_votes,
    poll_results_text,
    start_poll,
    toggle_vote,
)
from qa import answer_question
from trips import end_trip, get_active_or_last_trip, get_active_trip, is_admin, start_trip


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
/模型：查看目前各階段用哪個AI模型（修改僅限管理員）
/說明：顯示這則說明

Bot只會在旅程進行中被動記錄訊息，其餘時間不會插話，所有回覆都要靠上面的指令觸發。"""


class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env, ctx):
        # env/ctx params are unreliable under local --test-scheduled; self.env is always bound.
        active = await self.env.DB.prepare("SELECT id FROM trips WHERE status = 'active'").all()
        for trip in active.results:
            try:
                # 300s read + 300s connect + a much larger batch cap, not the webhook path's
                # tight defaults: scheduled() has its own ~15min wall-clock budget (Cloudflare
                # cron limit), not the ~30s ctx.waitUntil() ceiling /整理 and /問 share. Learned
                # the hard way - the 20s timeout default alone made every hourly auto-organize
                # fail once the backlog grew past what a quick call could fold in, and since a
                # failed attempt leaves organized_at unset, the backlog only grew further each
                # missed hour. 180s later proved not generous enough either (found live: a
                # small, unremarkable batch consistently took Claude longer than that to
                # respond, for reasons never fully pinned down - not a platform kill, a clean
                # client-side APITimeoutError). read+connect worst case is deliberately kept
                # around 600s total, well under the real 15min ceiling, leaving ~5min of
                # headroom for D1 queries, other active trips in the same sweep, and the
                # fact_check call that follows - hitting that 15min wall instead of our own
                # timeout would reproduce the exact "no catchable exception" platform-kill
                # problem this whole timeout parameter exists to avoid. Raising either one costs
                # nothing extra even on failure - the provider bills for tokens actually
                # generated, not for however long we waited to receive them.
                _t0 = time.time()
                # connect_timeout=300.0, not the webhook-safe 30.0 default: live diagnostics
                # (elapsed=30.4s, cause_chain=['ConnectTimeout', 'AbortError']) showed a past
                # failure was in the connect phase, not the read phase - raising read_timeout
                # alone did nothing that time because it wasn't the bottleneck. Widened from the
                # original 90.0 to give a slow/flaky connect more room to recover on its own
                # before giving up - read_timeout dropped from 600.0 to 300.0 in the same move so
                # the combined worst case doesn't grow past the ~600s total budget above.
                result = await organize_trip(
                    self.env, trip["id"], read_timeout=300.0, max_batch_chars=CRON_MAX_BATCH_CHARS,
                    connect_timeout=300.0,
                )  # no-ops cheaply if nothing pending
            except Exception as e:
                # temporary: elapsed time + full cause chain, to tell "genuinely waited out a
                # long timeout" apart from "failed fast for an unrelated connection reason that
                # just happens to also raise APITimeoutError" - remove once this is root-caused.
                elapsed = time.time() - _t0
                cause_chain = []
                c = e.__cause__
                while c is not None:
                    cause_chain.append(f"{type(c).__name__}: {c}")
                    c = c.__cause__
                log_error(
                    f"scheduled organize_trip error trip={trip['id']} elapsed={elapsed:.1f}s "
                    f"cause_chain={cause_chain}",
                    e,
                )
                continue
            if result.new_doc:
                # best-effort second pass, only on the cron path (not manual /整理) so it
                # never competes with /整理's tight waitUntil budget for time.
                try:
                    await fact_check_trip(self.env, trip["id"], result.new_doc, result.batch_text)
                except Exception as e:
                    log_error(f"scheduled fact_check_trip error trip={trip['id']}", e)

    async def fetch(self, request):
        path = urlparse(str(request.url)).path
        method = request.method

        if method == "POST" and path == "/webhook":
            return await self._handle_webhook(request)
        if method == "GET" and path == "/liff":
            return Response(render_liff_page(self.env.LIFF_ID), headers={"Content-Type": "text/html; charset=utf-8"})

        segments = [s for s in path.split("/") if s]
        if len(segments) >= 3 and segments[0] == "api" and segments[1] == "trips":
            trip_id = segments[2]
            if len(segments) == 3 and method == "GET":
                return await self._get_trip_api(trip_id)
            if len(segments) == 4 and segments[3] == "doc" and method == "PATCH":
                return await self._update_doc_api(request, trip_id)
            if len(segments) == 5 and segments[3] == "doc" and segments[4] == "revisions" and method == "GET":
                return await self._list_doc_revisions_api(trip_id)
            if (
                len(segments) == 7 and segments[3] == "doc" and segments[4] == "revisions"
                and segments[6] == "restore" and method == "POST"
            ):
                return await self._restore_doc_revision_api(request, trip_id, segments[5])
            if len(segments) == 4 and segments[3] == "expenses" and method == "POST":
                return await self._create_expense_api(request, trip_id)
            if len(segments) == 5 and segments[3] == "expenses" and method == "PATCH":
                return await self._update_expense_api(request, trip_id, segments[4])
            if len(segments) == 5 and segments[3] == "expenses" and method == "DELETE":
                return await self._delete_expense_api(trip_id, segments[4])
            if len(segments) == 6 and segments[3] == "polls" and segments[5] == "options" and method == "POST":
                return await self._add_poll_option_api(request, trip_id, segments[4])
            if (
                len(segments) == 8 and segments[3] == "polls" and segments[5] == "options"
                and segments[7] == "vote" and method == "POST"
            ):
                return await self._toggle_vote_api(request, trip_id, segments[4], segments[6])

        return Response("itinerary-manager", status=200)

    async def _create_expense_api(self, request, trip_id: str):
        body = json.loads(await request.text())
        error = self._validate_expense_body(body)
        if error:
            return Response.json({"error": error}, status=400)
        reply = await add_expense(
            self.env, trip_id, body["userId"], body.get("displayName", body["userId"]),
            float(body["amount"]), body["description"].strip(), body["participants"],
        )
        return Response.json({"message": reply})

    async def _update_expense_api(self, request, trip_id: str, expense_id: str):
        if not await self._expense_belongs_to_trip(expense_id, trip_id):
            return Response.json({"error": "not found"}, status=404)
        body = json.loads(await request.text())
        error = self._validate_expense_body(body, require_payer=False)
        if error:
            return Response.json({"error": error}, status=400)
        await update_expense(self.env, expense_id, float(body["amount"]), body["description"].strip(), body["participants"])
        return Response.json({"ok": True})

    async def _delete_expense_api(self, trip_id: str, expense_id: str):
        if not await self._expense_belongs_to_trip(expense_id, trip_id):
            return Response.json({"error": "not found"}, status=404)
        await delete_expense(self.env, expense_id)
        return Response.json({"ok": True})

    async def _expense_belongs_to_trip(self, expense_id: str, trip_id: str) -> bool:
        row = await self.env.DB.prepare("SELECT 1 FROM expenses WHERE id = ? AND trip_id = ?").bind(expense_id, trip_id).all()
        return bool(row.results)

    async def _update_doc_api(self, request, trip_id: str):
        body = json.loads(await request.text())
        content_md = body.get("content_md")
        user_id = body.get("userId")
        if not content_md or not user_id:
            return Response.json({"error": "missing content_md or userId"}, status=400)
        display_name = body.get("displayName") or user_id
        try:
            await save_doc_revision(
                self.env, trip_id, content_md,
                edited_by_user_id=user_id, edited_by_display_name=display_name,
            )
        except ValueError as e:
            return Response.json({"error": str(e)}, status=400)
        return Response.json({"ok": True})

    async def _list_doc_revisions_api(self, trip_id: str):
        # Full content_md per row, not a diff/summary - same "return everything in one shot"
        # shape the rest of this API already uses (expenses, poll options). LIMIT 30 bounds it
        # to a trip's recent history rather than every organize tick since the trip began.
        rows = await self.env.DB.prepare(
            "SELECT id, content_md, edited_by_display_name, created_at FROM trip_doc_revisions "
            "WHERE trip_id = ? ORDER BY created_at DESC LIMIT 30"
        ).bind(trip_id).all()
        revisions = [
            {
                "id": r["id"],
                "content_md": r["content_md"],
                "editor": r["edited_by_display_name"] or "🤖 AI整理",
                "created_at": r["created_at"],
            }
            for r in rows.results
        ]
        return Response.json({"revisions": revisions})

    async def _restore_doc_revision_api(self, request, trip_id: str, revision_id: str):
        row = await self.env.DB.prepare(
            "SELECT content_md FROM trip_doc_revisions WHERE id = ? AND trip_id = ?"
        ).bind(revision_id, trip_id).all()
        if not row.results:
            return Response.json({"error": "not found"}, status=404)
        body = json.loads(await request.text())
        user_id = body.get("userId")
        if not user_id:
            return Response.json({"error": "missing userId"}, status=400)
        display_name = body.get("displayName") or user_id
        # A restore is just another edit, recorded as its own new revision - never deletes or
        # rewrites history, so restoring an even-older version afterwards is always possible.
        try:
            await save_doc_revision(
                self.env, trip_id, row.results[0]["content_md"],
                edited_by_user_id=user_id, edited_by_display_name=display_name,
            )
        except ValueError as e:
            return Response.json({"error": str(e)}, status=400)
        return Response.json({"ok": True})

    async def _add_poll_option_api(self, request, trip_id: str, poll_id: str):
        poll = await self._get_poll_scoped(poll_id, trip_id)
        if not poll:
            return Response.json({"error": "not found"}, status=404)
        if poll["status"] != "active":
            return Response.json({"error": "poll is not active"}, status=400)
        body = json.loads(await request.text())
        text = (body.get("text") or "").strip()
        if not text:
            return Response.json({"error": "missing text"}, status=400)
        message = await add_option(self.env, poll_id, text)
        return Response.json({"message": message})

    async def _toggle_vote_api(self, request, trip_id: str, poll_id: str, option_id: str):
        poll = await self._get_poll_scoped(poll_id, trip_id)
        if not poll:
            return Response.json({"error": "not found"}, status=404)
        if poll["status"] != "active":
            return Response.json({"error": "poll is not active"}, status=400)
        option_row = await self.env.DB.prepare(
            "SELECT 1 FROM poll_options WHERE id = ? AND poll_id = ?"
        ).bind(option_id, poll_id).all()
        if not option_row.results:
            return Response.json({"error": "not found"}, status=404)
        body = json.loads(await request.text())
        user_id = body.get("userId")
        if not user_id:
            return Response.json({"error": "missing userId"}, status=400)
        display_name = body.get("displayName") or user_id
        voted = await toggle_vote(self.env, option_id, user_id, display_name)
        return Response.json({"voted": voted})

    async def _get_poll_scoped(self, poll_id: str, trip_id: str) -> dict | None:
        # ended polls are supposed to be locked (/投票 結束 promises this) - callers use
        # this to reject writes against a poll that isn't 'active', not just check scoping.
        row = await self.env.DB.prepare("SELECT status FROM polls WHERE id = ? AND trip_id = ?").bind(poll_id, trip_id).all()
        return row.results[0] if row.results else None

    @staticmethod
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

    async def _get_trip_api(self, trip_id: str):
        row = await self.env.DB.prepare(
            "SELECT t.name AS name, t.status AS status, t.line_group_id AS line_group_id, d.content_md AS content_md "
            "FROM trips t LEFT JOIN trip_docs d ON d.trip_id = t.id WHERE t.id = ?"
        ).bind(trip_id).all()
        if not row.results:
            return Response.json({"error": "not found"}, status=404)
        r = row.results[0]
        expenses = await list_expenses(self.env, trip_id)
        settlement = format_settlement(expenses)
        participants = await default_participants(self.env, r["line_group_id"])

        poll = await get_active_or_last_poll(self.env, trip_id)
        poll_data = None
        if poll:
            poll_data = {
                "id": poll["id"],
                "topic": poll["topic"],
                "status": poll["status"],
                "options": await get_options_with_votes(self.env, poll["id"]),
            }

        return Response.json({
            "name": r["name"],
            "status": r["status"],
            "content_md": r["content_md"] or "",
            "expenses": expenses,
            "settlement": settlement,
            "participants": participants,
            "poll": poll_data,
        })

    async def _handle_webhook(self, request):
        body = await request.text()
        signature = request.headers.get("x-line-signature")
        if not verify_signature(body, signature, self.env.LINE_CHANNEL_SECRET):
            return Response("invalid signature", status=403)

        # LINE expects a 2xx response within ~2s or it treats the delivery as failed/retries.
        # organize_trip (Claude call + several D1 writes) routinely takes several seconds, so
        # event handling must not block the response — defer it via waitUntil and ack immediately.
        payload = json.loads(body)
        for event in payload.get("events", []):
            self.ctx.waitUntil(self._handle_event(event))
        return Response("OK", status=200)

    async def _handle_unsend(self, event) -> None:
        line_message_id = (event.get("unsend") or {}).get("messageId")
        if not line_message_id:
            return

        # A user needs to actually see the message before recalling it, but this event and the
        # original "message" event are two independent webhook deliveries running concurrently
        # (each its own waitUntil task) - a fast enough recall can have this SELECT run before
        # capture_message's INSERT lands. One retry after a short wait covers that race for both
        # the message row itself and (since capture_message inserts the message row before
        # awaiting _ensure_attachment) its attachment row.
        row = await self.env.DB.prepare(
            "SELECT id, trip_id, msg_type, unsent_at FROM messages WHERE line_message_id = ?"
        ).bind(line_message_id).all()
        if not row.results:
            await asyncio.sleep(1.5)
            row = await self.env.DB.prepare(
                "SELECT id, trip_id, msg_type, unsent_at FROM messages WHERE line_message_id = ?"
            ).bind(line_message_id).all()
        if not row.results:
            return  # never captured (e.g. sent before any trip was active) - nothing to retract
        msg = row.results[0]
        if msg["unsent_at"]:
            return  # redelivered/duplicate unsend event - already processed, avoid a redundant reset

        # DB state first, R2 cleanup best-effort after: if PHOTOS.delete() throws (transient R2
        # error), the retraction itself must not be lost just because storage cleanup failed -
        # there's no LINE-level retry once this webhook has already returned 200.
        await self.env.DB.prepare(
            "UPDATE messages SET unsent_at = ?, organized_at = NULL WHERE id = ?"
        ).bind(int(time.time()), msg["id"]).run()

        if msg["msg_type"] == "image":
            try:
                atts = await self.env.DB.prepare(
                    "SELECT r2_key FROM attachments WHERE message_id = ?"
                ).bind(msg["id"]).all()
                for a in atts.results:
                    await self.env.PHOTOS.delete(a["r2_key"])
                await self.env.DB.prepare("DELETE FROM attachments WHERE message_id = ?").bind(msg["id"]).run()
            except Exception as e:
                # ponytail: an attachment row that's still mid-insert when the retry above
                # re-checks (rare - capture_message's R2 upload would have to outlast 1.5s) is
                # left as an orphaned R2 object. Acceptable at this app's storage scale; a sweep
                # job would be the fix if that ever shows up as a real cost.
                log_error("handle_unsend attachment cleanup error", e)

        trip = await self.env.DB.prepare("SELECT status FROM trips WHERE id = ?").bind(msg["trip_id"]).all()
        if trip.results and trip.results[0]["status"] == "ended":
            # An ended trip is never revisited by scheduled() (active trips only) or /整理
            # (requires an active trip), so organized_at=NULL here would otherwise sit
            # unprocessed forever - catch up immediately instead of leaving it stuck. This runs
            # on the webhook path (unsend arrives the same way /整理 does), NOT cron, so it
            # keeps the webhook-safe default timeout/batch size - passing cron's more generous
            # values here would reintroduce the exact ctx.waitUntil() risk fixed earlier tonight.
            try:
                await organize_trip(self.env, msg["trip_id"])
            except Exception as e:
                log_error("handle_unsend ended-trip catch-up error", e)

    async def _handle_event(self, event):
        if event.get("type") == "unsend":
            # no caller left to see an exception once this event is deferred via waitUntil -
            # log explicitly rather than let an R2/D1 hiccup fail silently like the dispatch
            # path used to before this session added the same logging there.
            try:
                await self._handle_unsend(event)
            except Exception as e:
                log_error("handle_unsend error", e)
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
                    self.env.LINE_CHANNEL_ACCESS_TOKEN, reply_token,
                    [{"type": "text", "text": WARN + "這個功能只能在群組裡使用，請把我加入群組後再試一次。"}],
                )
            return

        if is_command:
            user_id = source.get("userId")
            # Command handling runs deferred (see _handle_webhook) with no caller left to see an
            # exception — without this, a failure here is silent: no reply, no retry (LINE already
            # got its 200). Fall back to telling the user something broke instead of going quiet.
            try:
                reply = await self._dispatch_command(message["text"].strip(), message, group_id, user_id)
            except Exception as e:
                log_error("dispatch_command error", e)
                reply = WARN + "處理指令時發生錯誤，請稍後再試一次。"
            if reply and reply_token:
                messages = reply if isinstance(reply, list) else [{"type": "text", "text": reply}]
                await reply_messages(self.env.LINE_CHANNEL_ACCESS_TOKEN, reply_token, messages)
            return

        # any non-command message: passive capture, only if a trip is active
        await capture_message(self.env, group_id, event)

    async def _dispatch_command(self, text: str, message: dict, group_id: str, user_id: str) -> str | list[dict] | None:
        parts = text.split(maxsplit=2)
        cmd = parts[0]

        if cmd == "/說明":
            return HELP_TEXT

        if cmd == "/旅程":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "開始":
                name = parts[2] if len(parts) > 2 else ""
                return await start_trip(self.env, group_id, user_id, name)
            if sub == "結束":
                reply, ended_trip_id = await end_trip(self.env, group_id, user_id)
                if ended_trip_id:
                    settlement = await settlement_summary(self.env, ended_trip_id)
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

            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"

            payer_display_name = await get_display_name(self.env.LINE_CHANNEL_ACCESS_TOKEN, user_id)

            mentionees = (message.get("mention") or {}).get("mentionees", [])
            if mentionees:
                participants = await resolve_mentions(self.env, mentionees)
                # LINE doesn't let you @mention yourself, so there's no way to type your
                # way into the split - always include the payer instead of requiring it.
                if not any(p["line_user_id"] == user_id for p in participants):
                    participants.append({"line_user_id": user_id, "display_name": payer_display_name})
            else:
                participants = await default_participants(self.env, group_id)

            return await add_expense(
                self.env, trip["id"], user_id, payer_display_name, amount, description, participants
            )

        if cmd == "/結算":
            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            return await settlement_summary(self.env, trip["id"])

        if cmd == "/投票":
            sub = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else ""

            if sub == "開始":
                trip = await get_active_trip(self.env, group_id)
                if not trip:
                    return WARN + "請先 /旅程 開始 才能開投票"
                return await start_poll(self.env, trip["id"], user_id, arg)

            if sub == "新增":
                trip = await get_active_trip(self.env, group_id)
                if not trip:
                    return WARN + "目前沒有進行中的旅程"
                poll = await get_active_poll(self.env, trip["id"])
                if not poll:
                    return WARN + "目前沒有進行中的投票，請先 /投票 開始 <題目>"
                return await add_option(self.env, poll["id"], arg)

            if sub == "結果":
                trip = await get_active_or_last_trip(self.env, group_id)
                if not trip:
                    return WARN + "目前沒有旅程資料"
                return await poll_results_text(self.env, trip["id"])

            if sub == "結束":
                trip = await get_active_trip(self.env, group_id)
                if not trip:
                    return WARN + "目前沒有進行中的旅程"
                return await end_poll(self.env, trip["id"])

            return WARN + "指令格式：/投票 開始 <題目>｜/投票 新增 <選項>｜/投票 結果｜/投票 結束"

        if cmd == "/整理":
            trip = await get_active_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有進行中的旅程"
            count = (await organize_trip(self.env, trip["id"])).count
            return (OK + f"整理完成，處理了 {count} 則訊息。") if count else (WARN + "目前沒有新訊息可整理")

        if cmd == "/檢查":
            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            return await fact_check_summary(self.env, trip["id"])

        if cmd == "/模型":
            sub = parts[1] if len(parts) > 1 else ""
            label_to_purpose = {label: purpose for purpose, label in PURPOSE_LABELS.items()}
            if not sub:
                return await model_settings_text(self.env)
            if not is_admin(self.env, user_id):
                return WARN + "只有管理員能修改模型設定"
            purpose = label_to_purpose.get(sub)
            alias = parts[2] if len(parts) > 2 else ""
            if not purpose or alias not in MODEL_ALIASES:
                aliases = "、".join(MODEL_ALIASES)
                labels = "｜".join(PURPOSE_LABELS.values())
                return WARN + f"格式：/模型 {labels} <{aliases}>"
            await set_model(self.env, purpose, MODEL_ALIASES[alias])
            return OK + f"已把「{sub}」的模型設定為 {MODEL_ALIASES[alias]}"

        if cmd == "/問":
            question = text[len("/問"):].strip()  # not parts[1:]: the question itself may contain spaces
            if not question:
                return WARN + "請在 /問 後面接你的問題，例如：/問 我們住哪間飯店"
            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            return await answer_question(self.env, trip["id"], question)

        if cmd == "/未定事項":
            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            doc = await get_doc_content(self.env, trip["id"])
            section = extract_undecided_section(doc) if doc else ""
            return (f"❓ 未定事項：\n{to_line_plaintext(section)}") if section else (OK + "目前沒有待決定事項")

        if cmd == "/花費":
            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            return await usage_summary(self.env, trip["id"])

        if cmd == "/懶人包":
            trip = await get_active_or_last_trip(self.env, group_id)
            if not trip:
                return WARN + "目前沒有旅程資料"
            liff_url = f"https://liff.line.me/{self.env.LIFF_ID}?tripId={trip['id']}"
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
