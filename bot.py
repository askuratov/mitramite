#!/usr/bin/env python3
"""Telegram-бот: проверяет статус трамите в RENAPER (mitramite.renaper.gob.ar).

Веб-форма RENAPER больше не работает, но бэкенд по-прежнему отвечает на
POST /busqueda.php. Этот эндпоинт требует токен reCAPTCHA v3 *с action*
(`submit_tramite`), а site key привязан к домену, поэтому решить капчу можно
только через внешний сервис.

Режим по умолчанию: 2captcha (https://2captcha.com) — без браузера, образ маленький.
Альтернативный режим: CAPTCHA_MODE=browser — headless Chromium через Playwright.

Запуск:
    python bot.py                 # запускает Telegram-бота (long polling)
    python bot.py 754991285       # разовая проверка в консоли, без Telegram
"""

import json
import logging
import os
import re
import sys
import threading
import time

import requests

# --------------------------------------------------------------------------- конфиг

BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("MITRAMITE_TOKEN") or ""
).strip()
RENAPER_BASE = os.environ.get("RENAPER_BASE", "https://mitramite.renaper.gob.ar").rstrip("/")
# Резервные значения: источник истины — сам сайт (см. discover_recaptcha).
SITE_KEY = os.environ.get("RENAPER_SITE_KEY", "6Ld2mMAbAAAAAM9grHC4aJ6pJT1TtvUz04q4Fvjs")
RECAPTCHA_ACTION = os.environ.get("RECAPTCHA_ACTION", "submit_tramite")
RECAPTCHA_ENTERPRISE = os.environ.get("RECAPTCHA_ENTERPRISE", "0").lower() in (
    "1",
    "true",
    "yes",
)
# Как часто перепроверять конфиг с сайтом (сек, 0 = выключено).
HEALTHCHECK_INTERVAL = float(os.environ.get("HEALTHCHECK_INTERVAL", "86400"))
ALLOWED_CHAT_IDS = {
    x.strip()
    for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
    if x.strip()
}
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "25"))
# Лимиты на пользователя. 0 = без лимита. Хранятся в памяти (рестарт их обнуляет).
RATE_MIN_INTERVAL = float(os.environ.get("RATE_LIMIT_MIN_INTERVAL", "180"))
RATE_DAILY = int(os.environ.get("RATE_LIMIT_DAILY", "50"))
# Логи по умолчанию выключены (бот stateless). LOG_LEVEL=INFO — только для отладки вручную.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "off").strip().lower()

CAPTCHA_MODE = os.environ.get("CAPTCHA_MODE", "2captcha").strip().lower()

# Имя переменной — 2CAPTCHA_API_KEY; TWOCAPTCHA_API_KEY — алиас, потому что
# идентификатор, начинающийся с цифры, нельзя экспортировать в zsh/bash.
TWOCAPTCHA_KEY = (
    os.environ.get("2CAPTCHA_API_KEY") or os.environ.get("TWOCAPTCHA_API_KEY") or ""
).strip()
TWOCAPTCHA_BASE = os.environ.get("TWOCAPTCHA_BASE", "https://2captcha.com").rstrip("/")
TWOCAPTCHA_MIN_SCORE = os.environ.get("TWOCAPTCHA_MIN_SCORE", "0.3")
TWOCAPTCHA_POLL = float(os.environ.get("TWOCAPTCHA_POLL", "5"))
TWOCAPTCHA_TIMEOUT = float(os.environ.get("TWOCAPTCHA_TIMEOUT", "180"))

# Облегчённый профиль для режима browser.
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chromium-headless-shell").strip()
BLOCK_RESOURCES = os.environ.get("BLOCK_RESOURCES", "1").lower() not in ("0", "false", "no")
BLOCKED_TYPES = {"image", "font", "media", "stylesheet"}
PAGE_TIMEOUT = float(os.environ.get("PAGE_TIMEOUT", "60"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
CHROME_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

log = logging.getLogger("mitramite")


# --------------------------------------------------------------------------- обнаружение конфига

_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
_RENDER_RE = re.compile(r"recaptcha/(api|enterprise)\.js\?render=([A-Za-z0-9_\-]+)")
_EXEC_RE = re.compile(
    r"grecaptcha(\.enterprise)?\.execute\(\s*[\"']([A-Za-z0-9_\-]+)[\"']\s*,"
    r"\s*\{[^}]*?action\s*:\s*[\"']([^\"']+)[\"']",
    re.S,
)


def _fetch(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.text


def discover_recaptcha() -> tuple[str | None, str | None, bool | None]:
    """Читает site key и action прямо с сайта RENAPER.

    Возвращает (site_key, action, enterprise); None там, где не удалось определить.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": CHROME_UA})
    home = _fetch(session, f"{RENAPER_BASE}/")

    site_key = None
    match = _RENDER_RE.search(home)
    if match:
        enterprise = match.group(1) == "enterprise"
        site_key = match.group(2)
    elif "enterprise.js" in home:
        enterprise = True
    elif "api.js" in home:
        enterprise = False
    else:
        enterprise = None

    action = None
    for src in _SCRIPT_SRC_RE.findall(home) + ["dist/js/main.js"]:
        if ".js" not in src:
            continue
        url = src if src.startswith("http") else f"{RENAPER_BASE}/{src.lstrip('/')}"
        try:
            js = _fetch(session, url)
        except Exception:  # noqa: BLE001
            continue
        match = _EXEC_RE.search(js)
        if match:
            if match.group(1):
                enterprise = True
            site_key = site_key or match.group(2)
            action = match.group(3)
            break

    return site_key, action, enterprise


def refresh_recaptcha(notify: bool) -> bool:
    """Перепроверяет конфиг на сайте. Возвращает True, если что-то изменилось."""
    global SITE_KEY, RECAPTCHA_ACTION, RECAPTCHA_ENTERPRISE
    try:
        key, action, enterprise = discover_recaptcha()
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось определить reCAPTCHA: %s", exc)
        return False

    changes = []
    if key and key != SITE_KEY:
        changes.append(("site key", SITE_KEY, key))
        SITE_KEY = key
    if action and action != RECAPTCHA_ACTION:
        changes.append(("action", RECAPTCHA_ACTION, action))
        RECAPTCHA_ACTION = action
    if enterprise is not None and enterprise != RECAPTCHA_ENTERPRISE:
        changes.append(("enterprise", RECAPTCHA_ENTERPRISE, enterprise))
        RECAPTCHA_ENTERPRISE = enterprise

    if changes:
        detail = "; ".join(f"{name}: {old} -> {new}" for name, old, new in changes)
        log.warning("reCAPTCHA обновлён с сайта: %s", detail)
        if notify:
            alert_admins(f"RENAPER изменил reCAPTCHA: {detail}")
        return True

    if not key and not action:
        log.warning("на сайте не найдены site key и action (схема изменилась?)")
    return False


def alert_admins(text: str):
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            send(chat_id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось уведомить %s: %s", chat_id, exc)


def healthcheck_loop():
    while True:
        time.sleep(HEALTHCHECK_INTERVAL)
        refresh_recaptcha(notify=True)


# --------------------------------------------------------------------------- 2captcha

def twocaptcha_token(session: requests.Session, pageurl: str) -> str:
    """Решает reCAPTCHA v3 (с action) через 2captcha и возвращает токен."""
    if not TWOCAPTCHA_KEY:
        raise RuntimeError("не задан 2CAPTCHA_API_KEY")

    params = {
        "key": TWOCAPTCHA_KEY,
        "method": "userrecaptcha",
        "version": "v3",
        "action": RECAPTCHA_ACTION,
        "min_score": TWOCAPTCHA_MIN_SCORE,
        "googlekey": SITE_KEY,
        "pageurl": pageurl,
        "json": 1,
    }
    if RECAPTCHA_ENTERPRISE:
        params["enterprise"] = 1

    response = session.get(
        f"{TWOCAPTCHA_BASE}/in.php",
        params=params,
        timeout=HTTP_TIMEOUT,
    )
    payload = response.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha: {payload.get('request')}")
    captcha_id = payload["request"]
    log.info("2captcha: задача %s отправлена, ждём токен…", captcha_id)

    deadline = time.time() + TWOCAPTCHA_TIMEOUT
    while time.time() < deadline:
        time.sleep(TWOCAPTCHA_POLL)
        response = session.get(
            f"{TWOCAPTCHA_BASE}/res.php",
            params={
                "key": TWOCAPTCHA_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1,
            },
            timeout=HTTP_TIMEOUT,
        )
        payload = response.json()
        if payload.get("status") == 1:
            return payload["request"]
        if payload.get("request") == "CAPCHA_NOT_READY":
            continue
        raise RuntimeError(f"2captcha: {payload.get('request')}")
    raise RuntimeError("2captcha: таймаут ожидания токена")


def query_2captcha(tramite: str) -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": CHROME_UA})
    token = twocaptcha_token(session, f"{RENAPER_BASE}/")

    response = session.post(
        f"{RENAPER_BASE}/busqueda.php",
        data={"tramite": tramite, "token": token, "action": RECAPTCHA_ACTION},
        headers={
            "Origin": RENAPER_BASE,
            "Referer": f"{RENAPER_BASE}/",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(
            "RENAPER вернул не-JSON (сайт недоступен или геоблокировка?)"
        )


# --------------------------------------------------------------------------- браузер

EXECUTE_JS = """
async ([siteKey, action, tramite, enterprise]) => {
  const g = enterprise ? grecaptcha.enterprise : grecaptcha;
  const token = await new Promise((ok, no) =>
    g.ready(() =>
      g.execute(siteKey, { action }).then(ok, no)));
  const fd = new FormData();
  fd.append('tramite', tramite);
  fd.append('token', token);
  fd.append('action', action);
  const r = await fetch('busqueda.php', { method: 'POST', body: fd });
  return await r.json();
}
"""


class RenapBrowser:
    """Переиспользуемый headless Chromium: выписывает токен и делает запрос."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def _start(self):
        from playwright.sync_api import sync_playwright

        log.info("запускаю Chromium (channel=%s)…", BROWSER_CHANNEL or "default")
        self._pw = sync_playwright().start()

        self._browser = None
        last_error = None
        for channel in ([BROWSER_CHANNEL] if BROWSER_CHANNEL else []) + [None]:
            try:
                self._browser = self._pw.chromium.launch(
                    headless=True, channel=channel, args=CHROME_ARGS
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("не удалось запустить channel=%s: %s", channel, exc)
        if self._browser is None:
            raise RuntimeError(f"не удалось запустить Chromium: {last_error}")

        self._context = self._browser.new_context(
            locale="es-AR",
            user_agent=CHROME_UA,
            viewport={"width": 1366, "height": 768},
        )
        self._page = self._context.new_page()
        if BLOCK_RESOURCES:
            self._page.route("**/*", self._block)
        self._page.goto(
            f"{RENAPER_BASE}/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000
        )
        self._page.wait_for_function(
            "() => window.grecaptcha && grecaptcha.ready", timeout=PAGE_TIMEOUT * 1000
        )

    @staticmethod
    def _block(route):
        if route.request.resource_type in BLOCKED_TYPES:
            route.abort()
        else:
            route.continue_()

    def _shutdown(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._context = self._page = None

    def _ensure(self):
        if self._page is None or self._page.is_closed():
            self._shutdown()
            self._start()

    def query(self, tramite: str) -> dict:
        last_error = None
        for attempt in (1, 2):
            try:
                self._ensure()
                return self._page.evaluate(
                    EXECUTE_JS, [SITE_KEY, RECAPTCHA_ACTION, tramite, RECAPTCHA_ENTERPRISE]
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("браузер: попытка %s не удалась: %s", attempt, exc)
                self._shutdown()
        raise RuntimeError(f"браузер: {last_error}")

    def close(self):
        self._shutdown()


BROWSER = RenapBrowser()


# --------------------------------------------------------------------------- RENAPER

def normalize_tramite(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits.zfill(11)


def consultar(raw_tramite: str) -> str:
    """Проверяет трамите и возвращает отформатированный текст (или бросает исключение)."""
    tramite = normalize_tramite(raw_tramite)
    if not 8 <= len(tramite) <= 11:
        raise ValueError("Номер трамите должен содержать 9 цифр (или 11 с нулями).")

    if CAPTCHA_MODE == "browser":
        payload = BROWSER.query(tramite)
    else:
        payload = query_2captcha(tramite)

    data = payload.get("data")
    if not data:
        errors = payload.get("errors") or payload
        raise RuntimeError(f"RENAPER: {json.dumps(errors, ensure_ascii=False)}")
    return format_status(tramite, data)


def format_status(tramite: str, data: dict) -> str:
    lines = [f"Трамите: {tramite}", ""]
    lines.append(
        f"Текущий статус: {data.get('id_ultimo_estado')} — "
        f"{data.get('descripcion_ultimo_estado')} ({data.get('fecha_ultimo_estado')})"
    )
    if data.get("descripcion_anteultimo_estado"):
        lines.append(
            f"Предыдущий статус: {data.get('id_anteultimo_estado')} — "
            f"{data.get('descripcion_anteultimo_estado')} "
            f"({data.get('fecha_anteultimo_estado')})"
        )
    lines.append(f"Тип: {data.get('tipo_tramite')} / {data.get('descripcion_tramite')}")
    lines.append(f"Доставка: {data.get('tipo_retiro')}")

    tramites = data.get("tramitesUI") or []
    for idx, ui in enumerate(tramites, 1):
        historico = ui.get("historico") or []
        if len(tramites) > 1:
            lines.append(f"\n#{idx} {ui.get('descripcion_tramite', '')}".rstrip())
        lines.append("История:")
        if historico:
            for h in historico:
                lines.append(f"  {h.get('FechaCambio')}  {h.get('EstadoMotivo')}")
        else:
            lines.append("  (пусто)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- Telegram

def tg(method: str, **params):
    response = requests.post(f"{TG_API}/{method}", json=params, timeout=HTTP_TIMEOUT + 30)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body}")
    return body["result"]


def send(chat_id, text: str):
    for start in range(0, len(text), 4000):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text=text[start : start + 4000],
            disable_web_page_preview=True,
        )


def _limits_text() -> str:
    parts = []
    if RATE_MIN_INTERVAL > 0:
        if RATE_MIN_INTERVAL % 60 == 0:
            parts.append(f"1 запрос каждые {int(RATE_MIN_INTERVAL // 60)} мин")
        else:
            parts.append(f"1 запрос каждые {int(RATE_MIN_INTERVAL)} с")
    if RATE_DAILY > 0:
        parts.append(f"до {RATE_DAILY} в сутки")
    return ("Лимит: " + ", ".join(parts) + ".") if parts else ""


HELP = (
    "Пришли номер трамите (9 цифр, как в constancia de solicitud de trámite) — "
    "верну статус из RENAPER.\n\n"
    "Пример:\n754991285\n\n"
    "Команды: /start /help /id"
) + (("\n\n" + _limits_text()) if _limits_text() else "")


_usage: dict[int, dict] = {}


def check_rate_limit(user_id: int) -> str | None:
    """None, если можно; иначе текст ответа. Слот резервируется при разрешении."""
    if RATE_MIN_INTERVAL <= 0 and RATE_DAILY <= 0:
        return None

    now = time.time()
    today = time.strftime("%Y-%m-%d")
    record = _usage.get(user_id)
    if record is None or record["date"] != today:
        if len(_usage) > 5000:  # дешёвая чистка старых записей
            for key in [k for k, v in _usage.items() if v["date"] != today]:
                _usage.pop(key, None)
        record = {"date": today, "count": 0, "last": 0.0}
        _usage[user_id] = record

    if RATE_MIN_INTERVAL > 0 and record["last"]:
        elapsed = now - record["last"]
        if elapsed < RATE_MIN_INTERVAL:
            wait = int(RATE_MIN_INTERVAL - elapsed) + 1
            return f"Подожди немного: следующая проверка через {wait} с."

    if RATE_DAILY > 0 and record["count"] >= RATE_DAILY:
        return f"Дневной лимит исчерпан ({RATE_DAILY} проверок). Попробуй завтра."

    record["count"] += 1
    record["last"] = now
    return None


def handle_message(message: dict):
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id", chat_id)
    text = (message.get("text") or "").strip()

    if ALLOWED_CHAT_IDS and str(chat_id) not in ALLOWED_CHAT_IDS:
        log.warning("сообщение из неразрешённого чата")
        send(chat_id, f"Нет доступа. Твой chat id: {chat_id}.")
        return

    if text in ("/start", "/help", ""):
        send(chat_id, HELP)
        return
    if text == "/id":
        send(chat_id, f"Твой chat id: {chat_id}")
        return

    query = text[len("/check") :].strip() if text.startswith("/check") else text
    digits = re.sub(r"\D", "", query)
    if len(digits) < 8:
        send(chat_id, "Не вижу корректного номера трамите.\n\n" + HELP)
        return

    limited = check_rate_limit(user_id)
    if limited:
        send(chat_id, limited)
        return

    send(chat_id, f"Проверяю трамите {normalize_tramite(digits)}…")
    tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        send(chat_id, consultar(digits))
    except Exception as exc:  # noqa: BLE001 - сообщаем пользователю
        log.exception("ошибка при проверке трамите")
        send(chat_id, f"ОШИБКА: {exc}")


def run_bot():
    if not BOT_TOKEN:
        print("Не задан TELEGRAM_BOT_TOKEN", file=sys.stderr)
        sys.exit(2)
    if CAPTCHA_MODE != "browser" and not TWOCAPTCHA_KEY:
        print("Не задан 2CAPTCHA_API_KEY", file=sys.stderr)
        sys.exit(2)
    me = tg("getMe")
    log.info(
        "бот @%s запущен (режим капчи: %s)", me.get("username"), CAPTCHA_MODE
    )

    refresh_recaptcha(notify=True)
    if HEALTHCHECK_INTERVAL > 0:
        threading.Thread(target=healthcheck_loop, daemon=True).start()

    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = tg("getUpdates", **params)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_message(message)
        except KeyboardInterrupt:
            log.info("выхожу")
            return
        except Exception:  # noqa: BLE001 - polling не должен умирать
            log.exception("ошибка в цикле; повтор через 5 с")
            time.sleep(5)


def setup_logging():
    if LOG_LEVEL in ("", "off", "none", "0", "false", "no"):
        logging.disable(logging.CRITICAL)
        return
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main():
    setup_logging()
    if len(sys.argv) > 1:
        print(consultar(sys.argv[1]))
        return
    try:
        run_bot()
    finally:
        BROWSER.close()


if __name__ == "__main__":
    main()
