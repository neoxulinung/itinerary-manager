import time
import uuid

from icons import WARN
from log import log_error
from organize import organize_trip


async def get_active_trip(env, group_id: str) -> dict | None:
    result = await env.db.query(
        "SELECT id, name, owner_line_user_id FROM trips WHERE line_group_id = ? AND status = 'active'",
        [group_id],
    )
    return result.results[0] if result.results else None


async def get_active_or_last_trip(env, group_id: str) -> dict | None:
    trip = await get_active_trip(env, group_id)
    if trip:
        return trip
    result = await env.db.query(
        "SELECT id, name, owner_line_user_id FROM trips WHERE line_group_id = ? "
        "ORDER BY started_at DESC LIMIT 1",
        [group_id],
    )
    return result.results[0] if result.results else None


ALREADY_ACTIVE_MSG = WARN + "這個群組已經有進行中的旅程了，請先 /旅程 結束 再開新的"


async def start_trip(env, group_id: str, user_id: str, name: str) -> tuple[str, str | None]:
    """Returns (reply_text, trip_id). trip_id is only set when a trip actually started, so
    callers can tell success from a rejection message without string-matching (e.g. to decide
    whether to append the add-friend nudge)."""
    name = name.strip()
    if not name:
        return WARN + "請輸入旅程名稱，例如：/旅程 開始 東京五日遊", None

    if await get_active_trip(env, group_id):
        return ALREADY_ACTIVE_MSG, None

    trip_id = str(uuid.uuid4())
    now = int(time.time())
    try:
        await env.db.query(
            "INSERT INTO trips (id, line_group_id, name, status, owner_line_user_id, started_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            [trip_id, group_id, name, user_id, now],
        )
    except Exception:
        # idx_trips_one_active_per_group caught a race: someone else's
        # /旅程 開始 committed between our check above and this insert.
        return ALREADY_ACTIVE_MSG, None
    return f"🧳 旅程「{name}」開始了！我會開始記錄接下來的討論。", trip_id


def is_admin(env, user_id: str) -> bool:
    # Plain field access, not the Workers-JS-proxy-defensive getattr() the Cloudflare version
    # used: env is a normal dataclass here (config.py), and admin_user_id defaults to "" when
    # ADMIN_USER_ID isn't set, so there's no AttributeError/undefined case to guard against.
    # The `user_id is not None` guard still matters: without it, an empty admin_user_id ("")
    # could only ever equal a user_id that's also "", which LINE never sends, but keeping the
    # explicit check documents the intent rather than relying on that coincidence.
    return user_id is not None and user_id == env.admin_user_id and env.admin_user_id != ""


async def end_trip(env, group_id: str, user_id: str) -> tuple[str, str | None]:
    """Returns (reply_text, trip_id). trip_id is only set when the trip actually
    ended, so callers can tell success from a rejection message without string-matching."""
    trip = await get_active_trip(env, group_id)
    if not trip:
        return WARN + "目前沒有進行中的旅程", None

    if trip["owner_line_user_id"] != user_id and not is_admin(env, user_id):
        return WARN + "只有開始這趟旅程的人才能結束它", None

    try:
        await organize_trip(env, trip["id"])  # fold in anything not yet organized before closing out
    except Exception as e:
        # best-effort: an organize hiccup (Claude/D1 issue, or the new heading-safety check in
        # organize.py rejecting a malformed LLM rewrite) should not block ending the trip -
        # whatever's unorganized just stays pending, same as if this line didn't run at all.
        log_error("end_trip organize_trip error", e)

    now = int(time.time())
    await env.db.query("UPDATE trips SET status = 'ended', ended_at = ? WHERE id = ?", [now, trip["id"]])
    return f"🏁 旅程「{trip['name']}」結束了！", trip["id"]
