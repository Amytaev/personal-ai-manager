"""Telegram bot package - implemented in Phase 3 (TZ v4 §23-26).

Not populated in Phase 2 (Core Infrastructure). Config already exposes
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (see config.py) so Phase 3 can
wire the bot straight in without touching this phase's code, and the
single-user `chat_id` check (TZ v4 §24) will live in telegram/auth.py.
"""
