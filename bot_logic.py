# -*- coding: utf-8 -*-
"""
Спільна логіка бота — один "цикл перевірки". Викликається знову і знову
з app.py (кожні 5-10 хв, у фоновому потоці), а стан між циклами
зберігається не в пам'яті процесу (Render може будь-коли перезапустити
сервіс), а прямо в Google Таблиці — у службових клітинках D1/E1/F1/G1/H1.

У таблиці НЕМАЄ жодної видимої "галочки SEND" — HR редагує тільки
колонки A-C, а весь службовий стан прихований у клітинках рядка 1.

ВАЖЛИВО про стабільність при переоновленні таблиці HR-ом:
Хто вже привітаний СЬОГОДНІ визначається за ключем "Ім'я + Дата
народження + Підрозділ" (службова клітинка), а НЕ за номером рядка. Це
навмисно з двох причин:
  1) якщо HR видаляє всю таблицю і вставляє дані наново, порядок рядків
     може змінитись — прив'язка до номера рядка могла б помилково
     "переплутати" одну людину з іншою і пропустити привітання;
  2) саме по собі Ім'я+Дата теоретично може збігтись у двох різних людей
     (поширені імена + плинність кадрів) — додавання підрозділу різко
     знижує ймовірність такого збігу. Якщо колізія все ж станеться, бот
     один раз на день попередить про це в логах (ЛОГ ⚠️).

Логіка одного циклу:
  1. Новий день? -> чистимо список "кому відправлено" і список "кого вже бачили".
  2. Хто сьогодні іменинник (за поточним вмістом таблиці)?
  3. Перший цикл за день? -> лог-зведення "Сьогодні N іменинників".
  3б. Якщо зведення вже було раніше і в таблиці з'явився НОВИЙ іменинник
      (якого HR додав протягом дня) -> окремий лог про це.
  4. Є непривітаний (за ключем Ім'я+Дата+Підрозділ)? -> шлемо РІВНО ОДНЕ
     привітання (перше за день — звичайний шаблон, кожне наступне — з
     фразою-переходом типу "Також сьогодні День народження святкує...").
  5. Усіх привітано? -> один раз лог "усіх привітано".
"""
import logging
import random
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import sheets_client
import telegram_client
from templates import TEMPLATES

logger = logging.getLogger("birthday_bot")

# Пам'ять процесу (НЕ Google Таблиці). Це додатковий запобіжник на
# випадок, якщо хтось випадково зачепить службові клітинки D1:K1 у
# таблиці (наприклад, HR виділив ВЕСЬ аркуш, від A до Z, і вставив дані
# наново, не лишивши цих колонок недоторканими). Без цього запобіжника
# бот міг би:
#   а) вирішити, що почався новий день, і почати вітати вже привітаних
#      людей повторно;
#   б) "забути", кому вже відправлено сьогодні, і продублювати;
#   в) "забути" історію використаних шаблонів/вступних фраз за останні
#      7/31 днів — і випадково повторити те, що вже надсилалось.
# Тому бот дублює ВСІ ці дані і в пам'яті процесу, і використовує це,
# щоб "вилікувати" таблицю, якщо там щось раптом зникло. Оскільки процес
# на Render живе годинами/днями без перезапуску — це надійний додатковий
# захист (хоч і не стовідсотковий: якщо збіг стирання з перезапуском
# процесу станеться одночасно, захист не спрацює — саме тому й існує
# базовий захист "genuinely_new_day" нижче, який працює навіть тоді).
_last_seen_day = None
_sent_keys_memory = set()
_seen_keys_memory = set()
_template_history_memory = set()  # множина (date_str, idx)
_intro_history_memory = set()     # множина (date_str, idx)


def _prune_history_pairs(pairs: set, history_days: int, today_naive: datetime) -> set:
    """Прибирає з множини (date_str, idx) записи, старші за history_days
    відносно today_naive. Використовується і для пам'яті процесу, і для
    об'єднання з таблицею — щоб застарілі записи не "воскресали" назавжди
    через пам'ять."""
    cutoff = today_naive - timedelta(days=history_days)
    pruned = set()
    for date_str, idx in pairs:
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if entry_date >= cutoff:
            pruned.add((date_str, idx))
    return pruned


def get_first_name(full_name: str) -> str:
    """ПІБ у форматі 'Прізвище Ім'я По-батькові' -> повертає Ім'я."""
    parts = full_name.split()
    if len(parts) >= 2:
        return parts[1]
    return full_name


def get_surname_first_name(full_name: str) -> str:
    """ПІБ у форматі 'Прізвище Ім'я По-батькові' -> повертає
    'Прізвище Ім'я' (для фрази-переходу — верхньої частини повідомлення,
    коли за день кілька іменинників)."""
    parts = full_name.split()
    if len(parts) >= 2:
        surname, first_name = parts[0], parts[1]
        return f"{surname} {first_name}"
    return full_name


def get_name_with_city(full_name: str, city: str = "", city_type: str = "") -> str:
    """Ім'я (без прізвища — воно вже було у фразі-переході вище, якщо
    вона є) + місто, вказане прямо в тексті (без дужок):
    - якщо це справжнє місто/магазин (city_type="city") -> "з м. Львів";
    - якщо це регіон/адмінструктура без конкретного міста
      (city_type="region", напр. "Львівський регіон") -> через тире,
      без "м." (граматично неправильно казати "м. Львівський регіон") ->
      "— Львівський регіон";
    - якщо міста не визначено взагалі -> просто ім'я, без жодної згадки
      міста."""
    first_name = get_first_name(full_name)
    if not city:
        return first_name
    if city_type == "city":
        return f"{first_name} з м. {city}"
    if city_type == "region":
        return f"{first_name} — {city}"
    return first_name


# Фрази-переходи для 2-го, 3-го, 4-го, 5-го, 6-го привітання за один день
# (якщо іменинників кілька). Перше привітання за день йде БЕЗ переходу —
# просто звичайний шаблон з templates.py. Кожен наступний починається з
# такої фрази-місточка, а тоді вже йде сам текст привітання.
#
# ВАЖЛИВО: усі фрази побудовані так, щоб {name} завжди був ПІДМЕТОМ
# речення в називному відмінку ("хто?" святкує/відзначає), а НЕ прямим
# звертанням через кому ("з Днем народження, {name}!"). Це навмисно —
# такі конструкції залишаються граматично коректними для БУДЬ-ЯКОГО
# імені й прізвища без необхідності їх відмінювати (а правильне
# автоматичне відмінювання довільних українських імен — дуже складна
# задача). Якщо додаєте власні фрази-переходи — тримайтесь того самого
# принципу: {name} має бути тим, ХТО щось робить (підмет), а не тим, ДО
# КОГО звертаються напряму.
CONNECTOR_PHRASES = {
    2: "Також сьогодні День народження святкує {name}!",
    3: "А ще сьогодні свій День народження відзначає {name}!",
    4: "Сьогодні День народження святкує також {name}!",
    5: "І {name} сьогодні відзначає своє свято!",
    6: "До сьогоднішніх привітань долучається {name}!",
}
# Резервна фраза, якщо іменинників за день виявиться більше шести —
# щоб бот не "закінчився" на шостому і продовжував коректно вітати всіх.
CONNECTOR_PHRASE_FALLBACK = "Ще одне привітання сьогодні — День народження святкує {name}!"

# Вступні фрази — ТІЛЬКИ для ПЕРШОГО привітання за день (overall_index=1).
# Одна з них обирається так, щоб не повторюватись протягом 31 дня (див.
# choose_intro_index / INTRO_HISTORY_DAYS нижче) — щоб розсилка виглядала
# живіше і не набридала однаковими фразами навіть за цілий місяць. Той
# самий принцип граматичної безпеки, що й для CONNECTOR_PHRASES: {name}
# завжди підмет речення ("хто?" святкує), а не пряме звернення — працює
# коректно для будь-якого імені й прізвища без відмінювання.
INTRO_PHRASES = [
    "Друзі, привіт! А ви знали, що сьогодні свій День народження святкує {name}?",
    "Увага, увага! Сьогодні особливий день — День народження святкує {name}!",
    "Доброго дня, команда! Поспішаємо повідомити: сьогодні святкує {name}!",
    "Раді повідомити: сьогодні День народження відзначає {name}!",
    "Несемо гарну новину: сьогодні День народження святкує {name}!",
    "Увага, колеги! Сьогодні у нашій команді свято — День народження святкує {name}!",
    "Народ, зверніть увагу! Сьогодні День народження святкує {name}!",
    "Ось і чудова новина на сьогодні: День народження відзначає {name}!",
    "Тримайте цікаву інформацію: сьогодні святкує {name}!",
    "Ловіть чудову новину дня: святкує {name}!",
    "Команда, увага! Сьогодні особлива подія — святкує {name}!",
    "От і привід для радості: сьогодні відзначає своє свято {name}!",
    "Цікавинка на сьогодні: День народження святкує {name}!",
    "Спішимо поділитися: сьогодні День народження відзначає {name}!",
    "Хороші новини не змушують чекати: сьогодні святкує {name}!",
    "От і чудовий привід зібратися разом: сьогодні відзначає День народження {name}!",
    "Головна подія дня: День народження святкує {name}!",
    "Раді анонсувати: сьогодні своє свято відзначає {name}!",
    "Барабанний дріб! Сьогодні День народження святкує {name}!",
    "Не проґавте: сьогодні відзначає День народження {name}!",
    "Сьогодні точно є привід для свята: святкує {name}!",
    "Тепла новина на ранок: сьогодні святкує {name}!",
    "Наша команда сьогодні на позитиві — святкує {name}!",
    "Оголошуємо: сьогодні День народження святкує {name}!",
    "Це чудовий привід посміхнутися: сьогодні святкує {name}!",
    "Важлива новина ранку: сьогодні День народження відзначає {name}!",
    "З самого ранку хороші новини: сьогодні святкує {name}!",
    "Даруємо усмішку дня: сьогодні відзначає свято {name}!",
    "Це особливий день для команди: святкує {name}!",
    "Ще одна причина посміхнутися сьогодні: День народження святкує {name}!",
    "Оголошення дня: сьогодні своє свято відзначає {name}!",
]


def choose_template_index(excluded_indices: set) -> tuple:
    """Обирає випадковий шаблон, якого НЕ було серед excluded_indices
    (шаблони, використані за останні 7 днів). Повертає (індекс, чи_довелось_повторити).

    Якщо ВСІ шаблони виявились виключеними (малоймовірно при достатній
    кількості шаблонів, але можливо в дуже "завантажений" тиждень) —
    доводиться повторити якийсь шаблон, і про це повертається прапорець,
    щоб bot_logic міг попередити в логах."""
    available = [i for i in range(len(TEMPLATES)) if i not in excluded_indices]
    if available:
        return random.choice(available), False
    # Резерв: пул шаблонів вичерпано за останній тиждень. Це сигнал, що
    # варто додати більше шаблонів у templates.py (див. README).
    return random.randrange(len(TEMPLATES)), True


def choose_intro_index(excluded_indices: set) -> tuple:
    """Те саме, що choose_template_index, але для INTRO_PHRASES — обирає
    вступну фразу, якої НЕ було серед excluded_indices (використані за
    останні 31 день). Повертає (індекс, чи_довелось_повторити)."""
    available = [i for i in range(len(INTRO_PHRASES)) if i not in excluded_indices]
    if available:
        return random.choice(available), False
    return random.randrange(len(INTRO_PHRASES)), True


def build_greeting(
    full_name: str,
    city: str = "",
    city_type: str = "",
    overall_index: int = 1,
    excluded_template_indices: set = frozenset(),
    excluded_intro_indices: set = frozenset(),
) -> dict:
    """overall_index — це порядковий номер цього привітання серед усіх
    сьогоднішніх (1 = перше за день, 2 = друге, і т.д.).
    - Перше привітання (overall_index=1) отримує вступну фразу
      (INTRO_PHRASES), яка НЕ повторюється протягом 31 дня — "Прізвище
      Ім'я" підметом, у стилі "А ви знали, що сьогодні святкує...".
    - Кожне наступне (2, 3, ...) отримує фразу-перехід (CONNECTOR_PHRASES).

    excluded_template_indices — шаблони, використані за останні 7 днів.
    excluded_intro_indices — вступні фрази, використані за останні 31 день.
    Обидва списки виключаються з вибору, щоб нічого не повторювалось.

    Основний текст привітання отримує тільки ім'я (+ місто, якщо відоме),
    а верхня частина (вступ чи перехід) — "Прізвище Ім'я" без міста, щоб
    не перевантажувати повідомлення повторенням.

    Повертає словник:
      {"text": ..., "template_idx": ..., "template_repeated": bool,
       "intro_idx": int | None, "intro_repeated": bool}
    ("intro_idx" є тільки для overall_index<=1, інакше None)."""
    template_idx, template_repeated = choose_template_index(excluded_template_indices)
    template = TEMPLATES[template_idx]
    greeting_text = template.format(name=get_name_with_city(full_name, city, city_type))

    intro_idx = None
    intro_repeated = False

    if overall_index <= 1:
        intro_idx, intro_repeated = choose_intro_index(excluded_intro_indices)
        intro_template = INTRO_PHRASES[intro_idx]
        intro_text = intro_template.format(name=get_surname_first_name(full_name))
        greeting_text = f"{intro_text}\n\n{greeting_text}"
    else:
        connector_template = CONNECTOR_PHRASES.get(overall_index, CONNECTOR_PHRASE_FALLBACK)
        connector_text = connector_template.format(name=get_surname_first_name(full_name))
        greeting_text = f"{connector_text}\n\n{greeting_text}"

    return {
        "text": greeting_text,
        "template_idx": template_idx,
        "template_repeated": template_repeated,
        "intro_idx": intro_idx,
        "intro_repeated": intro_repeated,
    }



def now_kyiv() -> datetime:
    """Поточний час, ЯВНО у часовому поясі Europe/Kyiv (config.TIMEZONE) —
    а не системний час сервера. Це важливо, бо Render зазвичай працює в
    UTC: без явного переведення в київський час "новий день" міг би
    наставати на 2-3 години раніше/пізніше реальної київської півночі,
    а вікно відправки (10:00-20:00) рахувалось би за неправильною
    годиною."""
    return datetime.now(ZoneInfo(config.TIMEZONE))


def is_within_sending_window(now: datetime) -> bool:
    """Чи можна прямо зараз надсилати привітання (10:00-20:00 за Києвом,
    налаштовується через SEND_WINDOW_START_HOUR / SEND_WINDOW_END_HOUR).
    Поза цим вікном бот усе одно перевіряє таблицю, пише ранкове
    зведення і т.д. — просто НЕ надсилає сам текст привітання, аж поки
    не настане 10:00."""
    return config.SEND_WINDOW_START_HOUR <= now.hour < config.SEND_WINDOW_END_HOUR


def today_str(now: datetime = None) -> str:
    if now is None:
        now = now_kyiv()
    return now.strftime("%Y-%m-%d")


def run_once():
    """Виконує один цикл перевірки. Повертає короткий текстовий статус
    (використовується у /health для діагностики)."""
    global _last_seen_day, _sent_keys_memory, _seen_keys_memory
    global _template_history_memory, _intro_history_memory
    kyiv_now = now_kyiv()
    day = today_str(kyiv_now)
    logger.info("=== Цикл перевірки за %s (%s Києва) ===", day, kyiv_now.strftime("%H:%M"))

    try:
        ws = sheets_client.get_worksheet()
        state_ws = sheets_client.get_state_worksheet()
    except Exception as e:  # noqa: BLE001
        logger.exception("Критична помилка звернення до Google Sheets")
        telegram_client.log(
            f"⚠️ ПОМИЛКА: не вдалося відкрити Google Таблицю ({day}).\n"
            f"Причина: {e}\nНаступна спроба — приблизно через кілька хвилин."
        )
        return "error: google sheets unavailable"

    # 1. Визначаємо, чи справді почався новий день. Довіряємо службовій
    #    клітинці (на окремому службовому аркуші) ТІЛЬКИ якщо це
    #    узгоджується з власною пам'яттю процесу — інакше вважаємо, що
    #    клітинку просто випадково стерли, а не що прийшов новий день, і
    #    НЕ чистимо стан (захист від дублювання).
    last_clear = sheets_client.get_state_cell(state_ws, config.STATE_CELL_CLEAR_DATE)
    genuinely_new_day = (last_clear != day) and (_last_seen_day is None or _last_seen_day != day)

    if genuinely_new_day:
        sheets_client.clear_sent_keys(state_ws)
        sheets_client.clear_seen_keys(state_ws)
        sheets_client.set_state_cell(state_ws, config.STATE_CELL_CLEAR_DATE, day)
        _sent_keys_memory = set()
        _seen_keys_memory = set()
    elif last_clear != day:
        # Службова клітинка не відповідає сьогоднішній даті, але пам'ять
        # процесу каже, що сьогодні вже було — просто "лікуємо" клітинку,
        # не чистимо стан.
        logger.warning(
            "Службова клітинка дати не відповідає сьогоднішній, але процес "
            "уже бачив сьогодні '%s' раніше — НЕ очищую стан, лише "
            "відновлюю клітинку.", day
        )
        sheets_client.set_state_cell(state_ws, config.STATE_CELL_CLEAR_DATE, day)

    _last_seen_day = day

    # 2. Хто сьогодні іменинник — "sent" визначається за ключем
    #    Ім'я+Дата+Підрозділ, а НЕ за номером рядка (стійко до
    #    перевставлення таблиці HR-ом). Список "кому відправлено"
    #    беремо як ОБ'ЄДНАННЯ таблиці і пам'яті процесу — якщо таблиця
    #    раптом "забула" когось (клітинку стерли), пам'ять процесу це
    #    компенсує, і ми "лікуємо" клітинку назад.
    try:
        sheet_sent_keys = sheets_client.get_sent_keys(state_ws)
        effective_sent_keys = sheet_sent_keys | _sent_keys_memory
        if effective_sent_keys != sheet_sent_keys:
            logger.warning(
                "Службова клітинка не містить %s ключ(ів), які пам'ятає "
                "процес — відновлюю клітинку.",
                len(effective_sent_keys - sheet_sent_keys)
            )
            sheets_client.set_state_cell(
                state_ws, config.STATE_CELL_SENT_KEYS,
                json.dumps(sorted(effective_sent_keys), ensure_ascii=False)
            )
        _sent_keys_memory = effective_sent_keys
        birthdays, duplicates, excluded_region = sheets_client.get_today_birthdays(ws, effective_sent_keys, today=kyiv_now)
    except Exception as e:  # noqa: BLE001
        logger.exception("Помилка читання іменинників")
        telegram_client.log(f"⚠️ ПОМИЛКА читання таблиці ({day}): {e}")
        return "error: reading birthdays failed"

    # 2б. Попередження про колізію ключів (двоє повних тезок з однаковою
    #     датою народження в одному підрозділі) — вкрай рідкісний випадок,
    #     але якщо трапився, попереджаємо один раз на день, щоб ви могли
    #     вручну переконатись, що обидва отримали привітання.
    if duplicates:
        last_dup_warn = sheets_client.get_state_cell(state_ws, config.STATE_CELL_DUP_WARNING)
        if last_dup_warn != day:
            names = ", ".join(sorted({d[0]["name"] for d in duplicates}))
            telegram_client.log(
                f"⚠️ УВАГА ({day}): знайдено співробітників з однаковим ПІБ, "
                f"датою народження і підрозділом — {names}. Бот привітає їх "
                f"як одну особу (одне повідомлення замість двох). Будь ласка, "
                f"перевірте вручну, чи всі такі люди отримали привітання."
            )
            sheets_client.set_state_cell(state_ws, config.STATE_CELL_DUP_WARNING, day)

    total = len(birthdays)

    # 3. Ранкове зведення — один раз на день, навіть якщо іменинників 0.
    #    Запам'ятовуємо, чи зведення ВЖЕ було до цього циклу (потрібно для
    #    кроку 3б нижче — щоб не плутати "перший цикл дня" з "новоприбулим").
    last_summary = sheets_client.get_state_cell(state_ws, config.STATE_CELL_SUMMARY_DATE)
    summary_already_existed = last_summary == day
    if not summary_already_existed:
        if total == 0:
            telegram_client.log(f"📋 ЛОГ ({day}): сьогодні Днів народження немає.")
        else:
            telegram_client.log(
                f"📋 ЛОГ ({day}): сьогодні День народження у {total} "
                f"працівник(ів). Очікуємо відправки привітань."
            )
        if excluded_region:
            names = ", ".join(sorted(p["name"] for p in excluded_region))
            telegram_client.log(
                f"ℹ️ ЛОГ ({day}): ще у {len(excluded_region)} співробітник(ів) "
                f"регіонального/адміністративного рівня сьогодні також День "
                f"народження — {names}. Бот їх НЕ вітає (за налаштуванням) — "
                f"їх привітають окремо безпосередньо керівники."
            )
        sheets_client.set_state_cell(state_ws, config.STATE_CELL_SUMMARY_DATE, day)

    # 3б. Виявлення НОВИХ іменинників, доданих HR-ом у таблицю ПІСЛЯ того,
    #     як ранкове зведення вже було надіслано (тобто це не перший цикл
    #     дня). Порівнюємо поточний список іменинників з тим, кого бот уже
    #     "бачив" сьогодні раніше — хто з'явився новим, про того окремо
    #     пишемо в логи, щоб було видно, що HR щось додав протягом дня.
    #     Список "кого вже бачили" читаємо як ОБ'ЄДНАННЯ таблиці й пам'яті
    #     процесу (той самий принцип лікування, що й для sent_keys) — щоб
    #     повне стирання клітинки не спричинило хибні "🆕 новий іменинник"
    #     для людей, яких насправді вже бачили раніше сьогодні.
    current_keys = {b["key"] for b in birthdays}
    sheet_seen_keys = sheets_client.get_seen_keys(state_ws)
    effective_seen_keys = sheet_seen_keys | _seen_keys_memory
    if summary_already_existed:
        new_arrivals = [b for b in birthdays if b["key"] not in effective_seen_keys]
        for person in new_arrivals:
            telegram_client.log(
                f"🆕 ЛОГ ({day}): у таблиці з'явився новий іменинник, якого не "
                f"було під час ранкового зведення — {person['name']}. "
                f"Загалом сьогодні іменинників: {total}."
            )
    updated_seen_keys = effective_seen_keys | current_keys
    if updated_seen_keys != sheet_seen_keys:
        sheets_client.set_seen_keys(state_ws, updated_seen_keys)
    _seen_keys_memory = updated_seen_keys

    if total == 0:
        return "ok: no birthdays today"

    pending = [b for b in birthdays if not b["sent"]]
    already_done = total - len(pending)

    # 4. Усіх уже привітано -> лог один раз
    if not pending:
        last_done = sheets_client.get_state_cell(state_ws, config.STATE_CELL_DONE_DATE)
        if last_done != day:
            telegram_client.log(
                f"🎉 ЛОГ ({day}): усіх {total} іменинник(ів) успішно привітано."
            )
            sheets_client.set_state_cell(state_ws, config.STATE_CELL_DONE_DATE, day)
        return "ok: all greeted"

    # 4б. Є кому вітати, але зараз поза "робочим вікном" (10:00-20:00 за
    #     Києвом) -> нічого не надсилаємо цим циклом, просто чекаємо.
    #     Наступний цикл (за ~2 хв) перевірить знову, і як тільки настане
    #     10:00 — розсилка почнеться автоматично, без жодного втручання.
    if not is_within_sending_window(kyiv_now):
        logger.info(
            "Є %s непривітаних, але зараз %s Києва — поза вікном "
            "%02d:00-%02d:00, чекаємо.",
            len(pending), kyiv_now.strftime("%H:%M"),
            config.SEND_WINDOW_START_HOUR, config.SEND_WINDOW_END_HOUR
        )
        return "ok: waiting for sending window (10:00-20:00 Kyiv)"

    # 5. Надсилаємо РІВНО ОДНЕ привітання за цей цикл. Шаблон основного
    #    тексту обираємо так, щоб він НЕ повторював жоден шаблон,
    #    використаний за останні 7 днів. Якщо це ПЕРШЕ привітання за
    #    день (overall_index=1) — так само обираємо вступну фразу, яка
    #    НЕ повторювалась за останні 31 день.
    #
    #    Обидві історії читаємо як ОБ'ЄДНАННЯ службового аркуша й пам'яті
    #    процесу (той самий принцип лікування, що й для sent_keys/seen_keys).
    person = pending[0]
    overall_index = already_done + 1
    today_naive = kyiv_now.replace(tzinfo=None)

    try:
        sheet_template_pairs = {(e["date"], e["idx"]) for e in sheets_client.get_template_history(state_ws)}
        _template_history_memory = _prune_history_pairs(
            _template_history_memory, config.TEMPLATE_HISTORY_DAYS, today_naive
        )
        effective_template_pairs = sheet_template_pairs | _template_history_memory
        if effective_template_pairs != sheet_template_pairs:
            sheets_client.set_template_history(
                state_ws, [{"date": d, "idx": i} for d, i in sorted(effective_template_pairs)]
            )
        excluded_template_indices = {i for _, i in effective_template_pairs}
    except Exception:  # noqa: BLE001
        logger.exception("Не вдалося прочитати історію шаблонів, продовжую без обмежень")
        excluded_template_indices = set()

    excluded_intro_indices = set()
    if overall_index <= 1:
        try:
            sheet_intro_pairs = {(e["date"], e["idx"]) for e in sheets_client.get_intro_history(state_ws)}
            _intro_history_memory = _prune_history_pairs(
                _intro_history_memory, config.INTRO_HISTORY_DAYS, today_naive
            )
            effective_intro_pairs = sheet_intro_pairs | _intro_history_memory
            if effective_intro_pairs != sheet_intro_pairs:
                sheets_client.set_intro_history(
                    state_ws, [{"date": d, "idx": i} for d, i in sorted(effective_intro_pairs)]
                )
            excluded_intro_indices = {i for _, i in effective_intro_pairs}
        except Exception:  # noqa: BLE001
            logger.exception("Не вдалося прочитати історію вступних фраз, продовжую без обмежень")

    greeting = build_greeting(
        person["name"], person.get("city", ""), person.get("city_type", ""),
        overall_index, excluded_template_indices, excluded_intro_indices,
    )
    text = greeting["text"]

    if greeting["template_repeated"]:
        telegram_client.log(
            f"⚠️ УВАГА ({day}): усі шаблони привітань були використані за "
            f"останні {config.TEMPLATE_HISTORY_DAYS} днів — довелось "
            f"повторити один із них. Рекомендую додати більше варіантів "
            f"у templates.py."
        )
    if greeting["intro_repeated"]:
        telegram_client.log(
            f"⚠️ УВАГА ({day}): усі вступні фрази були використані за "
            f"останні {config.INTRO_HISTORY_DAYS} днів — довелось "
            f"повторити одну з них. Рекомендую додати більше варіантів "
            f"у списку INTRO_PHRASES (bot_logic.py)."
        )

    ok = telegram_client.send_message(config.TARGET_CHAT_ID, config.TARGET_TOPIC_ID, text)

    if ok:
        try:
            # Джерело істини — ключ Ім'я+Дата+Підрозділ, незалежний від
            # номера рядка. Зберігаємо і на службовому аркуші, і в пам'ять
            # процесу.
            sheets_client.add_sent_key(state_ws, person["key"])
            _sent_keys_memory.add(person["key"])
            sheets_client.add_template_history_entry(state_ws, day, greeting["template_idx"])
            _template_history_memory.add((day, greeting["template_idx"]))
            if greeting["intro_idx"] is not None:
                sheets_client.add_intro_history_entry(state_ws, day, greeting["intro_idx"])
                _intro_history_memory.add((day, greeting["intro_idx"]))
        except Exception:  # noqa: BLE001
            logger.exception("Не вдалося зберегти позначку 'відправлено' для %s", person["name"])
        telegram_client.log(f"✅ Привітання {overall_index}/{total} — готово ({person['name']}).")
        return f"ok: sent {overall_index}/{total}"
    else:
        telegram_client.log(
            f"❌ Привітання {overall_index}/{total} — ПОМИЛКА відправки "
            f"({person['name']}). Спробуємо ще раз наступним циклом."
        )
        return "error: telegram send failed"

