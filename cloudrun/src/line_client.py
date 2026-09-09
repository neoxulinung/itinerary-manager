import base64
import hashlib
import hmac

import httpx

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_DATA_API = "https://api-data.line.me/v2/bot"
LINE_API = "https://api.line.me/v2/bot"

# httpx.AsyncClient() defaults to Timeout(5.0) - 5s for connect too. Same Cloudflare
# Worker-to-external-API TLS handshake latency that hit the Anthropic client (see
# llm_client.py / docs/plan.md item 11) applies here just as much; widen the same way.
_TIMEOUT = httpx.Timeout(30.0, connect=30.0)


def verify_signature(body: str, signature: str | None, channel_secret: str) -> bool:
    if not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def reply_messages(access_token: str, reply_token: str, messages: list[dict]) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        await client.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": messages},
        )


async def reply_text(access_token: str, reply_token: str, text: str) -> None:
    await reply_messages(access_token, reply_token, [{"type": "text", "text": text}])


async def get_display_name(access_token: str, user_id: str) -> str:
    # ponytail: no caching, one call per captured message; fine at this scale,
    # add a users-table cache if this ever shows up as a real cost/latency issue.
    #
    # Uses the 1:1 profile endpoint, not getGroupMemberProfile: the group-scoped one
    # 404'd in live testing even for a member who'd friended the bot (undocumented
    # LINE-side quirk), while the plain /profile/{userId} endpoint worked reliably for
    # anyone who has added the bot as a friend. Falls back to the raw user_id for anyone
    # who hasn't friended it — harmless, just less readable in the doc/replies.
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{LINE_API}/profile/{user_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return user_id
        return resp.json().get("displayName", user_id)


async def refresh_display_name(env, user_id: str, current_name: str | None) -> str:
    # get_display_name falls back to the raw userId when the person hasn't friended the bot
    # yet - that stale fallback used to get stored as if it were their real name and stay
    # wrong forever. Detect the stale case (stored name == the raw id) and retry live; fix
    # every row it's stored in so this only ever self-heals once. Shared (not just
    # expenses.py, which originally had this as a private helper) so organize.py's doc
    # timeline - the one universal surface every trip goes through - self-heals too. Also now
    # covers poll_votes, which the original expenses-only version never touched.
    name = current_name or user_id
    if name != user_id:
        return name
    fresh = await get_display_name(env.line_channel_access_token, user_id)
    if fresh == user_id:
        return user_id  # still not a friend, nothing to fix yet
    await env.db.query(
        "UPDATE messages SET user_display_name = ? WHERE line_user_id = ? AND user_display_name = ?",
        [fresh, user_id, user_id],
    )
    await env.db.query(
        "UPDATE expenses SET payer_display_name = ? WHERE payer_line_user_id = ? AND payer_display_name = ?",
        [fresh, user_id, user_id],
    )
    await env.db.query(
        "UPDATE expense_splits SET display_name = ? WHERE line_user_id = ? AND display_name = ?",
        [fresh, user_id, user_id],
    )
    await env.db.query(
        "UPDATE poll_votes SET display_name = ? WHERE line_user_id = ? AND display_name = ?",
        [fresh, user_id, user_id],
    )
    return fresh


async def get_message_content(access_token: str, message_id: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{LINE_DATA_API}/message/{message_id}/content",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, content_type
