"""ponytail: throwaway local test script for Phase 1, not part of the deployed app."""
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.request

secret = None
with open(".dev.vars") as f:
    for line in f:
        if line.startswith("LINE_CHANNEL_SECRET="):
            secret = line.strip().split("=", 1)[1]

text = sys.argv[1] if len(sys.argv) > 1 else "/旅程 開始 測試旅程"
group_id = sys.argv[2] if len(sys.argv) > 2 else "Cxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
user_id = sys.argv[3] if len(sys.argv) > 3 else "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

body = {
    "destination": "xxx",
    "events": [
        {
            "type": "message",
            "message": {"type": "text", "id": str(time.time_ns()), "text": text},
            "timestamp": int(time.time() * 1000),
            "source": {"type": "group", "groupId": group_id, "userId": user_id},
            "replyToken": "fake-reply-token",
            "mode": "active",
        }
    ],
}
raw = json.dumps(body).encode("utf-8")
sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()

req = urllib.request.Request(
    "http://localhost:8787/webhook",
    data=raw,
    headers={"Content-Type": "application/json", "X-Line-Signature": sig},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(resp.status, resp.read().decode())
