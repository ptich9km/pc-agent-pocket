#!/usr/bin/env python3
"""Telegram <-> OpenCode bridge.

Forwards Telegram messages into a pinned OpenCode session and relays answers,
permission requests and progress back. Deliberately dependency-free (urllib)
and single-file so it can be audited in one read.

Security model:
- the bot is a remote shell on this machine, so only one chat id is served;
  it is bound once through a pairing code printed at first start;
- every action is appended to the log file;
- tool permissions are never auto-approved here: they are relayed as inline
  buttons and must be pressed in Telegram.
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.path.expanduser("~/.config/tg-opencode")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token", "token")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
LOG_FILE = os.path.join(CONFIG_DIR, "bridge.log")
WORKDIR = os.path.expanduser("~")
FOLDER_HISTORY_LIMIT = 8
BROWSER_PAGE_SIZE = 10
OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
TG = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000

BOT_COMMANDS = [
    {"command": "status", "description": "Статус сессии и папки"},
    {"command": "context", "description": "Модель, токены, кэш, стоимость"},
    {"command": "sessions", "description": "Список сессий и переключение"},
    {"command": "model", "description": "Модель: посмотреть или сменить"},
    {"command": "agent", "description": "Агент: посмотреть или сменить"},
    {"command": "history", "description": "История, откат и fork"},
    {"command": "form", "description": "Ответы на вопросы агента"},
    {"command": "compact", "description": "Сжать контекст (/compact confirm)"},
    {"command": "folder", "description": "Сменить папку проекта"},
    {"command": "browse", "description": "Обзор папок"},
    {"command": "menu", "description": "Показать кнопки меню"},
    {"command": "help", "description": "Справка"},
]

KEYBOARD_ROWS = [
    ["📊 Статус", "🗂 Сессии"],
    ["📁 Папка", "🆕 Новая"],
    ["⚙️ Модель", "👤 Агент"],
    ["❓ Помощь"],
]
BUTTON_COMMANDS = {
    "📊 Статус": "/status",
    "🗂 Сессии": "/sessions",
    "📁 Папка": "/folder",
    "🆕 Новая": "/new",
    "⚙️ Модель": "/model",
    "👤 Агент": "/agent",
    "❓ Помощь": "/help",
}


def main_keyboard():
    return {"keyboard": [[{"text": text} for text in row] for row in KEYBOARD_ROWS],
            "resize_keyboard": True, "persistent": True}


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_token():
    with open(TOKEN_FILE, encoding="utf-8") as handle:
        return handle.read().strip()


def http_json(url, payload=None, timeout=40):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


# ── state ─────────────────────────────────────────────────────────────────────


def default_state():
    return {
        "chat_id": None,
        "pair_code": f"{random.randint(0, 999999):06d}",
        "session_id": None,
        "last_message_id": None,
        "last_update_id": 0,
        "notified_permissions": [],
        "workdir": WORKDIR,
        "folder_history": [],
        "browser_cwd": None,
        "browser_page": 0,
        "browser_hidden": False,
        "job": None,
        "notify_done": True,
        "notify_all": False,
        "session_cursors": {},
        "active_forms": [],
        "notified_forms": [],
    }


def load_state():
    # Merge with defaults so an existing state.json gains the folder fields.
    state = default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as handle:
                state.update(json.load(handle))
        except ValueError:
            log("state.json is not valid JSON, starting from defaults")
    if not os.path.isdir(state.get("workdir") or ""):
        log(f"saved workdir {state.get('workdir')!r} is unavailable, using {WORKDIR}")
        state["workdir"] = WORKDIR
    history = []
    for path in state.get("folder_history") or []:
        if isinstance(path, str) and os.path.isdir(path) and path not in history:
            history.append(path)
    state["folder_history"] = history[:FOLDER_HISTORY_LIMIT]
    save_state(state)
    return state


def save_state(state):
    # The relay thread and the main loop can save concurrently. A shared
    # ".tmp" path makes one writer remove the other's file before replace().
    directory = os.path.dirname(STATE_FILE) or "."
    fd, tmp = tempfile.mkstemp(prefix="state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── project folders ───────────────────────────────────────────────────────────


def resolve_folder(value):
    """Return (canonical path, None) or (None, error message)."""
    value = (value or "").strip()
    if not value:
        return None, "Пустой путь."
    path = os.path.realpath(os.path.expanduser(value))
    if not os.path.isdir(path):
        return None, f"Папка не найдена: {value}"
    return path, None


def folder_choices(state):
    """Current and recently used folders, current first."""
    current = state.get("workdir") or WORKDIR
    choices = [current]
    for path in state.get("folder_history") or []:
        if path not in choices:
            choices.append(path)
    return [path for path in choices if os.path.isdir(path)][:FOLDER_HISTORY_LIMIT]


def folder_keyboard(state):
    rows = []
    for path in folder_choices(state):
        data = f"folder:{path}"
        # Telegram allows at most 64 bytes of callback_data; long paths are
        # still reachable through the /folder <path> command.
        if len(data.encode("utf-8")) > 64:
            continue
        label = os.path.basename(path.rstrip("/")) or path
        rows.append([{"text": f"📁 {label}"[:64], "callback_data": data}])
    return {"inline_keyboard": rows} if rows else None


def switch_folder(state, value):
    """Persist a new project folder and bind the next session to it."""
    path, error = resolve_folder(value)
    if error:
        return False, error
    previous = state.get("workdir")
    if path == previous:
        return False, f"Уже в папке {path}"
    history = [item for item in (state.get("folder_history") or []) if item != path]
    if previous and os.path.isdir(previous) and previous not in history:
        history.insert(0, previous)
    state["workdir"] = path
    state["folder_history"] = history[:FOLDER_HISTORY_LIMIT]
    # A session is pinned to its location; a new one must be created for the
    # new folder. Drop the message tip so the relay does not replay old output.
    state["session_id"] = None
    state["last_message_id"] = None
    state["job"] = None
    save_state(state)
    log(f"workdir {previous} -> {path}")
    try:
        session_id = ensure_session(state)
    except Exception as error:  # noqa: BLE001 - state is saved, next prompt retries
        log(f"session creation in {path} failed: {error}")
        return False, f"Папка сменена на {path}, но сессия не создана: {error}"
    return True, f"Папка: {path}\nНовая сессия: {session_id}"


# ── directory browser ─────────────────────────────────────────────────────────


def browser_dirs(cwd, include_hidden=False):
    try:
        entries = os.listdir(cwd)
    except OSError as error:
        return [], str(error)
    dirs = []
    for name in entries:
        if name.startswith(".") and not include_hidden:
            continue
        path = os.path.join(cwd, name)
        if os.path.isdir(path):
            dirs.append((name, path))
    dirs.sort(key=lambda item: item[0].lower())
    return dirs, None


def browser_view(state):
    """Build (text, keyboard) for the current browser directory.

    Button payloads use short indices, not paths, so Telegram's 64-byte
    callback_data limit is never an issue.
    """
    cwd = state.get("browser_cwd") or state.get("workdir") or WORKDIR
    dirs, error = browser_dirs(cwd, bool(state.get("browser_hidden")))
    pages = max(1, (len(dirs) + BROWSER_PAGE_SIZE - 1) // BROWSER_PAGE_SIZE)
    page = min(max(0, int(state.get("browser_page") or 0)), pages - 1)
    start = page * BROWSER_PAGE_SIZE

    lines = [f"📂 {cwd}"]
    if error:
        lines.append(f"Ошибка чтения: {error}")
    else:
        lines.append(f"Папок: {len(dirs)}")
        if pages > 1:
            lines.append(f"Страница {page + 1}/{pages}")

    keyboard = [[{
        "text": f"📁 {name}"[:64],
        "callback_data": f"bd:{start + offset}",
    }] for offset, (name, _) in enumerate(dirs[start:start + BROWSER_PAGE_SIZE])]

    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"bp:{page - 1}"})
    if page + 1 < pages:
        nav.append({"text": "➡️", "callback_data": f"bp:{page + 1}"})
    nav.append({"text": "⬆️ Вверх", "callback_data": "bu"})
    nav.append({"text": "🏠 ~", "callback_data": "bhome"})
    keyboard.append(nav)
    keyboard.append([
        {"text": "👁 Скрытые", "callback_data": "bt"},
        {"text": "✅ Выбрать", "callback_data": "bs"},
        {"text": "❌ Закрыть", "callback_data": "bx"},
    ])

    state["browser_cwd"] = cwd
    state["browser_page"] = page
    return "\n".join(lines), {"inline_keyboard": keyboard}


def browser_callback(data, state, tg, query):
    """Handle a browser button press; returns True when handled."""
    prefixes = ("bd:", "bp:", "bu", "bhome", "bt", "bs", "bx")
    if not data.startswith(prefixes):
        return False
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    answer = "ok"

    if data.startswith("bd:"):
        dirs, error = browser_dirs(state.get("browser_cwd") or WORKDIR,
                                   bool(state.get("browser_hidden")))
        try:
            index = int(data.split(":", 1)[1])
            state["browser_cwd"] = dirs[index][1]
            state["browser_page"] = 0
        except (ValueError, IndexError):
            answer = "пункт устарел"
    elif data.startswith("bp:"):
        try:
            state["browser_page"] = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            answer = "неверная страница"
    elif data == "bu":
        cwd = state.get("browser_cwd") or WORKDIR
        state["browser_cwd"] = os.path.dirname(cwd.rstrip("/")) or "/"
        state["browser_page"] = 0
    elif data == "bhome":
        state["browser_cwd"] = os.path.expanduser("~")
        state["browser_page"] = 0
    elif data == "bt":
        state["browser_hidden"] = not bool(state.get("browser_hidden"))
        state["browser_page"] = 0
    elif data == "bs":
        ok, text = switch_folder(state, state.get("browser_cwd") or WORKDIR)
        answer = "папка выбрана" if ok else "не удалось"
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer})
        try:
            tg.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text,
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as error:  # noqa: BLE001 - fall back to a new message
            log(f"browser select edit failed: {error}")
            tg.send(chat_id, text)
        return True
    elif data == "bx":
        state.pop("browser_cwd", None)
        state.pop("browser_page", None)
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": "закрыто"})
        try:
            tg.call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as error:  # noqa: BLE001 - cosmetic cleanup only
            log(f"browser close failed: {error}")
        save_state(state)
        return True

    text, keyboard = browser_view(state)
    save_state(state)
    tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer})
    try:
        tg.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "reply_markup": keyboard,
        })
    except Exception as error:  # noqa: BLE001 - edit may be unsupported; send fresh
        log(f"browser edit failed: {error}")
        tg.send(chat_id, text, keyboard)
    return True


# ── telegram ──────────────────────────────────────────────────────────────────


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, payload=None, timeout=40):
        url = TG.format(token=self.token, method=method)
        return http_json(url, payload, timeout=timeout)

    def send(self, chat_id, text, keyboard=None):
        for chunk in chunk_text(text):
            payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if keyboard:
                payload["reply_markup"] = keyboard
                keyboard = None  # only on the last chunk
            self.call("sendMessage", payload)
        log(f"sent {len(text or '')} chars to {chat_id}")

    def updates(self, offset, timeout=30):
        url = TG.format(token=self.token, method="getUpdates")
        query = urllib.parse.urlencode({
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        })
        with urllib.request.urlopen(f"{url}?{query}", timeout=timeout + 15) as response:
            return json.loads(response.read().decode())


def chunk_text(text, size=MAX_LEN):
    text = text or ""
    if len(text) <= size:
        return [text or "(пусто)"]
    return [text[i:i + size] for i in range(0, len(text), size)]


# ── opencode ──────────────────────────────────────────────────────────────────


def oc(args, payload=None, timeout=120):
    """Call the opencode CLI in the project directory.

    The CLI truncates JSON written to a pipe, so stdout must go to a file;
    reading it back avoids "Unterminated string" on large session contexts.
    """
    command = [OPENCODE] + args
    if payload is not None:
        command += ["--data", json.dumps(payload)]
    temp_dir = "/tmp/opencode" if os.path.isdir("/tmp/opencode") else None
    fd, out_path = tempfile.mkstemp(prefix="oc-out-", dir=temp_dir)
    os.close(fd)
    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            result = subprocess.run(
                command, cwd=WORKDIR, stdout=handle, stderr=subprocess.PIPE,
                text=True, timeout=timeout,
            )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "opencode api failed").strip()[:500])
        with open(out_path, encoding="utf-8") as handle:
            output = handle.read().strip()
        return json.loads(output) if output else {}
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def ensure_session(state):
    if state.get("session_id"):
        return state["session_id"]
    workdir = state.get("workdir") or WORKDIR
    # The location must be explicit: without it the CLI creates the session in
    # the user's home directory instead of the selected project folder.
    created = oc(["api", "post", "/api/session"],
                 {"title": "Telegram", "location": {"directory": workdir}})
    session_id = created["data"]["id"]
    state["session_id"] = session_id
    save_state(state)
    log(f"new session {session_id} in {workdir}")
    return session_id


def active_sessions():
    try:
        data = oc(["api", "get", "/api/session/active"])
        return set((data.get("data") or {}).keys())
    except Exception as error:  # noqa: BLE001 - status must never crash the bot
        log(f"active check failed: {error}")
        return set()


def session_is_running(session_id):
    """A session is busy until OpenCode sets time.idle on it.

    The active-session list is not usable here: an idle session keeps appearing
    there as "running", which produced false statuses and blocked poweroff.
    """
    if not session_id:
        return False
    try:
        timing = session_info(session_id).get("time") or {}
    except Exception as error:  # noqa: BLE001 - status must never crash the bot
        log(f"running check failed: {error}")
        return False
    return timing.get("idle") is None


def prompt(session_id, text):
    return oc(["api", "post", f"/api/session/{session_id}/prompt"], {"text": text})


def messages(session_id):
    data = oc(["api", "get", f"/api/session/{session_id}/message"])
    return data.get("data") or []


def new_messages(session_id, last_id):
    """Assistant texts after last_id, plus the newest processed message id.

    The API returns newest first; sort it before slicing, same as the relay.
    A newest assistant message without time.completed is still streaming and
    is left for a later call, so the cursor never skips unfinished text.
    """
    data = sorted(messages(session_id),
                  key=lambda item: ((item.get("time") or {}).get("created", 0),
                                    item.get("id") or ""))
    ids = [message.get("id") for message in data]
    start = ids.index(last_id) + 1 if last_id in ids else max(0, len(data) - 2)
    fresh = []
    newest = last_id
    for offset, message in enumerate(data[start:]):
        timing = message.get("time") or {}
        is_last = start + offset == len(data) - 1
        if is_last and message.get("type") == "assistant" and not timing.get("completed"):
            break
        text = render(message)
        if text:
            fresh.append((message.get("id"), text))
        newest = message.get("id")
    return fresh, newest


def interrupt(session_id):
    return oc(["api", "post", f"/api/session/{session_id}/interrupt"], {})


def session_info(session_id):
    return oc(["api", "get", f"/api/session/{session_id}"]).get("data") or {}


def session_list():
    data = oc(["api", "get", "/api/session"]).get("data") or []
    return sorted(data, key=lambda item: (item.get("time") or {}).get("updated", 0),
                  reverse=True)


def sessions_view(state, page=0):
    """Build (text, keyboard) for a page of sessions.

    Button payloads carry indices, never session ids, to stay under Telegram's
    64-byte callback_data limit.
    """
    sessions = session_list()
    page_size = 8
    pages = max(1, (len(sessions) + page_size - 1) // page_size)
    page = min(max(0, int(page or 0)), pages - 1)
    start = page * page_size
    lines = [f"🗂 Сессии: {len(sessions)}", f"Страница {page + 1}/{pages}"]
    keyboard = []
    for offset, session in enumerate(sessions[start:start + page_size]):
        index = start + offset
        title = (session.get("title") or session["id"][:8])[:40]
        directory = (session.get("location") or {}).get("directory") or ""
        project = os.path.basename(directory.rstrip("/")) or "—"
        flag = "⏳" if session_is_running(session["id"]) else "✅"
        lines.append(f"{index + 1}. {flag} {title} · {project}")
        keyboard.append([{"text": f"{index + 1}. {title}"[:64],
                          "callback_data": f"ss:{index}"}])
    navigation = []
    if page > 0:
        navigation.append({"text": "⬅️", "callback_data": f"sp:{page - 1}"})
    if page + 1 < pages:
        navigation.append({"text": "➡️", "callback_data": f"sp:{page + 1}"})
    if navigation:
        keyboard.append(navigation)
    state["session_page"] = page
    return "\n".join(lines), ({"inline_keyboard": keyboard} if keyboard else None)


def select_session(state, session_id):
    """Attach an existing session and follow it to its project folder."""
    info = session_info(session_id)
    directory = (info.get("location") or {}).get("directory")
    if directory and os.path.isdir(directory) and directory != state.get("workdir"):
        previous = state.get("workdir")
        state["workdir"] = directory
        history = [item for item in (state.get("folder_history") or [])
                   if item != directory and item != previous]
        if previous and os.path.isdir(previous):
            history.insert(0, previous)
        state["folder_history"] = history[:FOLDER_HISTORY_LIMIT]
    state["session_id"] = session_id
    state["last_message_id"] = None
    state["job"] = None
    save_state(state)
    log(f"attached session {session_id} in {state.get('workdir')}")
    return f"Сессия: {session_id}\nПапка: {state.get('workdir')}"


def session_callback(data, state, tg, query):
    """Handle session-list button presses; returns True when handled."""
    if not data.startswith(("ss:", "sp:")):
        return False
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    if data.startswith("sp:"):
        try:
            text, keyboard = sessions_view(state, int(data.split(":", 1)[1]))
        except ValueError:
            text, keyboard = sessions_view(state, 0)
        answer = "ok"
    else:
        try:
            session = session_list()[int(data.split(":", 1)[1])]
            text = select_session(state, session["id"])
        except (ValueError, IndexError, KeyError) as error:
            text, answer = f"Сессия устарела: {error}", "не найдено"
        else:
            answer = "сессия выбрана"
        keyboard = {"inline_keyboard": []}
    save_state(state)
    tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer})
    try:
        tg.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "reply_markup": keyboard or {"inline_keyboard": []},
        })
    except Exception as error:  # noqa: BLE001 - fall back to a fresh message
        log(f"session callback edit failed: {error}")
        tg.send(chat_id, text, keyboard)
    return True


def context_messages(session_id):
    return oc(["api", "get", f"/api/session/{session_id}/context"]).get("data") or []


def default_model():
    return oc(["api", "get", "/api/model/default"]).get("data") or {}


def compact_session(session_id):
    """Ask OpenCode to compact (summarize) the session context."""
    return oc(["api", "post", f"/api/session/{session_id}/compact"], {})


def set_session_model(session_id, provider_id, model_id):
    return oc(["api", "post", f"/api/session/{session_id}/model"],
              {"model": {"providerID": provider_id, "id": model_id}})


def available_models():
    data = oc(["api", "get", "/api/model"]).get("data") or []
    return sorted(data, key=lambda item: (
        item.get("providerID") or "", item.get("id") or ""))


def set_session_agent(session_id, agent):
    return oc(["api", "post", f"/api/session/{session_id}/agent"], {"agent": agent})


def format_count(value):
    return f"{int(value or 0):,}"


def pending_permissions():
    try:
        return oc(["api", "get", "/api/permission/request"]).get("data") or []
    except Exception as error:  # noqa: BLE001
        log(f"permission poll failed: {error}")
        return []


def reply_permission(session_id, request_id, decision):
    return oc(["api", "post", f"/api/session/{session_id}/permission/{request_id}/reply"],
              {"decision": decision})


def session_forms(session_id):
    try:
        return oc(["api", "get", f"/api/session/{session_id}/form"]).get("data") or []
    except Exception as error:  # noqa: BLE001 - form polling is best effort
        log(f"form poll failed: {error}")
        return []


def reply_form(session_id, form_id, answer):
    return oc(["api", "post", f"/api/session/{session_id}/form/{form_id}/reply"],
              {"answer": answer})


def form_choices(form):
    """Flatten a form's simple fields into (key, value, label) buttons."""
    choices = []
    for field in form.get("fields") or []:
        key = field.get("key")
        if not key:
            continue
        title = field.get("title") or key
        if field.get("type") == "boolean":
            choices.append((key, True, f"{title}: да"))
            choices.append((key, False, f"{title}: нет"))
        for option in field.get("options") or []:
            choices.append((key, option.get("value"),
                            f"{title}: {option.get('label') or option.get('value')}"))
    return choices


def forms_keyboard(entry):
    rows = []
    for index, (_, _, label) in enumerate(entry["choices"][:12]):
        rows.append([{"text": label[:64],
                      "callback_data": f"fm:{entry['token']}:{index}"}])
    rows.append([{"text": "❌ Закрыть", "callback_data": f"fx:{entry['token']}"}])
    return {"inline_keyboard": rows}


def relay_forms(state, tg):
    """Relay new agent questions (forms) from the selected session."""
    chat_id = state.get("chat_id")
    session_id = state.get("session_id")
    if not chat_id or not session_id:
        return
    notified = set(state.get("notified_forms") or [])
    active = state.setdefault("active_forms", [])
    for form in session_forms(session_id):
        form_id = form.get("id")
        if not form_id or form_id in notified:
            continue
        notified.add(form_id)
        if len(active) >= 10:
            continue
        token = str(int(state.get("form_seq") or 0) + 1)
        state["form_seq"] = int(token)
        entry = {
            "token": token,
            "id": form_id,
            "session": session_id,
            "title": form.get("title") or "Вопрос",
            "choices": form_choices(form),
            "answers": {},
            "fields": [{"key": f.get("key"), "title": f.get("title") or f.get("key"),
                        "type": f.get("type")}
                       for f in form.get("fields") or []],
        }
        active.append(entry)
        lines = [f"❓ {entry['title']}"]
        lines.extend(f"  · {f['title']} ({f['type']})" for f in entry["fields"])
        lines.append(f"Ответ: /form {token} <ключ>=<значение> или кнопки ниже")
        tg.send(chat_id, "\n".join(lines), forms_keyboard(entry) if entry["choices"] else None)
        log(f"form relayed: {form_id} token={token}")
    state["notified_forms"] = sorted(notified)[-200:]
    save_state(state)


def find_form(state, token):
    return next((entry for entry in state.get("active_forms") or []
                 if entry.get("token") == str(token)), None)


def form_callback(data, state, tg, query):
    """Handle form buttons: fm:<token>:<choice> and fx:<token>."""
    if not data.startswith(("fm:", "fx:")):
        return False
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    token = data.split(":", 2)[1]
    entry = find_form(state, token)
    if not entry:
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                        "text": "вопрос устарел"})
        return True
    if data.startswith("fx:"):
        state["active_forms"] = [item for item in state["active_forms"]
                                 if item.get("token") != token]
        save_state(state)
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": "закрыто"})
        tg.call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        return True
    try:
        index = int(data.split(":", 2)[2])
        key, value, label = entry["choices"][index]
        entry["answers"][key] = value
        reply_form(entry["session"], entry["id"], entry["answers"])
        entry["choices"].pop(index)
        answer = f"Ответ отправлен: {label}"
        if not entry["choices"]:
            state["active_forms"] = [item for item in state["active_forms"]
                                     if item.get("token") != token]
        save_state(state)
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer})
        if entry["choices"]:
            tg.call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": forms_keyboard(entry),
            })
        else:
            tg.call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            })
            tg.send(chat_id, answer)
    except Exception as error:  # noqa: BLE001 - keep the bot alive on bad form data
        log(f"form reply failed: {error}")
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                        "text": "не удалось ответить"})
    return True


def history_view(session_id, limit=8):
    """Build (text, keyboard) for recent user messages of a session."""
    items = sorted(messages(session_id),
                   key=lambda item: (item.get("time") or {}).get("created", 0))
    user_items = [item for item in items if item.get("type") == "user"][-limit:]
    lines = [f"🕘 История: {len(user_items)} сообщений"]
    keyboard = []
    for index, item in enumerate(user_items, 1):
        message_id = item.get("id")
        text = " ".join(str(part.get("text") or "") for part in item.get("content") or [])
        text = text.strip().replace("\n", " ")[:100] or "(без текста)"
        lines.append(f"{index}. {text}")
        if message_id:
            keyboard.append([
                {"text": f"↩️ {index}", "callback_data": f"rv:{message_id}"},
                {"text": f"🌱 {index}", "callback_data": f"fk:{message_id}"},
            ])
    return "\n".join(lines), ({"inline_keyboard": keyboard} if keyboard else None)


def revert_to_message(session_id, message_id):
    oc(["api", "post", f"/api/session/{session_id}/revert/stage"],
       {"messageID": message_id, "files": True})
    return oc(["api", "post", f"/api/session/{session_id}/revert/commit"], {})


def fork_session(session_id, message_id):
    result = oc(["api", "post", f"/api/session/{session_id}/fork"],
                {"before": message_id})
    return (result.get("data") or {}).get("id")


def history_callback(data, state, tg, query):
    """Handle revert/fork buttons from /history."""
    if not data.startswith(("rv:", "rvy:", "rvn:", "fk:")):
        return False
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    session_id = state.get("session_id")
    action, _, target = data.partition(":")
    if action == "rvn":
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                        "text": "отменено"})
        return True
    if action == "rv":
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Откатить", "callback_data": f"rvy:{target}"},
            {"text": "❌ Нет", "callback_data": f"rvn:{target}"},
        ]]}
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": "?"})
        tg.send(chat_id, f"Откатить сессию до {target}? "
                         "Изменения после этой точки будут отброшены.", keyboard)
        return True
    if not session_id:
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                        "text": "нет сессии"})
        return True
    try:
        if action == "rvy":
            revert_to_message(session_id, target)
            state["last_message_id"] = None
            save_state(state)
            text = f"Сессия откачена до {target}"
            log(f"reverted {session_id} to {target}")
        else:
            new_id = fork_session(session_id, target)
            if new_id:
                text = select_session(state, new_id)
                text = f"Новая ветка: {new_id}\n{text}"
            else:
                text = "Не удалось создать ветку"
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": "ок"})
        tg.call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        tg.send(chat_id, text)
    except Exception as error:  # noqa: BLE001 - destructive actions need error feedback
        log(f"history action failed: {error}")
        tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                        "text": "не удалось"})
        tg.send(chat_id, f"Не удалось выполнить действие: {error}")
    return True


def run_privileged(action):
    """poweroff/suspend: polkit allows it from the active local session.

    Falls back to passwordless sudo when someone adds a sudoers rule for it.
    """
    detail = ""
    for command in (["/usr/bin/systemctl", action],
                    ["sudo", "-n", "/usr/bin/systemctl", action]):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return True, " ".join(command)
        detail = (result.stderr or result.stdout or "").strip()[:300]
    return False, detail


# ── message rendering ─────────────────────────────────────────────────────────


def render(message):
    """Text of an assistant message: its text parts plus a tool summary."""
    if message.get("type") != "assistant":
        return None
    texts, tools = [], []
    for part in message.get("content") or []:
        if part.get("type") == "text" and part.get("text"):
            texts.append(part["text"].strip())
        elif part.get("type") == "tool":
            state = part.get("state") or {}
            status = state.get("status") or "?"
            tools.append(f"· {part.get('name')}: {status}")
    body = "\n\n".join(texts)
    if tools:
        body = (body + "\n\n" if body else "") + "\n".join(tools)
    return body or None


# ── main loop ─────────────────────────────────────────────────────────────────


def handle_command(text, state, tg, chat_id):
    """Returns True when the message was handled as a command."""
    if not text.startswith("/"):
        return False
    command, _, argument = text.partition(" ")
    command = command.lower()
    command = command.split("@")[0]
    session_id = state.get("session_id")

    if command == "/pair":
        if state.get("chat_id") == chat_id:
            tg.send(chat_id, "Этот чат уже привязан.")
        elif argument.strip() == state.get("pair_code"):
            state["chat_id"] = chat_id
            save_state(state)
            log(f"paired chat {chat_id}")
            tg.send(chat_id, "Чат привязан. Пишите задачу текстом или /help.")
        else:
            tg.send(chat_id, "Неверный код привязки. Код напечатан в логе моста.")
        return True

    if state.get("chat_id") != chat_id:
        log(f"ignored message from unpaired chat {chat_id}")
        return True

    if command == "/start" or command == "/help":
        tg.send(chat_id, HELP, main_keyboard())
    elif command == "/menu":
        tg.send(chat_id, "Меню:", main_keyboard())
    elif command == "/status":
        running = session_is_running(session_id)
        lines = [
            f"папка: {state.get('workdir') or WORKDIR}",
            f"сессия: {session_id or '—'}",
            f"состояние: {'работает' if running else 'простаивает'}",
        ]
        if session_id:
            fresh, newest = new_messages(session_id, state.get("last_message_id"))
            state["last_message_id"] = newest
            save_state(state)
            if fresh:
                lines.append("")
                lines.extend(text for _, text in fresh[-2:])
        tg.send(chat_id, "\n".join(lines))
    elif command == "/context":
        if not session_id:
            tg.send(chat_id, "Сессии ещё нет.")
        else:
            try:
                info = session_info(session_id)
                messages = context_messages(session_id)
                reference = info.get("model")
                if not reference:
                    for message in reversed(messages):
                        if message.get("model"):
                            reference = message["model"]
                            break
                fallback = default_model()
                if not reference:
                    reference = fallback
                tokens = info.get("tokens") or {}
                cache = tokens.get("cache") or {}
                title = f"{reference.get('providerID', '?')}/{reference.get('id', '?')}"
                if reference.get("variant"):
                    title += f" ({reference['variant']})"
                if reference.get("name"):
                    title += f" — {reference['name']}"
                elif (fallback.get("id") == reference.get("id")
                      and fallback.get("providerID") == reference.get("providerID")
                      and fallback.get("name")):
                    title += f" — {fallback['name']}"
                lines = [
                    f"сессия: {session_id}",
                    f"папка: {state.get('workdir') or WORKDIR}",
                    f"модель: {title}",
                    f"сообщений в контексте: {len(messages)}",
                    "токены (накопительно): "
                    f"input {format_count(tokens.get('input'))} · "
                    f"output {format_count(tokens.get('output'))} · "
                    f"reasoning {format_count(tokens.get('reasoning'))}",
                    "кэш (накопительно): "
                    f"read {format_count(cache.get('read'))} · "
                    f"write {format_count(cache.get('write'))}",
                    f"стоимость: ${info.get('cost', 0)}",
                ]
                limit = (fallback.get("limit") or {}).get("context")
                if (limit and fallback.get("id") == reference.get("id")
                        and fallback.get("providerID") == reference.get("providerID")):
                    lines.append(f"лимит контекста: {format_count(limit)}")
                lines.append("Сжать контекст: /compact")
                tg.send(chat_id, "\n".join(lines))
            except Exception as error:  # noqa: BLE001 - status command must not kill the bot
                tg.send(chat_id, f"Не удалось прочитать контекст: {error}")
    elif command == "/model":
        if not session_id:
            tg.send(chat_id, "Сессии ещё нет.")
        else:
            request = argument.strip()
            if request.lower().startswith("set "):
                request = request[4:].strip()
            if "/" in request:
                provider_id, model_id = request.split("/", 1)
                try:
                    set_session_model(session_id, provider_id.strip(), model_id.strip())
                    tg.send(chat_id, f"Модель: {provider_id.strip()}/{model_id.strip()}")
                    log(f"model switch {session_id} -> {provider_id}/{model_id}")
                except Exception as error:  # noqa: BLE001
                    tg.send(chat_id, f"Не удалось сменить модель: {error}")
            else:
                try:
                    info = session_info(session_id)
                    current = info.get("model") or {}
                    lines = ["Текущая модель: "
                             f"{current.get('providerID', '?')}/{current.get('id', '?')}"]
                    lines.append("Сменить: /model set <providerID>/<modelID>")
                    models = available_models()
                    favorite = [item for item in models
                                if item.get("providerID") == "opencode-go"][:12]
                    if not favorite:
                        favorite = models[:12]
                    lines.append("Доступные (первые 12):")
                    lines.extend(f"  {item.get('providerID')}/{item.get('id')}"
                                 f" — {item.get('name') or ''}" for item in favorite)
                    tg.send(chat_id, "\n".join(lines))
                except Exception as error:  # noqa: BLE001
                    tg.send(chat_id, f"Не удалось получить модели: {error}")
    elif command == "/agent":
        if not session_id:
            tg.send(chat_id, "Сессии ещё нет.")
        elif argument.strip():
            agent = argument.strip().split()[0]
            try:
                set_session_agent(session_id, agent)
                tg.send(chat_id, f"Агент: {agent}")
                log(f"agent switch {session_id} -> {agent}")
            except Exception as error:  # noqa: BLE001
                tg.send(chat_id, f"Не удалось сменить агента: {error}")
        else:
            try:
                current = session_info(session_id).get("agent") or "—"
                tg.send(chat_id, f"Агент: {current}\n"
                                 "Сменить: /agent build|plan|general|explore|<имя>")
            except Exception as error:  # noqa: BLE001
                tg.send(chat_id, f"Не удалось получить агента: {error}")
    elif command == "/form":
        active = state.get("active_forms") or []
        request = argument.strip()
        if not request:
            if not active:
                tg.send(chat_id, "Открытых вопросов нет.")
            else:
                lines = ["Открытые вопросы:"]
                lines.extend(f"  {item['token']}. {item['title']}" for item in active)
                lines.append("Ответ: /form <токен> <ключ>=<значение>")
                tg.send(chat_id, "\n".join(lines))
        else:
            parts = request.split()
            if parts[0].lower() == "done" and len(parts) > 1:
                token = parts[1]
                state["active_forms"] = [item for item in active
                                         if item.get("token") != token]
                save_state(state)
                tg.send(chat_id, f"Вопрос {token} закрыт")
            else:
                token, _, pair = request.partition(" ")
                key, separator, value = pair.partition("=")
                entry = find_form(state, token)
                if not entry or not separator:
                    tg.send(chat_id, "Формат: /form <токен> <ключ>=<значение>")
                else:
                    try:
                        entry["answers"][key.strip()] = value.strip()
                        reply_form(entry["session"], entry["id"], entry["answers"])
                        save_state(state)
                        tg.send(chat_id, f"Ответ отправлен: {key.strip()}")
                    except Exception as error:  # noqa: BLE001
                        tg.send(chat_id, f"Не удалось ответить: {error}")
    elif command == "/compact":
        if not session_id:
            tg.send(chat_id, "Сессии ещё нет.")
        elif argument.strip() not in ("confirm", "compact"):
            tg.send(chat_id, "Подтвердите: /compact confirm")
        else:
            try:
                result = compact_session(session_id)
                item = result.get("data") or {}
                tg.send(chat_id, f"Контекст сжат. Задача: {item.get('id') or '—'}")
                log(f"compaction requested for {session_id}: {item.get('id')}")
            except Exception as error:  # noqa: BLE001
                tg.send(chat_id, f"Не удалось сжать контекст: {error}")
    elif command == "/notify":
        choice = argument.strip().lower()
        if choice.startswith("all"):
            rest = choice[3:].strip()
            if rest in ("on", "вкл", "1"):
                enabled = True
            elif rest in ("off", "выкл", "0"):
                enabled = False
            else:
                enabled = not state.get("notify_all", False)
            state["notify_all"] = enabled
            if enabled:
                # Re-baseline so old turns are not replayed into the chat.
                state["session_cursors"] = {}
            save_state(state)
            tg.send(chat_id, "Уведомления по всем сессиям: вкл" if enabled
                    else "Уведомления по всем сессиям: выкл")
        else:
            if choice in ("on", "вкл", "1"):
                enabled = True
            elif choice in ("off", "выкл", "0"):
                enabled = False
            else:
                enabled = not state.get("notify_done", True)
            state["notify_done"] = enabled
            if not enabled:
                state["job"] = None
            save_state(state)
            tg.send(chat_id, f"Уведомление о завершении задачи: "
                             f"{'вкл' if enabled else 'выкл'}\n"
                             f"Все сессии: {'вкл' if state.get('notify_all') else 'выкл'}")
    elif command == "/new":
        state["session_id"] = None
        state["last_message_id"] = None
        state["job"] = None
        save_state(state)
        tg.send(chat_id, f"Новая сессия: {ensure_session(state)} в {state.get('workdir') or WORKDIR}")
    elif command == "/history":
        if not session_id:
            tg.send(chat_id, "Сессии ещё нет.")
        else:
            try:
                limit = int(argument.strip()) if argument.strip() else 8
            except ValueError:
                limit = 8
            try:
                text, keyboard = history_view(session_id, min(max(1, limit), 15))
            except Exception as error:  # noqa: BLE001
                text, keyboard = f"Не удалось прочитать историю: {error}", None
            tg.send(chat_id, text, keyboard)
    elif command == "/sessions":
        try:
            page = int(argument.strip()) if argument.strip() else 0
        except ValueError:
            page = 0
        try:
            text, keyboard = sessions_view(state, page)
        except Exception as error:  # noqa: BLE001
            text, keyboard = f"Не удалось получить сессии: {error}", None
        tg.send(chat_id, text, keyboard)
    elif command == "/folder":
        argument = argument.strip()
        if not argument:
            current = state.get("workdir") or WORKDIR
            lines = [f"Текущая папка: {current}"]
            history = [path for path in (state.get("folder_history") or []) if path != current]
            if history:
                lines.append("Недавние папки:")
                lines.extend(f"  {index}. {path}" for index, path in enumerate(history, 1))
            lines.append("Сменить: /folder <путь> или /folder default")
            tg.send(chat_id, "\n".join(lines), folder_keyboard(state))
        else:
            if argument in ("default", "reset"):
                argument = WORKDIR
            elif argument in ("-", "back", "prev"):
                history = state.get("folder_history") or []
                argument = history[0] if history else WORKDIR
            ok, text = switch_folder(state, argument)
            tg.send(chat_id, text)
    elif command in ("/browse", "/ls"):
        argument = argument.strip()
        if argument:
            path, error = resolve_folder(argument)
            if error:
                tg.send(chat_id, error)
                return True
            state["browser_cwd"] = path
            state["browser_page"] = 0
            save_state(state)
        text, keyboard = browser_view(state)
        tg.send(chat_id, text, keyboard)
    elif command == "/stop":
        if session_id:
            interrupt(session_id)
            tg.send(chat_id, "Отправил прерывание.")
        else:
            tg.send(chat_id, "Сессии ещё нет.")
    elif command == "/log":
        try:
            with open(LOG_FILE, encoding="utf-8") as handle:
                tail = handle.readlines()[-30:]
            tg.send(chat_id, "".join(tail))
        except OSError as error:
            tg.send(chat_id, f"Лог недоступен: {error}")
    elif command in ("/shutdown", "/suspend"):
        action = "poweroff" if command == "/shutdown" else "suspend"
        if argument.strip() not in ("confirm", action):
            tg.send(chat_id, f"Подтвердите: {command} confirm")
            return True
        if session_is_running(session_id):
            tg.send(chat_id, "Сессия выполняется — сначала /stop. Выключение отменено.")
            return True
        ok, detail = run_privileged(action)
        if ok:
            log(f"{action} requested via {detail}")
            tg.send(chat_id, f"Выполняю {action}. После выключения бот недоступен, "
                             "пока машину не включат физически.")
        else:
            log(f"{action} failed: {detail}")
            tg.send(chat_id, f"Не удалось выполнить {action}: {detail}")
        return True
    else:
        tg.send(chat_id, "Неизвестная команда. /help")
    return True


HELP = (
    "Команды:\n"
    "/status — состояние и последние сообщения\n"
    "/context — модель, токены, кэш и стоимость сессии\n"
    "/sessions [страница] — список сессий и переключение\n"
    "/model [set provider/id] — посмотреть или сменить модель\n"
    "/agent [имя] — посмотреть или сменить агента\n"
    "/history [n] — история с откатом и fork\n"
    "/form [токен ключ=значение] — ответ на вопрос агента\n"
    "/compact confirm — сжать контекст сессии\n"
    "/notify on|off — уведомление о своей задаче\n"
    "/notify all — уведомления по всем сессиям (по умолчанию выкл)\n"
    "/menu — показать кнопки меню\n"
    "/new — начать новую сессию в текущей папке\n"
    "/folder — показать/сменить папку проекта (кнопки недавних)\n"
    "/folder <путь> — переключиться на папку (~/ работает; default — исходная)\n"
    "/browse [путь] — обзор папок кнопками, выбрать текущую через ✅\n"
    "/stop — прервать текущую работу\n"
    "/log — хвост лога моста\n"
    "/shutdown confirm — выключить ПК\n"
    "/suspend confirm — усыпить ПК\n"
    "/help — эта справка\n\n"
    "Любой другой текст уходит как задача в OpenCode."
)


def main():
    token = read_token()
    tg = Telegram(token)
    state = load_state()
    lock = threading.Lock()
    if not state.get("chat_id"):
        log(f"pairing required, code: {state['pair_code']}")
    me = tg.call("getMe")["result"]
    log(f"bridge started as @{me['username']}, workdir {state.get('workdir')}")
    try:
        tg.call("setMyCommands", {"commands": BOT_COMMANDS})
        log("bot command menu registered")
    except Exception as error:  # noqa: BLE001 - menu is cosmetic, never fatal
        log(f"setMyCommands failed: {error}")

    stop = threading.Event()
    threading.Thread(target=relay_loop, args=(state, tg, lock, stop), daemon=True).start()

    while True:
        try:
            updates = tg.updates(state["last_update_id"] + 1)
        except Exception as error:  # noqa: BLE001 - network hiccups must not kill the bridge
            log(f"getUpdates failed: {error}")
            time.sleep(5)
            continue

        for update in updates.get("result") or []:
            state["last_update_id"] = max(state["last_update_id"], update["update_id"])
            save_state(state)

            if "callback_query" in update:
                query = update["callback_query"]
                chat_id = query["message"]["chat"]["id"]
                if state.get("chat_id") != chat_id:
                    continue
                data = query["data"] or ""
                if data.startswith("folder:"):
                    ok, text = switch_folder(state, data[len("folder:"):])
                    tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                                    "text": "папка сменена" if ok else "не удалось"})
                    try:
                        tg.call("editMessageReplyMarkup", {
                            "chat_id": chat_id,
                            "message_id": query["message"]["message_id"],
                            "reply_markup": {"inline_keyboard": []},
                        })
                    except Exception as error:  # noqa: BLE001 - cosmetic cleanup only
                        log(f"keyboard cleanup failed: {error}")
                    tg.send(chat_id, text)
                    continue
                if browser_callback(data, state, tg, query):
                    continue
                if session_callback(data, state, tg, query):
                    continue
                if form_callback(data, state, tg, query):
                    continue
                if history_callback(data, state, tg, query):
                    continue
                if not data.startswith("perm:"):
                    tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                                    "text": "неизвестное действие"})
                    continue
                _, request_id, decision = data.split(":", 2)
                session_id = state.get("session_id")
                try:
                    reply_permission(session_id, request_id, decision)
                    tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                                    "text": decision})
                    log(f"permission {request_id} -> {decision}")
                except Exception as error:  # noqa: BLE001
                    tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                                    "text": "не удалось"})
                    log(f"permission reply failed: {error}")
                continue

            message = update.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            text = (message.get("text") or "").strip()
            if not chat_id or not text:
                continue
            text = BUTTON_COMMANDS.get(text, text)
            if handle_command(text, state, tg, chat_id):
                continue
            if state.get("chat_id") != chat_id:
                log(f"ignored unpaired message from {chat_id}")
                continue

            session_id = ensure_session(state)
            try:
                prompt(session_id, text)
                log(f"prompt -> {session_id}: {text[:120]!r}")
                if state.get("notify_done", True):
                    state["job"] = {"session": session_id, "started": int(time.time() * 1000)}
                    save_state(state)
            except Exception as error:  # noqa: BLE001
                tg.send(chat_id, f"Не удалось отправить задачу: {error}")
                continue

        relay_permissions(state, tg)


def check_job_done(state, tg):
    """Notify once when the session finishes the job started by a prompt.

    A running session has no time.idle; a finished one has time.idle and an
    outcome. Active-session membership is not used: it stays set for idle
    sessions too, so it cannot signal completion reliably.
    """
    job = state.get("job")
    if not job or not state.get("notify_done", True):
        return
    now = int(time.time() * 1000)
    if now - job.get("started", 0) > 30 * 60 * 1000:
        state["job"] = None
        save_state(state)
        return
    try:
        info = session_info(job.get("session"))
    except Exception as error:  # noqa: BLE001 - keep the pending job for the next poll
        log(f"job completion check failed: {error}")
        return
    timing = info.get("time") or {}
    idle = timing.get("idle")
    if not idle or idle + 2000 < job.get("started", 0):
        return
    # Mark this turn as seen so the all-session notifier cannot report it twice.
    state.setdefault("session_cursors", {})[job.get("session")] = idle
    state["job"] = None
    save_state(state)
    duration = max(0, (idle - job.get("started", 0)) // 1000)
    labels = {
        "succeeded": "завершена",
        "failed": "завершилась с ошибкой",
        "interrupted": "прервана",
    }
    label = labels.get(info.get("outcome"), "завершена")
    tg.send(state["chat_id"], f"✅ Задача {label} · {duration} c")
    log(f"job done: {job.get('session')} outcome={info.get('outcome')} duration={duration}s")


def check_all_sessions_done(state, tg):
    """Notify once per finished turn in every session, not just our own."""
    if not state.get("notify_all") or not state.get("chat_id"):
        return
    try:
        sessions = session_list()
    except Exception as error:  # noqa: BLE001 - keep polling next iteration
        log(f"all-session poll failed: {error}")
        return
    cursors = state.setdefault("session_cursors", {})
    baseline = not cursors
    changed = False
    for session in sessions:
        sid = session.get("id")
        timing = session.get("time") or {}
        idle = timing.get("idle")
        if not sid:
            continue
        previous = cursors.get(sid)
        if previous is None:
            # Baseline existing sessions so enabling the feature cannot replay
            # old turns into the chat.
            cursors[sid] = idle or timing.get("created") or 0
            changed = True
            continue
        if not idle or idle <= previous:
            continue
        cursors[sid] = idle
        changed = True
        if baseline:
            continue
        try:
            items = sorted(messages(sid),
                           key=lambda item: (item.get("time") or {}).get("created", 0))
        except Exception as error:  # noqa: BLE001 - notification is best effort
            log(f"all-session message fetch failed: {error}")
            continue
        reply = ""
        for item in reversed(items):
            if item.get("type") == "assistant":
                reply = render(item) or ""
                if reply:
                    break
        directory = (session.get("location") or {}).get("directory") or ""
        project = os.path.basename(directory.rstrip("/")) or sid[:8]
        title = session.get("title") or sid[:8]
        lines = [f"✅ Готово · {project} · {title}"]
        if reply:
            lines.append(reply[:600])
        tg.send(state["chat_id"], "\n".join(lines))
        log(f"session done: {sid} idle={idle}")
    if changed:
        save_state(state)


def relay_messages(state, tg, lock):
    """Send assistant messages that appeared since the last relay.

    This runs continuously in its own thread: the previous design waited inside
    the prompt handler and gave up after a few idle seconds, so a reply that
    arrived later was never delivered.
    """
    with lock:
        chat_id = state.get("chat_id")
        session_id = state.get("session_id")
        last_id = state.get("last_message_id")
    if not chat_id or not session_id:
        return
    try:
        data = messages(session_id)
    except Exception as error:  # noqa: BLE001
        log(f"message poll failed: {error}")
        return
    if not data:
        return
    # The API returns newest first; the relay logic needs chronological order.
    data = sorted(data, key=lambda item: ((item.get("time") or {}).get("created", 0),
                                          item.get("id") or ""))
    ids = [message.get("id") for message in data]
    if last_id is None:
        # First observation: remember the tip, never replay history.
        with lock:
            state["last_message_id"] = ids[-1]
            save_state(state)
        return
    start = ids.index(last_id) + 1 if last_id in ids else max(0, len(data) - 3)
    if start < len(ids):
        log(f"relay: {len(ids) - start} new message(s), last was {last_id}")
    processed = last_id
    for offset, message in enumerate(data[start:]):
        timing = message.get("time") or {}
        is_last = start + offset == len(data) - 1
        if is_last and message.get("type") == "assistant" and not timing.get("completed"):
            # Still streaming: rendering now would snapshot a partial text and
            # advancing the cursor would skip the rest forever. Only the newest
            # message can still be streaming; revisit it on the next poll.
            break
        text = render(message)
        if text and message.get("type") == "assistant":
            tg.send(chat_id, text)
        processed = message.get("id")
    if processed != last_id:
        with lock:
            state["last_message_id"] = processed
            save_state(state)


def relay_loop(state, tg, lock, stop):
    while not stop.is_set():
        try:
            relay_messages(state, tg, lock)
            relay_permissions(state, tg)
            relay_forms(state, tg)
            check_job_done(state, tg)
            check_all_sessions_done(state, tg)
        except Exception as error:  # noqa: BLE001 - the relay thread must never die silently
            log(f"relay iteration failed: {error!r}")
        stop.wait(2)


def relay_permissions(state, tg):
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    notified = set(state.get("notified_permissions") or [])
    for request in pending_permissions():
        request_id = request.get("id")
        if not request_id or request_id in notified:
            continue
        keyboard = {"inline_keyboard": [[
            {"text": "Разрешить", "callback_data": f"perm:{request_id}:once"},
            {"text": "Всегда", "callback_data": f"perm:{request_id}:always"},
            {"text": "Отклонить", "callback_data": f"perm:{request_id}:reject"},
        ]]}
        tg.send(chat_id,
                f"Нужно разрешение: {request.get('action')}\n{request.get('message') or ''}\n"
                f"{', '.join(request.get('resources') or [])[:500]}",
                keyboard)
        notified.add(request_id)
        state["notified_permissions"] = sorted(notified)[-100:]
        save_state(state)
        log(f"permission requested {request_id} {request.get('action')}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("bridge stopped")
        sys.exit(0)
