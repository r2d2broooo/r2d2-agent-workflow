"""Small shared Telegram primitives; provider sessions and runners stay independent."""
import logging
import os
import re

from telegram.error import TelegramError

from . import chat_log, group_chat

log = logging.getLogger(__name__)


def env_int(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.error("invalid %s", name)
        return default


def configured_chat_ids(*extra: str) -> set[int]:
    names = ("TELEGRAM_PERSONAL_CHAT_ID", "TELEGRAM_GROUP_CHAT_ID", *extra)
    return {value for name in names if (value := env_int(name))}


def is_operator(actor, owner_id: int) -> bool:
    return bool(actor and not actor.is_bot and owner_id and actor.id == owner_id)


def _username_pattern(username: str) -> str:
    return rf"(?<![\w@])@{re.escape(username.lstrip('@'))}(?![\w-])"


def is_mentioned(text: str, username: str) -> bool:
    return bool(username and username.lstrip("@") and
                re.search(_username_pattern(username), text, flags=re.I))


def addressed(text: str, agent_id: str, username: str) -> bool:
    return agent_id in group_chat.mentioned_targets(text) or is_mentioned(text, username)


def strip_mentions(text: str, username: str, agent_id: str | None = None) -> str:
    if username and username.lstrip("@"):
        text = re.sub(_username_pattern(username), "", text, flags=re.I)
    return group_chat.strip_aliases(text, agent_id)


def log_group_message(message, text: str) -> int:
    """All receivers share one atomic row for an original Telegram chat/message pair."""
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    kwargs = {}
    if chat_id is not None and message_id is not None:
        kwargs["source_key"] = f"telegram:{chat_id}:{message_id}"
    return chat_log.log_message(group_chat.GROUP_CHAT_ID, group_chat.GROUP_CHAT_TITLE,
                                "dashboard", "user", text, **kwargs)


async def send_typing(bot, chat_id: int) -> None:
    """A best-effort indicator must never prevent the actual request from running."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
    except TelegramError as exc:
        log.warning("typing indicator unavailable: %s", type(exc).__name__)


async def handle_error(update, context) -> None:
    # Telegram exceptions can include token-bearing URLs; only record their type.
    log.error("Telegram handler error: %s", type(context.error).__name__)
