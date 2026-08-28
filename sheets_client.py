# -*- coding: utf-8 -*-
"""
Робота з Google Таблицею.

Логіка розпізнавання рядків:
У файлі є рядки-заголовки підрозділів (в них заповнена тільки колонка A,
або там текст типу "Подразделение") та рядок "№ в группе / Физическое лицо /
Дата рождения". Робочими вважаються ТІЛЬКИ ті рядки, де колонка "Дата
рождения" відповідає формату ДД.ММ.РРРР — все інше ігнорується автоматично,
тому структура з заголовками підрозділів НЕ заважає боту.

HR редагує ТІЛЬКИ колонки A-C. Жодної видимої "галочки" чи позначки бот
у таблицю не додає — весь службовий стан прихований у клітинках рядка 1
(колонки D-H), і на них можна взагалі не звертати уваги.
"""
import re
import time
import json
import logging
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

import config

logger = logging.getLogger("birthday_bot.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _client():
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return gspread.authorize(creds)


def _retry(fn, *args, retries=3, base_delay=5, **kwargs):
    """Обгортка з повторними спробами на випадок тимчасового збою Google API."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Помилка звернення до Google Sheets (спроба %s/%s): %s",
                            attempt, retries, e)
            time.sleep(base_delay * attempt)
    raise last_err


def get_worksheet():
    gc = _retry(_client)
    sh = _retry(gc.open_by_key, config.GOOGLE_SHEET_ID)
    ws = _retry(sh.worksheet, config.GOOGLE_SHEET_TAB)
    return ws


def read_rows(ws):
    """Повертає список усіх рядків таблиці (список списків), як є."""
    return _retry(ws.get_all_values)


def parse_location_line(line: str) -> tuple:
    """Перевіряє, чи ЦЕЙ КОНКРЕТНИЙ рядок-заголовок є явним визначенням
    локації — магазином ("ТВ <Місто> <адреса>") чи регіоном (містить
    слово "регіон"). Повертає (назва, тип) або ("", "") якщо це просто
    назва відділу/ролі (наприклад "Обслуговуючий персонал", "Торговий
    персонал") — такі рядки НЕ є визначенням локації."""
    p = line.strip()
    if p.startswith("ТВ "):
        rest = p[3:].strip()
        tokens = rest.split()
        if tokens:
            city = tokens[0].rstrip(",.")
            city = city.replace("\u2019", "'")  # нормалізація апострофа
            return (city, "city")
    if "регіон" in p.lower():
        return (p, "region")
    return ("", "")


def get_today_birthdays(ws, sent_keys: set, today: datetime = None):
    """Повертає список іменинників на сьогодні:
    [{"row": <номер рядка>, "name": str, "date_str": str, "section": str,
      "city": str, "key": str, "sent": bool}, ...]
    Порівняння дати йде тільки по дню і місяцю (рік ігнорується).

    "today" — момент, який вважати "сьогодні". Якщо не передано —
    використовується системний час сервера (за замовчуванням для
    зворотної сумісності/тестів), але bot_logic.py ЗАВЖДИ передає явно
    поточний час за Києвом (Europe/Kyiv), а не системний час сервера —
    це важливо, бо Render зазвичай працює в UTC, і без явної передачі
    "новий день" міг би наставати на 2-3 години раніше чи пізніше
    реальної київської півночі.

    ВАЖЛИВО: "sent" визначається за ключем "Ім'я + Дата народження +
    Підрозділ" (key), яка звіряється зі списком sent_keys — незалежним
    від позиції в таблиці станом, що зберігається в службовій клітинці
    (див. get_sent_keys/add_sent_key нижче). У таблиці НЕМАЄ жодної
    видимої колонки-позначки — весь стан прихований у службових клітинках
    рядка 1, HR бачить і редагує тільки колонки A-C.

    Підрозділ береться з найближчого вище рядка-заголовку (рядки типу
    "Адміністрація", "Львівський регіон" тощо, де немає валідної дати) —
    це і є третя складова ключа, яка додатково захищає від колізій, якщо
    в компанії є двоє тезок з однаковою датою народження (реальний ризик
    при поширених іменах і високій плинності кадрів).

    "city" — окремо витягнуте місто/населений пункт, яке визначається
    ІНАКШЕ, ніж "section": замість того щоб дивитись тільки на поточний
    ланцюжок заголовків (який скидається на кожному новому блоці типу
    "Торговий персонал"), бот "тягне вперед" останнє явно визначене
    місто/регіон через УВЕСЬ файл — тобто дивиться не тільки в межах
    поточного блоку, а й вище, аж до попереднього магазину/регіону, якщо
    в поточному блоці своєї явної локації немає. Це потрібно, бо у
    вихідних даних (як їх формує ваша БД) деякі відділи (наприклад
    "Відділ технічної підтримки") виносяться окремим блоком БЕЗ
    повторення назви магазину, хоча фактично працюють у тому самому
    магазині, що й попередній блок."""
    rows = read_rows(ws)
    if today is None:
        today = datetime.now()
    result = []
    section_parts = []
    last_was_employee = True  # щоб перший блок заголовків теж накопичився
    # "Пам'ять" про останню явно визначену локацію — НЕ скидається між
    # блоками відділів, тільки коли зустрічається НОВЕ явне визначення
    # (новий магазин чи новий регіон).
    last_known_city = ""
    last_known_city_type = ""
    region_found_this_chain = False
    for idx, row in enumerate(rows, start=1):
        if idx == 1:
            continue  # заголовок таблиці
        name = row[config.COL_NAME - 1].strip() if len(row) >= config.COL_NAME else ""
        raw_date = row[config.COL_BIRTHDATE - 1].strip() if len(row) >= config.COL_BIRTHDATE else ""
        if not name or not DATE_RE.match(raw_date):
            # Рядок-заголовок (підрозділ / магазин / роль тощо). Заголовки
            # у вашій таблиці йдуть ВКЛАДЕНО одне за одним (наприклад
            # "ТВ Івано-Франківськ Довженко 59" -> "Обслуговуючий персонал"),
            # тому накопичуємо ВЕСЬ ланцюжок підряд рядків-заголовків, а не
            # тільки останній — інакше різні магазини з однаковою назвою
            # ролі (напр. "Обслуговуючий персонал" є майже в кожному
            # магазині) злилися б в один і той самий "section".
            col_a = row[config.COL_NUM - 1].strip() if len(row) >= config.COL_NUM else ""
            section_text = col_a or name
            if section_text:
                if last_was_employee:
                    section_parts = [section_text]  # новий ланцюжок з нуля
                    region_found_this_chain = False
                else:
                    section_parts.append(section_text)  # продовжуємо вкладеність
                # Перевіряємо, чи саме ЦЕЙ рядок є явним визначенням
                # локації — якщо так, оновлюємо "пам'ять" (не скидаємо
                # на звичайних рядках типу "Торговий персонал").
                #
                # Для магазину ("ТВ ...") — оновлюємо ЗАВЖДИ, останній
                # знайдений магазин має пріоритет (найточніша, найсвіжіша
                # локація).
                # Для регіону — оновлюємо ТІЛЬКИ якщо в поточному
                # ланцюжку заголовків це ПЕРШИЙ такий рядок. Це важливо,
                # бо в одному ланцюжку часто є одразу два рядки зі словом
                # "регіон" (наприклад "Львівський регіон" і нижче
                # "Адміністративна команда (Львівський регіон)") — і без
                # цього правила бот узяв би довший, менш чистий варіант.
                loc_name, loc_type = parse_location_line(section_text)
                if loc_type == "city":
                    last_known_city, last_known_city_type = loc_name, loc_type
                elif loc_type == "region" and not region_found_this_chain:
                    last_known_city, last_known_city_type = loc_name, loc_type
                    region_found_this_chain = True
            last_was_employee = False
            continue
        try:
            bdate = datetime.strptime(raw_date, "%d.%m.%Y")
        except ValueError:
            continue
        last_was_employee = True
        current_section = " / ".join(section_parts)
        if bdate.day == today.day and bdate.month == today.month:
            key = make_person_key(name, raw_date, current_section)
            result.append({
                "row": idx,
                "name": name,
                "date_str": raw_date,
                "section": current_section,
                "city": last_known_city,
                "city_type": last_known_city_type,
                "key": key,
                "sent": key in sent_keys,
            })

    # Захист: якщо навіть з підрозділом ключі колізують (двоє повних
    # тезок з однаковою датою народження в одному підрозділі) — це
    # позначаємо окремо, щоб бот не "проковтнув" когось мовчки, а
    # bot_logic зміг попередити про це в логах.
    seen = {}
    duplicates = []
    for person in result:
        if person["key"] in seen:
            duplicates.append((seen[person["key"]], person))
        else:
            seen[person["key"]] = person
    if duplicates:
        logger.warning("Знайдено %s колізій ключів серед сьогоднішніх іменинників", len(duplicates))

    return result, duplicates



def make_person_key(name: str, date_str: str, section: str = "") -> str:
    """Стабільний ідентифікатор людини, який НЕ залежить від номера рядка.
    Складається з Ім'я + Дата + Підрозділ — так двоє тезок з однаковою
    датою народження в РІЗНИХ підрозділах не конфліктують між собою.
    Примітка: якщо є двоє АБСОЛЮТНО однакових ПІБ, з однаковою датою
    народження, В ОДНОМУ Й ТОМУ Ж підрозділі — це вкрай малоймовірний
    потрійний збіг, але якщо таке трапиться, бот про це попередить у
    логах (див. get_today_birthdays)."""
    return f"{name}|{date_str}|{section}"


# ---------------------------------------------------------------------------
# Службовий стан (замінює локальний state.json, бо процес може будь-коли
# перезапуститись/переспати — жодної спільної пам'яті між циклами немає,
# тому стан зберігаємо прямо в таблиці, у службових клітинках D1/E1/F1/G1/H1.
# Це ЄДИНЕ місце в таблиці, куди пише бот — жодних видимих колонок чи
# позначок для HR немає.
# ---------------------------------------------------------------------------
def get_state_cell(ws, col: int) -> str:
    try:
        return _retry(ws.cell, 1, col).value or ""
    except Exception:  # noqa: BLE001
        return ""


def set_state_cell(ws, col: int, value: str):
    _retry(ws.update_cell, 1, col, value)


def get_sent_keys(ws) -> set:
    """Список тих, кому вже відправлено СЬОГОДНІ — за ключем
    Ім'я+Дата+Підрозділ, а не за номером рядка. Зберігається в службовій
    клітинці (G1) у форматі JSON (а не через розділові символи типу ';' —
    бо ім'я чи назва підрозділу теоретично можуть містити такий символ, і
    "наївне" розділення могло б розірвати ключ навпіл і зіпсувати стан)."""
    raw = get_state_cell(ws, config.STATE_CELL_SENT_KEYS)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        # Резервний варіант на випадок дуже старого формату (до фіксу) —
        # краще спробувати відновити хоч щось, ніж втратити стан повністю.
        logger.warning("Не вдалося розпарсити службову клітинку як JSON, використовую as-is")
        return {raw}


def add_sent_key(ws, key: str):
    keys = get_sent_keys(ws)
    keys.add(key)
    set_state_cell(ws, config.STATE_CELL_SENT_KEYS, json.dumps(sorted(keys), ensure_ascii=False))


def clear_sent_keys(ws):
    set_state_cell(ws, config.STATE_CELL_SENT_KEYS, "")


def get_seen_keys(ws) -> set:
    """Список УСІХ сьогоднішніх іменинників, яких бот уже бачив у таблиці
    (незалежно від того, привітані вони чи ще ні). Використовується лише
    для детекції "нових" людей, доданих HR-ом протягом дня — окремо від
    get_sent_keys, який відстежує саме "кому вже надіслано"."""
    raw = get_state_cell(ws, config.STATE_CELL_SEEN_KEYS)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return {raw}


def set_seen_keys(ws, keys: set):
    set_state_cell(ws, config.STATE_CELL_SEEN_KEYS, json.dumps(sorted(keys), ensure_ascii=False))


def clear_seen_keys(ws):
    set_state_cell(ws, config.STATE_CELL_SEEN_KEYS, "")


# ---------------------------------------------------------------------------
# Історія використаних шаблонів привітань (щоб жодні два привітання за
# останні 7 днів не повторювались — навіть для різних людей).
# Зберігається як список записів [{"date": "YYYY-MM-DD", "idx": N}, ...],
# де idx — номер шаблону в списку TEMPLATES (templates.py). Записи
# старші за TEMPLATE_HISTORY_DAYS автоматично відсіюються при кожному
# читанні, тому клітинка сама "чиститься" і не росте нескінченно.
# ---------------------------------------------------------------------------
def get_template_history(ws) -> list:
    raw = get_state_cell(ws, config.STATE_CELL_TEMPLATE_HISTORY)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    cutoff = datetime.now() - timedelta(days=config.TEMPLATE_HISTORY_DAYS)
    fresh = []
    for entry in entries:
        try:
            entry_date = datetime.strptime(entry["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            continue
        if entry_date >= cutoff:
            fresh.append(entry)
    return fresh


def add_template_history_entry(ws, date_str: str, template_idx: int):
    """Додає запис про використаний шаблон і одразу відсіює застарілі
    (старші за TEMPLATE_HISTORY_DAYS) — так клітинка сама не росте
    нескінченно."""
    entries = get_template_history(ws)  # вже відфільтровані свіжі
    entries.append({"date": date_str, "idx": template_idx})
    set_state_cell(
        ws, config.STATE_CELL_TEMPLATE_HISTORY,
        json.dumps(entries, ensure_ascii=False)
    )


def get_recently_used_template_indices(ws) -> set:
    """Множина номерів шаблонів, використаних за останні
    TEMPLATE_HISTORY_DAYS днів (включно з сьогодні) — саме цю множину
    треба виключити при виборі наступного шаблону."""
    return {entry["idx"] for entry in get_template_history(ws)}
