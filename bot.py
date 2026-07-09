import os
import re
import json
import random
import time
import shutil
import subprocess
import threading
import datetime
import requests

# ==================== НАСТРОЙКИ ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARTS_DIR = os.path.join(BASE_DIR, "arts")
STATE_DIR = os.path.join(BASE_DIR, "state")
POSTED_FILE = os.path.join(STATE_DIR, "posted.json")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL", "")
OWNER_ID = str(os.environ.get("OWNER_ID", ""))

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
KZ_OFFSET = datetime.timedelta(hours=5)  # Казахстан UTC+5

DEFAULT_CONFIG = {
    "schedule": ["10:00", "12:00", "14:00", "16:00", "20:00", "22:00"],
    "paused": False,
    "autoclean": False,
    "tags": True,          # подписывать посты хэштегом персонажа
    "last_posted_slot": "",
}

# Временное состояние для пошагового ввода (например, ожидание ввода времени)
pending_action = {}


# ==================== ФАЙЛЫ / КОНФИГ ====================

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default() if callable(default) else default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_config():
    cfg = load_json(CONFIG_FILE, lambda: dict(DEFAULT_CONFIG))
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def kz_now():
    return datetime.datetime.utcnow() + KZ_OFFSET


# ==================== АРТЫ ====================

def list_arts():
    if not os.path.isdir(ARTS_DIR):
        return []
    return sorted(
        f for f in os.listdir(ARTS_DIR)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
    )


def get_posted():
    return set(load_json(POSTED_FILE, list))


def set_posted(posted):
    save_json(POSTED_FILE, sorted(posted))


def available_arts():
    posted = get_posted()
    return [f for f in list_arts() if f not in posted]


# ==================== TELEGRAM API ====================

def api(method, **kwargs):
    try:
        return requests.post(f"{API_URL}/{method}", timeout=30, **kwargs).json()
    except Exception as e:
        print(f"API error {method}: {e}")
        return {}


def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    return api("sendMessage", data=data)


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    return api("editMessageText", data=data)


def answer_callback(callback_id, text=""):
    api("answerCallbackQuery", data={"callback_query_id": callback_id, "text": text})


def send_photo_path(chat_id, path, caption=""):
    with open(path, "rb") as photo:
        return requests.post(
            f"{API_URL}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": photo},
            timeout=60,
        ).json()


def send_photo_with_keyboard(chat_id, path, caption="", keyboard=None):
    with open(path, "rb") as photo:
        data = {"chat_id": chat_id, "caption": caption}
        if keyboard is not None:
            data["reply_markup"] = json.dumps(keyboard)
        return requests.post(
            f"{API_URL}/sendPhoto",
            data=data,
            files={"photo": photo},
            timeout=60,
        ).json()


def download_file(file_id, dest):
    info = api("getFile", data={"file_id": file_id})
    if not info.get("ok"):
        return False
    fp = info["result"]["file_path"]
    data = requests.get(f"{FILE_URL}/{fp}", timeout=60).content
    with open(dest, "wb") as f:
        f.write(data)
    return True


# ==================== КЛАВИАТУРЫ (КНОПКИ) ====================

def main_menu():
    cfg = get_config()
    pause_label = "▶️ Возобновить" if cfg["paused"] else "⏸ Пауза"
    clean_label = "🧹 Автоочистка: ВКЛ" if cfg["autoclean"] else "🧹 Автоочистка: выкл"
    tags_label = "🏷 Теги: ВКЛ" if cfg.get("tags", True) else "🏷 Теги: выкл"
    return {
        "inline_keyboard": [
            [{"text": "📦 Очередь", "callback_data": "queue"},
             {"text": "🖼 Показать арты", "callback_data": "preview"}],
            [{"text": "⏰ Расписание", "callback_data": "schedule"},
             {"text": "🚀 Опубликовать сейчас", "callback_data": "postnow"}],
            [{"text": "🗑 Очистить опубликованные", "callback_data": "clearposted"}],
            [{"text": clean_label, "callback_data": "toggleclean"}],
            [{"text": tags_label, "callback_data": "toggletags"}],
            [{"text": pause_label, "callback_data": "togglepause"}],
            [{"text": "🖥 Статус сервера", "callback_data": "status"},
             {"text": "🔄 Перезапуск бота", "callback_data": "restart"}],
        ]
    }


def schedule_menu():
    cfg = get_config()
    rows = []
    for t in cfg["schedule"]:
        rows.append([{"text": f"🕐 {t}", "callback_data": "noop"},
                     {"text": "❌ убрать", "callback_data": f"deltime:{t}"}])
    rows.append([{"text": "➕ Добавить время", "callback_data": "addtime"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def back_menu():
    return {"inline_keyboard": [[{"text": "⬅️ В меню", "callback_data": "menu"}]]}


# ==================== ТЕКСТЫ ЭКРАНОВ ====================

def queue_text():
    posted = get_posted()
    all_arts = list_arts()
    avail = [f for f in all_arts if f not in posted]
    used = [f for f in all_arts if f in posted]
    cfg = get_config()

    # место на диске
    total, used_disk, free = shutil.disk_usage(BASE_DIR)
    free_gb = free / (1024**3)

    status = "⏸ на паузе" if cfg["paused"] else "▶️ работает"
    return (
        f"📦 <b>Очередь публикаций</b>\n\n"
        f"Ждут публикации: <b>{len(avail)}</b>\n"
        f"Уже опубликовано: <b>{len(used)}</b>\n"
        f"Всего на диске: <b>{len(all_arts)}</b>\n\n"
        f"Состояние: {status}\n"
        f"Свободно на диске: <b>{free_gb:.1f} ГБ</b>\n\n"
        f"Постов в день: <b>{len(cfg['schedule'])}</b> "
        f"(хватит примерно на {len(avail) // max(len(cfg['schedule']),1)} дн.)"
    )


def status_text():
    # аптайм
    try:
        with open("/proc/uptime") as f:
            up_seconds = float(f.read().split()[0])
        days = int(up_seconds // 86400)
        hours = int((up_seconds % 86400) // 3600)
        uptime = f"{days}д {hours}ч"
    except Exception:
        uptime = "?"

    # память
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.5)
        mem_txt = f"{mem.percent}% ({mem.used//1024//1024}/{mem.total//1024//1024} МБ)"
        cpu_txt = f"{cpu}%"
    except Exception:
        mem_txt = "n/a"
        cpu_txt = "n/a"

    total, used_disk, free = shutil.disk_usage(BASE_DIR)
    disk_txt = f"{used_disk*100//total}% занято, свободно {free/(1024**3):.1f} ГБ"

    return (
        f"🖥 <b>Статус сервера</b>\n\n"
        f"⏱ Аптайм: {uptime}\n"
        f"⚙️ CPU: {cpu_txt}\n"
        f"🧠 Память: {mem_txt}\n"
        f"💾 Диск: {disk_txt}\n"
        f"🕐 Время (KZ): {kz_now().strftime('%H:%M')}"
    )


# ==================== ПУБЛИКАЦИЯ ====================

# ==================== РАСПОЗНАВАНИЕ ПЕРСОНАЖА ====================

SAUCENAO_KEY = os.environ.get("SAUCENAO_KEY", "")
SAUCENAO_URL = "https://saucenao.com/search.php"
SIMILARITY_THRESHOLD = 80.0   # ниже этого — не доверяем, ставим #tyan
FALLBACK_TAG = "#tyan"


def _clean_character_name(raw):
    """'makima (chainsaw man), power (...)' -> '#makima'"""
    first = raw.split(",")[0]                    # берём только первого персонажа
    first = re.sub(r"\(.*?\)", "", first)        # убираем скобки с тайтлом
    first = first.strip().lower()
    first = re.sub(r"[^a-z0-9]+", "_", first)    # пробелы и мусор -> _
    first = first.strip("_")
    if not first or len(first) > 40:
        return None
    return "#" + first


def detect_character(path):
    """Ищет арт на SauceNAO и возвращает хэштег персонажа. При любой неудаче -> #tyan"""
    if not SAUCENAO_KEY:
        return FALLBACK_TAG
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                SAUCENAO_URL,
                params={
                    "output_type": 2,      # json
                    "api_key": SAUCENAO_KEY,
                    "db": 999,             # искать по всем базам
                    "numres": 8,
                },
                files={"file": f},
                timeout=(10, 30),
            )
        data = resp.json()

        if data.get("header", {}).get("status", -1) != 0:
            print(f"saucenao status != 0: {data.get('header')}")
            return FALLBACK_TAG

        for result in data.get("results", []):
            try:
                similarity = float(result["header"]["similarity"])
            except (KeyError, ValueError):
                continue
            if similarity < SIMILARITY_THRESHOLD:
                continue
            characters = result.get("data", {}).get("characters")
            if not characters:
                continue                    # у этого источника нет тегов персонажей
            tag = _clean_character_name(characters)
            if tag:
                print(f"Распознан: {tag} ({similarity}%)")
                return tag

        return FALLBACK_TAG
    except Exception as e:
        print(f"detect_character error: {e}")
        return FALLBACK_TAG


def do_post(reason="scheduled", slot=""):
    avail = available_arts()
    if not avail:
        if OWNER_ID:
            send_message(OWNER_ID,
                         "⚠️ Время публикации, но арты закончились. "
                         "Пришли новые картинки боту.")
        return False, "Арты закончились"

    chosen = random.choice(avail)
    path = os.path.join(ARTS_DIR, chosen)

    cfg = get_config()
    caption = detect_character(path) if cfg.get("tags", True) else ""

    res = send_photo_path(CHANNEL, path, caption=caption)

    if res.get("ok"):
        cfg = get_config()
        if cfg["autoclean"]:
            # удаляем файл сразу и не копим posted
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            posted = get_posted()
            posted.add(chosen)
            set_posted(posted)
        if OWNER_ID:
            when = kz_now().strftime("%H:%M")
            tag_info = f"\nТег: {caption}" if caption else ""
            send_message(OWNER_ID, f"✅ Опубликовано в {when} ({reason}): {chosen}{tag_info}")
        return True, chosen
    else:
        if OWNER_ID:
            send_message(OWNER_ID, f"❌ Ошибка публикации: {res}")
        return False, str(res)


# ==================== ФОНОВЫЙ ПЛАНИРОВЩИК ====================

def scheduler_loop():
    while True:
        try:
            cfg = get_config()
            if not cfg["paused"]:
                now = kz_now()
                hhmm = now.strftime("%H:%M")
                today_key = now.strftime("%Y-%m-%d") + " " + hhmm
                if hhmm in cfg["schedule"] and cfg.get("last_posted_slot") != today_key:
                    # СНАЧАЛА ставим метку и сохраняем — чтобы повторный заход
                    # в ту же минуту не опубликовал второй раз
                    cfg["last_posted_slot"] = today_key
                    save_config(cfg)
                    # и только потом публикуем
                    do_post(reason="по расписанию", slot=hhmm)
        except Exception as e:
            print(f"scheduler error: {e}")
        time.sleep(20)  # проверка каждые 20 секунд — точность до минуты


# ==================== ОБРАБОТКА КНОПОК ====================

def handle_callback(cb):
    cid = str(cb["message"]["chat"]["id"])
    mid = cb["message"]["message_id"]
    data = cb["data"]
    cb_id = cb["id"]

    if cid != OWNER_ID:
        answer_callback(cb_id, "Нет доступа")
        return

    if data == "menu":
        edit_message(cid, mid, "🎨 <b>Панель управления ботом</b>\n\nВыбери действие:", main_menu())
    elif data == "queue":
        edit_message(cid, mid, queue_text(), back_menu())
    elif data == "status":
        edit_message(cid, mid, status_text(), back_menu())
    elif data == "schedule":
        edit_message(cid, mid, "⏰ <b>Расписание публикаций</b>\n\nТекущие времена:", schedule_menu())
    elif data == "preview":
        answer_callback(cb_id, "Отправляю превью...")
        all_avail = available_arts()
        avail = all_avail[:5]
        if not avail:
            send_message(cid, "Очередь пуста — нет артов, ждущих публикации.")
        else:
            total = len(all_avail)
            send_message(cid, f"🖼 Показываю {len(avail)} из {total} артов в очереди.\n"
                              f"Под каждым — кнопка удаления, если арт не нужен:")
            for name in avail:
                kb = {"inline_keyboard": [[
                    {"text": "🗑 Удалить этот арт", "callback_data": f"delart:{name}"}
                ]]}
                send_photo_with_keyboard(cid, os.path.join(ARTS_DIR, name), caption=name, keyboard=kb)
            if total > 5:
                send_message(cid, f"…и ещё {total-5}. Открой «Показать арты» снова после удаления, "
                                  f"чтобы увидеть следующие.", back_menu())
    elif data.startswith("delart:"):
        name = data.split(":", 1)[1]
        path = os.path.join(ARTS_DIR, name)
        if os.path.exists(path):
            try:
                os.remove(path)
                # на всякий случай убираем из posted, если было
                posted = get_posted()
                if name in posted:
                    posted.discard(name)
                    set_posted(posted)
                answer_callback(cb_id, "Удалено из очереди")
                edit_message(cid, mid, f"🗑 Удалён из очереди: {name}")
            except Exception as e:
                answer_callback(cb_id, "Ошибка удаления")
        else:
            answer_callback(cb_id, "Файл уже удалён")
            edit_message(cid, mid, f"Этот арт уже был удалён: {name}")
    elif data == "postnow":
        answer_callback(cb_id, "Публикую...")
        ok, info = do_post(reason="вручную")
        edit_message(cid, mid, queue_text(), back_menu())
    elif data == "clearposted":
        posted = get_posted()
        removed = 0
        for name in list(posted):
            p = os.path.join(ARTS_DIR, name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed += 1
                except Exception:
                    pass
        set_posted(set())
        answer_callback(cb_id, f"Удалено {removed} опубликованных артов")
        edit_message(cid, mid, f"🗑 Удалено {removed} опубликованных артов с диска.", back_menu())
    elif data == "toggleclean":
        cfg = get_config()
        cfg["autoclean"] = not cfg["autoclean"]
        save_config(cfg)
        answer_callback(cb_id, "Готово")
        edit_message(cid, mid, "🎨 <b>Панель управления ботом</b>\n\nВыбери действие:", main_menu())
    elif data == "toggletags":
        cfg = get_config()
        cfg["tags"] = not cfg.get("tags", True)
        save_config(cfg)
        answer_callback(cb_id, "Теги включены" if cfg["tags"] else "Теги выключены")
        edit_message(cid, mid, "🎨 <b>Панель управления ботом</b>\n\nВыбери действие:", main_menu())
    elif data == "togglepause":
        cfg = get_config()
        cfg["paused"] = not cfg["paused"]
        save_config(cfg)
        answer_callback(cb_id, "Пауза включена" if cfg["paused"] else "Возобновлено")
        edit_message(cid, mid, "🎨 <b>Панель управления ботом</b>\n\nВыбери действие:", main_menu())
    elif data == "restart":
        answer_callback(cb_id, "Перезапускаюсь...")
        send_message(cid, "🔄 Перезапускаю бота...")
        os._exit(1)  # systemd поднимет заново
    elif data == "addtime":
        pending_action[cid] = "addtime"
        send_message(cid, "Напиши новое время в формате ЧЧ:ММ (например 09:30):")
        answer_callback(cb_id)
    elif data.startswith("deltime:"):
        t = data.split(":", 1)[1]
        cfg = get_config()
        if t in cfg["schedule"]:
            cfg["schedule"].remove(t)
            save_config(cfg)
        answer_callback(cb_id, f"Убрал {t}")
        edit_message(cid, mid, "⏰ <b>Расписание публикаций</b>\n\nТекущие времена:", schedule_menu())
    elif data == "noop":
        answer_callback(cb_id)
    else:
        answer_callback(cb_id)


# ==================== ОБРАБОТКА СООБЩЕНИЙ ====================

def handle_message(msg):
    cid = str(msg["chat"]["id"])
    if cid != OWNER_ID:
        send_message(cid, "Этим ботом управляет только владелец.")
        return

    # Ожидание ввода времени
    if pending_action.get(cid) == "addtime" and "text" in msg:
        t = msg["text"].strip()
        try:
            datetime.datetime.strptime(t, "%H:%M")
            cfg = get_config()
            if t not in cfg["schedule"]:
                cfg["schedule"].append(t)
                cfg["schedule"].sort()
                save_config(cfg)
            pending_action.pop(cid, None)
            send_message(cid, f"✅ Добавил время {t}", schedule_menu())
        except ValueError:
            send_message(cid, "Неверный формат. Нужно ЧЧ:ММ, например 09:30. Попробуй ещё раз:")
        return

    # Получена картинка
    if "photo" in msg:
        os.makedirs(ARTS_DIR, exist_ok=True)
        fid = msg["photo"][-1]["file_id"]
        fname = f"art_{int(time.time()*1000)}.jpg"
        if download_file(fid, os.path.join(ARTS_DIR, fname)):
            n = len(available_arts())
            send_message(cid, f"✅ Арт добавлен в очередь (всего ждут: {n})")
        else:
            send_message(cid, "❌ Не удалось сохранить картинку, попробуй ещё раз.")
        return

    # Документ-картинка (если прислали файлом без сжатия)
    if "document" in msg:
        doc = msg["document"]
        name = doc.get("file_name", "")
        if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
            os.makedirs(ARTS_DIR, exist_ok=True)
            fname = f"art_{int(time.time()*1000)}{os.path.splitext(name)[1].lower()}"
            if download_file(doc["file_id"], os.path.join(ARTS_DIR, fname)):
                n = len(available_arts())
                send_message(cid, f"✅ Арт (файл) добавлен в очередь (всего ждут: {n})")
                return
        send_message(cid, "Это не картинка. Пришли изображение.")
        return

    # Текст / команды
    text = msg.get("text", "")
    if text in ("/start", "/menu", "меню", "Меню"):
        send_message(cid, "🎨 <b>Панель управления ботом</b>\n\nВыбери действие:", main_menu())
    else:
        send_message(cid, "Пришли картинку, чтобы добавить в очередь, или открой /menu", main_menu())


# ==================== ГЛАВНЫЙ ЦИКЛ ====================

def main():
    if not BOT_TOKEN or not CHANNEL or not OWNER_ID:
        print("Не заданы BOT_TOKEN / CHANNEL / OWNER_ID")
        return

    # запускаем планировщик в фоне
    threading.Thread(target=scheduler_loop, daemon=True).start()

    print("Бот запущен.")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=40).json()
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
        except Exception as e:
            print(f"main loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
