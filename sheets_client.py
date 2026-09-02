# -*- coding: utf-8 -*-
"""
Робота з Google Таблицею.

Логіка розпізнавання рядків:
У файлі є рядки-заголовки підрозділів (в них заповнена тільки колонка A,
або там текст типу "Подразделение") та рядок "№ в группе / Физическое лицо /
Дата рождения". Робочими вважаються ТІЛЬКИ ті рядки, де колонка "Дата
рождения" відповідає формату ДД.ММ.РРРР — все інше ігнорується автоматично,
тому структура з заголовками підрозділів НЕ заважає боту.

HR редагує ТІЛЬКИ основний аркуш, колонки A-C. Весь службовий стан бота
(кому відправлено, історія шаблонів тощо) живе на ОКРЕМОМУ, повністю
відокремленому аркуші (config.BOT_STATE_SHEET_TAB) — HR його ніколи не
бачить і не редагує, тому навіть повне очищення основного аркуша (A-Z)
фізично не може зачепити стан бота.
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

DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")


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


def get_spreadsheet():
    gc = _retry(_client)
    return _retry(gc.open_by_key, config.GOOGLE_SHEET_ID)


def get_worksheet():
    """Основний аркуш з даними працівників (той, який редагує HR)."""
    sh = get_spreadsheet()
    return _retry(sh.worksheet, config.GOOGLE_SHEET_TAB)


def get_state_worksheet():
    """Окремий СЛУЖБОВИЙ аркуш, де бот зберігає весь свій технічний стан
    (кому відправлено, кого бачили, історію шаблонів/вступів тощо). HR
    його НІКОЛИ не редагує — навіть повне очищення основного аркуша з
    працівниками (Крок "виділити A-Z і вставити наново") фізично не може
    зачепити цей окремий аркуш, бо це геть інша вкладка.

    Якщо аркуша ще не існує (перший запуск бота) — створюється сам,
    автоматично, з поясювальним заголовком. Жодних ручних дій від вас
    не потрібно."""
    sh = get_spreadsheet()
    last_err = None
    for attempt in range(1, 4):
        try:
            return sh.worksheet(config.BOT_STATE_SHEET_TAB)
        except gspread.exceptions.WorksheetNotFound:
            logger.info("Службового аркуша '%s' ще немає — створюю.", config.BOT_STATE_SHEET_TAB)
            new_ws = sh.add_worksheet(title=config.BOT_STATE_SHEET_TAB, rows=20, cols=10)
            _init_state_sheet(new_ws)
            return new_ws
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                "Помилка відкриття службового аркуша (спроба %s/3): %s", attempt, e
            )
            time.sleep(5 * attempt)
    raise last_err


def _init_state_sheet(ws):
    """Заповнює новостворений службовий аркуш зрозумілими заголовками
    (для людини, яка випадково туди зазирне) — дані самого стану
    зберігаються рядком нижче (config.STATE_ROW), щоб рядок 1 міг
    містити людські підписи."""
    headers = [
        "Дата очищення", "Дата ранкового зведення", "Дата 'усіх привітано'",
        "Кому відправлено сьогодні (JSON)", "Дата попередження про колізію",
        "Кого бачили сьогодні (JSON)", "Історія шаблонів за 7 днів (JSON)",
        "Історія вступів за 31 день (JSON)",
    ]
    _retry(ws.update, "A1:H1", [headers])
    _retry(
        ws.update, "A3",
        [["⚠️ Це службовий аркуш бота привітань з Днем народження. "
          "Будь ласка, НЕ редагуйте і не видаляйте його вміст — тут "
          "зберігається технічний стан бота (кому вже відправлено "
          "привітання, історія використаних шаблонів тощо). Дані "
          "працівників редагуються на основному аркуші."]]
    )


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
    """Повертає (greetable, duplicates, excluded_region):
    - greetable: список іменинників на сьогодні, яких бот МАЄ привітати
      [{"row": <номер рядка>, "name": str, "date_str": str, "section": str,
        "city": str, "city_type": str, "key": str, "sent": bool}, ...]
    - duplicates: колізії ключів серед greetable (див. нижче)
    - excluded_region: іменинники, яких бот СВІДОМО НЕ вітає, бо їхня
      локація визначена як "регіон" без конкретного магазину (наприклад
      адміністративний персонал "Львівський регіон") — таких людей
      вітають окремо безпосередньо керівники, тому автоматична розсилка
      про них узагалі "не знає" (не рахує, не логує, не надсилає).

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
    від позиції в таблиці станом, що зберігається на ОКРЕМОМУ службовому
    аркуші (див. get_sent_keys/add_sent_key нижче). HR бачить і редагує
    тільки основний аркуш, колонки A-C.

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
            # ВАЖЛИВО: ключ ідентичності будується на "місті" (last_known_city),
            # а НЕ на сирому тексті ланцюжка заголовків (current_section).
            # Причина: "місто" обчислюється стійким алгоритмом з "пам'яттю"
            # (не залежить від того, скільки проміжних рядків-заголовків
            # HR вставить чи прибере між магазином і працівником), тоді як
            # "section" — це буквальний текст усього ланцюжка, який ламається
            # від найменшої структурної зміни вище по файлу (навіть якщо
            # це не стосується самого працівника). Використання "section"
            # у ключі спричиняло реальний баг: одна й та сама людина під
            # час дрібного редагування HR-ом ставала "новою" з точки зору
            # бота і отримувала повторне привітання.
            #
            # ТАК САМО дату для ключа беремо НЕ сирою (raw_date, як записано
            # в таблиці — "1.1.2001" чи "01.01.2001", будь-як), а
            # НОРМАЛІЗОВАНОЮ через bdate (уже успішно розпарсений datetime
            # об'єкт вище) — завжди у форматі ДД.ММ.РРРР з нулями. Це той
            # самий клас проблеми, що й із "section": якщо HR перезапише
            # дату в іншому написанні (без нуля замість з нулем, або
            # навпаки) для ТІЄЇ САМОЇ людини — без нормалізації ключ
            # змінився б, і бот вважав би її новою.
            normalized_date = bdate.strftime("%d.%m.%Y")
            identity_location = last_known_city or current_section
            key = make_person_key(name, normalized_date, identity_location)
            result.append({
                "row": idx,
                "name": name,
                "date_str": normalized_date,
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
    #
    # ВАЖЛИВО: людей, чия локація визначена як "region" (тобто немає
    # конкретного магазину — лише загальний регіон, наприклад
    # "Львівський регіон", як у адміністративного персоналу) —
    # ПОВНІСТЮ виключаємо зі списку. Такий персонал вітають окремо,
    # безпосередньо керівники, тому автоматичний бот про них узагалі
    # "не знає" — не рахує їх у зведенні, не логує, не надсилає нічого.
    greetable = [p for p in result if p["city_type"] != "region"]
    excluded_region = [p for p in result if p["city_type"] == "region"]

    seen = {}
    duplicates = []
    for person in greetable:
        if person["key"] in seen:
            duplicates.append((seen[person["key"]], person))
        else:
            seen[person["key"]] = person
    if duplicates:
        logger.warning("Знайдено %s колізій ключів серед сьогоднішніх іменинників", len(duplicates))

    return greetable, duplicates, excluded_region



def make_person_key(name: str, date_str: str, location: str = "") -> str:
    """Стабільний ідентифікатор людини, який НЕ залежить від номера рядка.
    Складається з Ім'я + Дата + Місто (а НЕ "Підрозділ"/повний ланцюжок
    заголовків — той варіант виявився занадто крихким, див. пояснення в
    get_today_birthdays) — так двоє тезок з однаковою датою народження в
    РІЗНИХ містах не конфліктують між собою.
    Примітка: якщо є двоє АБСОЛЮТНО однакових ПІБ, з однаковою датою
    народження, В ОДНОМУ Й ТОМУ Ж місті — це малоймовірний, але
    можливий збіг (наприклад, різні відділи одного магазину), і якщо
    таке трапиться, бот про це попередить у логах (див. get_today_birthdays)."""
    return f"{name}|{date_str}|{location}"


# ---------------------------------------------------------------------------
# Службовий стан. З версії з окремим аркушем — весь цей стан живе НЕ на
# основному аркуші з працівниками, а на окремому службовому аркуші
# (config.BOT_STATE_SHEET_TAB), недосяжному для HR. Дані зберігаються в
# рядку config.STATE_ROW (рядок 1 того аркуша лишається під людські
# підписи-заголовки, див. _init_state_sheet).
# ---------------------------------------------------------------------------
def get_state_cell(ws, col: int) -> str:
    try:
        return _retry(ws.cell, config.STATE_ROW, col).value or ""
    except Exception:  # noqa: BLE001
        return ""


def set_state_cell(ws, col: int, value: str):
    _retry(ws.update_cell, config.STATE_ROW, col, value)


def get_sent_keys(ws) -> set:
    """Список тих, кому вже відправлено СЬОГОДНІ — за ключем
    Ім'я+Дата+Підрозділ, а не за номером рядка. Зберігається в службовій
    клітинці службового аркуша у форматі JSON (а не через розділові
    символи типу ';' —
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
# Спільна логіка "історії використаного" — застосовується і для шаблонів
# привітань (не повторювати за тиждень), і для вступних фраз (не
# повторювати за місяць). Зберігається як список записів
# [{"date": "YYYY-MM-DD", "idx": N}, ...] у вказаній службовій клітинці.
# Записи старші за задану кількість днів автоматично відсіюються при
# кожному читанні, тому клітинка сама "чиститься" і не росте нескінченно.
# ---------------------------------------------------------------------------
def _get_index_history(ws, state_cell: int, history_days: int) -> list:
    raw = get_state_cell(ws, state_cell)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    cutoff = datetime.now() - timedelta(days=history_days)
    fresh = []
    for entry in entries:
        try:
            entry_date = datetime.strptime(entry["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            continue
        if entry_date >= cutoff:
            fresh.append(entry)
    return fresh


def _add_index_history_entry(ws, state_cell: int, history_days: int, date_str: str, idx: int):
    entries = _get_index_history(ws, state_cell, history_days)  # вже відфільтровані свіжі
    entries.append({"date": date_str, "idx": idx})
    set_state_cell(ws, state_cell, json.dumps(entries, ensure_ascii=False))


# --- Шаблони привітань (templates.py) — не повторювати за 7 днів ---
def get_template_history(ws) -> list:
    return _get_index_history(ws, config.STATE_CELL_TEMPLATE_HISTORY, config.TEMPLATE_HISTORY_DAYS)


def add_template_history_entry(ws, date_str: str, template_idx: int):
    _add_index_history_entry(
        ws, config.STATE_CELL_TEMPLATE_HISTORY, config.TEMPLATE_HISTORY_DAYS,
        date_str, template_idx
    )


def get_recently_used_template_indices(ws) -> set:
    """Множина номерів шаблонів, використаних за останні
    TEMPLATE_HISTORY_DAYS днів (включно з сьогодні) — саме цю множину
    треба виключити при виборі наступного шаблону."""
    return {entry["idx"] for entry in get_template_history(ws)}


# --- Вступні фрази (тільки для першого привітання за день) — не
#     повторювати протягом 31 дня ---
def get_intro_history(ws) -> list:
    return _get_index_history(ws, config.STATE_CELL_INTRO_HISTORY, config.INTRO_HISTORY_DAYS)


def add_intro_history_entry(ws, date_str: str, intro_idx: int):
    _add_index_history_entry(
        ws, config.STATE_CELL_INTRO_HISTORY, config.INTRO_HISTORY_DAYS,
        date_str, intro_idx
    )

def _set_index_history(ws, state_cell: int, entries: list):
    """Прямий перезапис списку записів історії (використовується для
    "лікування" клітинки, якщо її стерли, а в пам'яті процесу є свіжіші
    дані — див. bot_logic.py)."""
    set_state_cell(ws, state_cell, json.dumps(entries, ensure_ascii=False))


def set_template_history(ws, entries: list):
    _set_index_history(ws, config.STATE_CELL_TEMPLATE_HISTORY, entries)


def set_intro_history(ws, entries: list):
    _set_index_history(ws, config.STATE_CELL_INTRO_HISTORY, entries)


def get_recently_used_intro_indices(ws) -> set:
    """Множина номерів вступних фраз, використаних за останні
    INTRO_HISTORY_DAYS днів (включно з сьогодні)."""
    return {entry["idx"] for entry in get_intro_history(ws)}

