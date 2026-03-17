import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

def validate_telegram_data(init_data: str) -> dict | None:
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed:
            return None

        received_hash = parsed.pop("hash")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        secret = hmac.new(
            key=b"WebAppData",
            msg=BOT_TOKEN.encode(),
            digestmod=hashlib.sha256
        ).digest()

        calc = hmac.new(
            key=secret,
            msg=data_check.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()

        if calc != received_hash:
            return None

        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None
