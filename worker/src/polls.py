import time
import uuid
from collections import defaultdict

from icons import WARN


async def get_active_poll(env, trip_id: str) -> dict | None:
    result = await env.DB.prepare(
        "SELECT id, topic, status, created_by_line_user_id FROM polls WHERE trip_id = ? AND status = 'active'"
    ).bind(trip_id).all()
    return result.results[0] if result.results else None


async def get_active_or_last_poll(env, trip_id: str) -> dict | None:
    poll = await get_active_poll(env, trip_id)
    if poll:
        return poll
    result = await env.DB.prepare(
        "SELECT id, topic, status, created_by_line_user_id FROM polls WHERE trip_id = ? ORDER BY created_at DESC LIMIT 1"
    ).bind(trip_id).all()
    return result.results[0] if result.results else None


ALREADY_ACTIVE_MSG = WARN + "這趟旅程已經有進行中的投票了，請先 /投票 結束 再開新的"


async def start_poll(env, trip_id: str, user_id: str, topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return WARN + "請輸入投票題目，例如：/投票 開始 晚餐吃什麼"

    if await get_active_poll(env, trip_id):
        return ALREADY_ACTIVE_MSG

    poll_id = str(uuid.uuid4())
    now = int(time.time())
    try:
        await env.DB.prepare(
            "INSERT INTO polls (id, trip_id, topic, status, created_by_line_user_id, created_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)"
        ).bind(poll_id, trip_id, topic, user_id, now).run()
    except Exception:
        # idx_polls_one_active_per_trip caught a race: someone else's /投票 開始
        # committed between our check above and this insert.
        return ALREADY_ACTIVE_MSG
    return f"🗳️ 投票「{topic}」開始了！用 /投票 新增 <選項> 加入候選項目，到LIFF頁面（/懶人包）投票。"


async def get_options_with_votes(env, poll_id: str) -> list[dict]:
    options_result = await env.DB.prepare(
        "SELECT id, text FROM poll_options WHERE poll_id = ? ORDER BY created_at"
    ).bind(poll_id).all()
    options = options_result.results
    if not options:
        return []

    ids = [o["id"] for o in options]
    placeholder = ", ".join("?" for _ in ids)
    votes_result = await env.DB.prepare(
        f"SELECT poll_option_id, line_user_id, display_name FROM poll_votes WHERE poll_option_id IN ({placeholder})"
    ).bind(*ids).all()

    votes_by_option = defaultdict(list)
    for v in votes_result.results:
        votes_by_option[v["poll_option_id"]].append(
            {"line_user_id": v["line_user_id"], "display_name": v["display_name"]}
        )

    return [{"id": o["id"], "text": o["text"], "votes": votes_by_option[o["id"]]} for o in options]


async def add_option(env, poll_id: str, text: str) -> str:
    text = text.strip()
    if not text:
        return WARN + "選項內容不能是空的"

    option_id = str(uuid.uuid4())
    now = int(time.time())
    await env.DB.prepare(
        "INSERT INTO poll_options (id, poll_id, text, created_at) VALUES (?, ?, ?, ?)"
    ).bind(option_id, poll_id, text, now).run()

    options = await get_options_with_votes(env, poll_id)
    listing = "\n".join(f"{i + 1}. {o['text']}" for i, o in enumerate(options))
    return f"➕ 已新增選項「{text}」。目前選項：\n{listing}"


async def toggle_vote(env, option_id: str, user_id: str, display_name: str) -> bool:
    """Returns True if the user now has a vote on this option, False if it was just removed."""
    existing = await env.DB.prepare(
        "SELECT 1 FROM poll_votes WHERE poll_option_id = ? AND line_user_id = ?"
    ).bind(option_id, user_id).all()
    if existing.results:
        await env.DB.prepare(
            "DELETE FROM poll_votes WHERE poll_option_id = ? AND line_user_id = ?"
        ).bind(option_id, user_id).run()
        return False

    now = int(time.time())
    try:
        await env.DB.prepare(
            "INSERT INTO poll_votes (poll_option_id, line_user_id, display_name, voted_at) VALUES (?, ?, ?, ?)"
        ).bind(option_id, user_id, display_name, now).run()
    except Exception:
        pass  # a concurrent toggle (double-tap, retry) already inserted the same row - still "voted", same outcome
    return True


def format_results(topic: str, options: list[dict], status: str) -> str:
    if not options:
        return f"🗳️ 投票「{topic}」目前還沒有任何選項"
    lines = [f"🗳️ 投票「{topic}」{'（已結束）' if status == 'ended' else ''}結果："]
    for i, o in enumerate(options):
        names = "、".join(v["display_name"] for v in o["votes"]) or "（尚無人投）"
        lines.append(f"{i + 1}. {o['text']}：{len(o['votes'])}票（{names}）")
    return "\n".join(lines)


async def poll_results_text(env, trip_id: str) -> str:
    poll = await get_active_or_last_poll(env, trip_id)
    if not poll:
        return WARN + "目前沒有任何投票"
    options = await get_options_with_votes(env, poll["id"])
    return format_results(poll["topic"], options, poll["status"])


async def end_poll(env, trip_id: str) -> str:
    poll = await get_active_poll(env, trip_id)
    if not poll:
        return WARN + "目前沒有進行中的投票"

    now = int(time.time())
    await env.DB.prepare("UPDATE polls SET status = 'ended', ended_at = ? WHERE id = ?").bind(now, poll["id"]).run()

    options = await get_options_with_votes(env, poll["id"])
    return "🏁 投票結束！\n" + format_results(poll["topic"], options, "ended")
