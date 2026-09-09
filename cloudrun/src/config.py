import os
from dataclasses import dataclass

from dotenv import load_dotenv

from d1 import D1
from r2 import R2

load_dotenv()  # no-op in production (Cloud Run injects env vars directly, no .env file ships)


@dataclass
class Env:
    db: D1
    r2: R2
    line_channel_secret: str
    line_channel_access_token: str
    anthropic_api_key: str
    openai_api_key: str
    admin_user_id: str
    liff_id: str
    scheduler_secret: str


_env: Env | None = None


def get_env() -> Env:
    global _env
    if _env is None:
        _env = Env(
            db=D1(
                account_id=os.environ["CF_ACCOUNT_ID"],
                database_id=os.environ["CF_D1_DATABASE_ID"],
                api_token=os.environ["CF_API_TOKEN"],
            ),
            # Optional like ADMIN_USER_ID/LIFF_ID/SCHEDULER_SECRET below, not required like
            # CF_ACCOUNT_ID above: boto3.client() doesn't validate credentials at construction
            # time, so leaving these unset until the new R2 API token exists doesn't break
            # anything except the image-capture path itself (caught by _handle_event's
            # try/except) - it must not take down text-only capture, commands, or the LIFF API,
            # which all call get_env() too.
            r2=R2(
                account_id=os.environ["CF_ACCOUNT_ID"],
                access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
                secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
                bucket=os.environ.get("R2_BUCKET_NAME", ""),
                public_base_url=os.environ.get("R2_PUBLIC_BASE_URL", ""),
            ),
            line_channel_secret=os.environ["LINE_CHANNEL_SECRET"],
            line_channel_access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"],
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            # Optional: a LINE user ID allowed to end/force-end any trip regardless of who
            # started it. Plain env lookup, not the Workers-JS-proxy-defensive getattr() the
            # Cloudflare version used (trips.py's is_admin) - a normal dataclass field on a
            # normal CPython object needs no such defensiveness.
            admin_user_id=os.environ.get("ADMIN_USER_ID", ""),
            liff_id=os.environ.get("LIFF_ID", ""),
            scheduler_secret=os.environ.get("SCHEDULER_SECRET", ""),
        )
    return _env
