# -*- coding: utf-8 -*-
"""Мінімальний клієнт Telegram Bot API (тільки надсилання повідомлень)."""
import time
import logging
import requests

import config

logger = logging.getLogger("birthday_bot.telegram")

API_URL = f"https://api.telegram.org/bot{{token}}/sendMessage"


def send_message(chat_id: int, topic_id: int, text: str, retries: int = 3) -> bool:
    url = API_URL.format(token=config.BOT_TOKEN)
    payload = {
        "chat_id": chat_id,
        "message_thread_id": topic_id,
        "text": text,
        "parse_mode": "HTML",
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                return True
            last_err = data
            logger.warning("Telegram API помилка (спроба %s/%s): %s", attempt, retries, data)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Помилка з'єднання з Telegram (спроба %s/%s): %s", attempt, retries, e)
        time.sleep(5 * attempt)
    logger.error("Не вдалося надіслати повідомлення після %s спроб: %s", retries, last_err)
    return False


def log(text: str):
    """Надсилає повідомлення в системну гілку логів."""
    send_message(config.LOG_CHAT_ID, config.LOG_TOPIC_ID, text)
