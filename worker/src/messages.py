import uuid

from line_client import get_display_name, get_message_content
from trips import get_active_trip

CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


async def capture_message(env, group_id: str, event: dict) -> None:
    trip = await get_active_trip(env, group_id)
    if not trip:
        return  # no active trip: Bot stays passive, nothing is recorded

    message = event["message"]
    line_message_id = message["id"]
    msg_type = message["type"]

    existing = await env.DB.prepare(
        "SELECT id FROM messages WHERE line_message_id = ?"
    ).bind(line_message_id).all()

    if existing.results:
        # Webhook redelivery of a message we've already stored. Don't re-fetch the
        # display name or re-insert, but DO still fall through to the attachment
        # check below — a prior delivery may have stored the row but failed partway
        # through the photo download, and this is the only retry path for that.
        row_id = existing.results[0]["id"]
    else:
        text = message.get("text") if msg_type == "text" else None
        sent_at = event.get("timestamp", 0) // 1000
        user_id = event["source"]["userId"]
        display_name = await get_display_name(env.LINE_CHANNEL_ACCESS_TOKEN, user_id)

        row_id = str(uuid.uuid4())
        await env.DB.prepare(
            "INSERT INTO messages "
            "(id, trip_id, line_message_id, line_user_id, user_display_name, msg_type, text, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(row_id, trip["id"], line_message_id, user_id, display_name, msg_type, text, sent_at).run()

    if msg_type == "image":
        await _ensure_attachment(env, trip["id"], row_id, line_message_id)


async def _ensure_attachment(env, trip_id: str, message_row_id: str, line_message_id: str) -> None:
    existing = await env.DB.prepare(
        "SELECT id FROM attachments WHERE message_id = ?"
    ).bind(message_row_id).all()
    if existing.results:
        return  # already captured on a prior delivery

    content, content_type = await get_message_content(env.LINE_CHANNEL_ACCESS_TOKEN, line_message_id)
    ext = CONTENT_TYPE_EXT.get(content_type, "bin")
    r2_key = f"trips/{trip_id}/{message_row_id}.{ext}"

    await env.PHOTOS.put(r2_key, content, {"httpMetadata": {"contentType": content_type}})

    attachment_id = str(uuid.uuid4())
    await env.DB.prepare(
        "INSERT INTO attachments (id, message_id, r2_key, content_type) VALUES (?, ?, ?, ?)"
    ).bind(attachment_id, message_row_id, r2_key, content_type).run()
