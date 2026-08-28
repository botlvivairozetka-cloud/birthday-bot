# -*- coding: utf-8 -*-
"""
Веб-сервіс для Render (безкоштовний Web Service).

Чому взагалі веб-сервер, якщо бот нічого "не показує" людям? Тому що
безкоштовний тариф Render визнає сервіс "живим" тільки якщо він слухає
HTTP-порт. Тому тут є один маршрут "/", який:
  - дозволяє Render вважати сервіс запущеним;
  - його ж пінгує UptimeRobot кожні ~5 хв, щоб Render не "заснув" сервіс
    через 15 хв бездіяльності (стандартна поведінка безкоштовного тарифу).

Уся реальна робота бота відбувається у фоновому потоці (background thread),
який запускається один раз при старті процесу і працює у нескінченному
циклі: перевіряє Google Таблицю, і якщо є непривітаний іменинник —
надсилає ОДНЕ привітання, тоді "спить" 5-10 хвилин (випадково) і повторює.
Так природньо витримується пауза між привітаннями різним людям.
"""
import logging
import random
import threading
import time
import os

from flask import Flask

import config
from bot_logic import run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("birthday_bot.app")

app = Flask(__name__)

_last_status = "not started yet"
_lock = threading.Lock()


@app.route("/")
def health():
    """Сторінка для Render health-check і для пінгів UptimeRobot."""
    with _lock:
        status = _last_status
    return {"status": "alive", "last_cycle_result": status}, 200


def worker_loop():
    """Фоновий нескінченний цикл. Один прохід = один cycle run_once()."""
    global _last_status
    logger.info("Фоновий цикл бота запущено.")
    while True:
        try:
            result = run_once()
            with _lock:
                _last_status = result
            logger.info("Результат циклу: %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("Непередбачена помилка у фоновому циклі")
            result = "error: unexpected exception, see logs"
            with _lock:
                _last_status = result

        # Якщо цим циклом реально надіслано привітання — витримуємо паузу
        # 5-10 хв (як і треба між людьми). Якщо нічого не надсилали
        # (немає іменинників / усіх уже привітано / була помилка) —
        # достатньо коротшої паузи ~2 хв, щоб швидше "підхопити" зміни.
        if isinstance(result, str) and result.startswith("ok: sent"):
            delay = random.randint(300, 600)  # 5-10 хв
        else:
            delay = 120  # 2 хв
        time.sleep(delay)


# Фоновий потік стартує один раз при імпорті модуля (тобто при старті
# gunicorn/flask), а не при кожному HTTP-запиті.
_thread = threading.Thread(target=worker_loop, daemon=True)
_thread.start()


if __name__ == "__main__":
    # Локальний запуск для тестування: python app.py
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
