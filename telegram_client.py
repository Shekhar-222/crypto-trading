"""Send Telegram messages. Run this file directly to find your chat id."""
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def is_enabled():
    return os.environ.get("TELEGRAM_ENABLED", "").lower() == "true"


def is_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _call(method, params=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode() if params else None
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)["result"]


def to_html(text, all_bold=False):
    """Escape the message for Telegram and bold each 'SELL CALL/PUT <strike>' and 'Stoploss: <price>'."""
    if all_bold:
        return f"<b>{html.escape(text, quote=False)}</b>"
    return re.sub(
        r"^(?:SELL (?:CALL|PUT) [\d,.]+|Stoploss: \S+)",
        r"<b>\g<0></b>",
        html.escape(text, quote=False),
        flags=re.M,
    )


def send(text, silent=False, all_bold=False):
    """Send a message and return (status, message_id). Errors never include the bot token."""
    if not is_configured():
        return "not configured", None
    try:
        result = _call("sendMessage", {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": to_html(text, all_bold),
            "parse_mode": "HTML",
            "disable_notification": "true" if silent else "false",
        })
        return "sent", result["message_id"]
    except urllib.error.HTTPError as error:
        return f"failed: Telegram returned HTTP {error.code}", None
    except (OSError, ValueError, KeyError) as error:
        return f"failed: {type(error).__name__}", None


def _attempt(method, params):
    """Call Telegram and return a short error reason, or None if it worked."""
    try:
        _call(method, {"chat_id": os.environ["TELEGRAM_CHAT_ID"], **params})
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (OSError, ValueError, KeyError) as error:
        return type(error).__name__
    return None


def pin(message_id):
    # The alert itself already notifies, so the pin stays silent.
    return _attempt("pinChatMessage", {"message_id": message_id, "disable_notification": "true"})


def unpin(message_id):
    return _attempt("unpinChatMessage", {"message_id": message_id})


if __name__ == "__main__":
    from app import load_env

    load_env()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Add TELEGRAM_BOT_TOKEN to .env first.")
    chats = {
        update[kind]["chat"]["id"]
        for update in _call("getUpdates")
        for kind in ("message", "channel_post")
        if kind in update
    }
    print(chats or "No messages found. Send your bot a message or post in its channel, then run this again.")
