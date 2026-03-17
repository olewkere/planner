import hashlib
import hmac
from urllib.parse import parse_qsl
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

def validate_telegram_data(init_data: str) -> dict | None:
    """Повертає дані юзера або None якщо невалідно."""
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            return None
        
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=BOT_TOKEN.encode(),
            digestmod=hashlib.sha256
        ).digest()
        
        calculated_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        if calculated_hash != received_hash:
            return None
        
        import json
        user_data = json.loads(parsed_data.get("user", "{}"))
        return user_data
        
    except Exception:
        return None