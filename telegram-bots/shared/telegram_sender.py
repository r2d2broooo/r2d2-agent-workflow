"""Send operator notices (messages/photos) to the Telegram group "Matrix" via the Claude bot.

Every automated notice — cloud-bot BUY/SELL, data gates, healer, position watch — lands in the
one group chat the operator reads. The ``chat_target`` argument is kept for legacy callers:
"group" and "personal" both resolve to the group; "dm" reaches the operator's private chat.
"""

import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

_TOKEN = os.environ.get("CLAUDE_BOT_TOKEN", "")
_PERSONAL_CHAT_ID = int(os.environ.get("TELEGRAM_PERSONAL_CHAT_ID", "0"))
_GROUP_CHAT_ID = int(os.environ.get("TELEGRAM_GROUP_CHAT_ID", "0"))
# Telegram caps a message at 4096 and a photo caption at 1024 UTF-16 code units (an emoji is 2).
# Nothing is ever cut to fit: longer text goes out as several messages (operator 2026-10-07).
_MESSAGE_CHUNK = 4000
_CAPTION_LIMIT = 1024


def resolve_chat_id(target: str) -> int:
    """Return the real Telegram chat_id for 'group'/'personal' (→ group) or 'dm'."""
    if target in ("group", "personal"):
        chat_id = _GROUP_CHAT_ID
    elif target == "dm":
        chat_id = _PERSONAL_CHAT_ID
    else:
        raise ValueError(f"Unknown chat target: {target!r} (use 'group', 'personal' or 'dm')")
    if not chat_id or not _TOKEN:
        raise RuntimeError("Telegram is not configured: need CLAUDE_BOT_TOKEN and the chat id in .env")
    return chat_id


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = _MESSAGE_CHUNK) -> list[str]:
    """Split text into Telegram-sized parts without dropping any of it.

    Each part breaks at the last paragraph, line or word boundary that fits, so a long reply
    reads as consecutive messages instead of one that stops mid-word."""
    rest = str(text or "")
    parts = []
    while _units(rest) > limit:
        cut = limit
        while _units(rest[:cut]) > limit:
            cut -= _units(rest[:cut]) - limit
        for sep in ("\n\n", "\n", " "):
            i = rest.rfind(sep, 0, cut)
            if i > cut // 2:
                cut = i + len(sep)
                break
        part = rest[:cut].rstrip()
        if part:
            parts.append(part)
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        parts.append(rest)
    return parts


async def send_photo(chat_target: str, image_path: str, caption: str | None = None) -> str:
    """Send an image file to a Telegram chat. Returns a status string."""
    from telegram import Bot

    chat_id = resolve_chat_id(chat_target)
    bot = Bot(token=_TOKEN)
    caption = str(caption or "")
    fits = _units(caption) <= _CAPTION_LIMIT
    with open(image_path, "rb") as f:
        msg = await bot.send_photo(chat_id=chat_id, photo=f, caption=(caption if fits else None) or None)
    if caption and not fits:
        # Too long for a caption: the full text follows the photo instead of being cut.
        for part in split_message(caption):
            await bot.send_message(chat_id=chat_id, text=part)
    return f"sent photo to {chat_target} (message_id={msg.message_id})"


async def send_message(chat_target: str, text: str) -> str:
    """Send a text message to a Telegram chat in as many parts as it needs. Returns a status string."""
    from telegram import Bot

    chat_id = resolve_chat_id(chat_target)
    parts = split_message(text)
    if not parts:
        return "no message to send"
    bot = Bot(token=_TOKEN)
    for part in parts:
        msg = await bot.send_message(chat_id=chat_id, text=part)
    return f"sent {len(parts)} message(s) to {chat_target} (message_id={msg.message_id})"


async def reply_full(message, text: str) -> None:
    """Reply to a chat message with ALL of `text`, in as many Telegram messages as it takes."""
    for part in split_message(text):
        await message.reply_text(part)
