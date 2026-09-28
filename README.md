# mitramite

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Телеграм-бот, который по номеру трамите отдаёт его статус из RENAPER
(`mitramite.renaper.gob.ar`). Работает так же, как ручной скрипт из консоли
браузера, только без человека.

## Как это работает

Сайт RENAPER сломан (форму ввода убрали), но бэкенд `POST /busqueda.php` жив.
Он требует токен reCAPTCHA v3 **с привязкой к action `submit_tramite`**, а site
key привязан к домену `mitramite.renaper.gob.ar` — поэтому токен нельзя выписать
ни серверным обходом, ни с чужого домена. Бот решает капчу через **2captcha**
(она умеет reCAPTCHA v3 с `action`), а затем сам делает POST в RENAPER:

1. `GET /in.php` → 2captcha решает капчу и возвращает id задачи.
2. `GET /res.php` → опрашиваем, пока не вернётся токен.
3. `POST https://mitramite.renaper.gob.ar/busqueda.php` с `tramite`, `token`,
   `action=submit_tramite`.

Браузер не нужен: зависимость только `requests`, образ ~156 МБ, RAM ~50 МБ.

**Site key и action не захардкожены**: при старте бот вычитывает их прямо с сайта
(`api.js?render=<key>` в HTML и `grecaptcha.execute(...)` в JS) и раз в
`HEALTHCHECK_INTERVAL` перепроверяет. Если RENAPER поменяет ключ, action или
перейдёт на reCAPTCHA Enterprise — бот подхватит это сам и пришлёт уведомление
в `ALLOWED_CHAT_IDS`. Значения из `RENAPER_SITE_KEY`/`RECAPTCHA_ACTION` —
только резерв, если сайт недоступен.

Есть и альтернативный режим `CAPTCHA_MODE=browser` — headless Chromium через
Playwright (см. `Dockerfile.browser`), без 2captcha, но тяжелее (~815 МБ, ~144 МБ
RAM) и требует доступ к сайту RENAPER напрямую.

## Что нужно

- Python 3.9+ **или** Docker.
- Токен бота от [@BotFather](https://t.me/BotFather).
- API key 2captcha с положительным балансом ([2captcha.com](https://2captcha.com)).
  Решение стоит примерно $1–3 за 1000 запросов.
- Сеть, из которой доступны `2captcha.com` и `mitramite.renaper.gob.ar`.

## Запуск локально (macOS/Linux)

```bash
cd mitramite
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN='123456:ABC...'
env '2CAPTCHA_API_KEY=ваш_ключ' .venv/bin/python bot.py
```

Проверить без Телеграма (разовая консультация):

```bash
env '2CAPTCHA_API_KEY=ваш_ключ' .venv/bin/python bot.py 754991285
```

> В zsh/bash нельзя написать `export 2CAPTCHA_API_KEY=...` — имя начинается с
> цифры. Используй `env '2CAPTCHA_API_KEY=...'` (или алиас `TWOCAPTCHA_API_KEY`).
> В `.env` для Docker такой проблемы нет.

В Телеграме: `/start`, затем просто шли 9-значный номер трамите.
Команда `/id` покажет chat id.

## Запуск в Docker

Образ ~156 МБ, работает без состояния:

```bash
cp .env.example .env   # вписать TELEGRAM_BOT_TOKEN и 2CAPTCHA_API_KEY
docker compose up -d --build
```

Или без compose:

```bash
docker build -t mitramite .
docker run -d --name mitramite --restart unless-stopped --memory=128m \
  --read-only --tmpfs /tmp --log-driver none \
  -e TELEGRAM_BOT_TOKEN='123456:ABC...' \
  -e '2CAPTCHA_API_KEY=ваш_ключ' \
  -e ALLOWED_CHAT_IDS='111111111' \
  mitramite
```

## Логи и состояние

Бот **stateless**: никакой БД, файлов и истории. Единственное состояние —
`offset` long-polling в памяти (теряется при рестарте, это нормально).

Логи выключены на обоих уровнях:

- в compose `logging.driver: "none"` — контейнерные логи не сохраняются,
  `docker logs` вернёт `configured logging driver does not support reading`;
- в боте `LOG_LEVEL=off` по умолчанию — ничего не пишется в stdout/stderr,
  номер трамите и chat id в логи не попадают.

Чтобы разово отладить: убери `logging` из compose (или запусти без
`--log-driver none`) и поставь `LOG_LEVEL=INFO` в `.env`. Тогда в логах будут
технические события, но **без номера трамите**.

## Лимиты

На каждого пользователя: не чаще одного запроса раз в `RATE_LIMIT_MIN_INTERVAL`
(180 с) и не больше `RATE_LIMIT_DAILY` (50) в сутки. В группе ключ — `from.id`
отправителя, в личке — `chat.id`, то есть лимит общий для пользователя во всех
чатах. `/start`, `/help`, `/id` и мусорные сообщения не считаются. Отклонённый
запрос не тратит 2captcha и не занимает дневной слот.

Счётчики живут **в памяти** (бот stateless, ФС read-only) — при рестарте
контейнера обнуляются. Если нужны лимиты, переживающие рестарт, придётся
добавить volume/БД, что ломает stateless-подход.

## Запуск на VPS (systemd, автозапуск)

```bash
sudo mkdir -p /opt/mitramite
sudo cp bot.py requirements.txt /opt/mitramite/
sudo python3 -m venv /opt/mitramite/.venv
sudo /opt/mitramite/.venv/bin/pip install -r /opt/mitramite/requirements.txt
```

`/etc/systemd/system/mitramite.service`:

```ini
[Unit]
Description=mitramite Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/mitramite
Environment=TELEGRAM_BOT_TOKEN=123456:ABC...
Environment=2CAPTCHA_API_KEY=ваш_ключ
Environment=ALLOWED_CHAT_IDS=111111111
ExecStart=/opt/mitramite/.venv/bin/python /opt/mitramite/bot.py
Restart=always
RestartSec=5
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mitramite
sudo systemctl status mitramite
journalctl -u mitramite -f
```

## Минимальное железо

| Ресурс | Значение |
| ------ | -------- |
| RAM    | ~50 МБ (влезает в 128 МБ) |
| CPU    | 1 vCPU, запрос ~5–20 с (зависит от 2captcha) |
| Диск   | ~156 МБ образ (или ~200 МБ venv) |
| Сеть   | нужен доступ к `2captcha.com` и `mitramite.renaper.gob.ar` |

Подойдёт самый дешёвый VPS (128–256 МБ). Геоблок RENAPER не важен — капчу
решает сеть 2captcha, но сам POST в `busqueda.php` идёт с твоего хоста, поэтому
доступ к сайту RENAPER всё же нужен.

## Переменные окружения

| Переменная            | Обяз. | Значение по умолчанию |
| --------------------- | :---: | --------------------- |
| `TELEGRAM_BOT_TOKEN`  |  да   | — (алиас: `MITRAMITE_TOKEN`) |
| `2CAPTCHA_API_KEY`    |  да*  | — (алиас: `TWOCAPTCHA_API_KEY`) |
| `ALLOWED_CHAT_IDS`    |  нет  | пусто = бот открыт всем |
| `LOG_LEVEL`           |  нет  | `off` (логи выключены) |
| `HEALTHCHECK_INTERVAL`|  нет  | `86400` (сек; 0 = выключить ре-проверку) |
| `RECAPTCHA_ENTERPRISE`|  нет  | `0` (обычно определяется автоматически) |
| `RATE_LIMIT_MIN_INTERVAL`| нет | `180` (сек между запросами одного пользователя; 0 = без лимита) |
| `RATE_LIMIT_DAILY`    |  нет  | `50` (запросов на пользователя в сутки; 0 = без лимита) |
| `CAPTCHA_MODE`        |  нет  | `2captcha` (или `browser`) |
| `TWOCAPTCHA_BASE`     |  нет  | `https://2captcha.com` |
| `TWOCAPTCHA_MIN_SCORE`|  нет  | `0.3` |
| `TWOCAPTCHA_POLL`     |  нет  | `5` (сек между опросами) |
| `TWOCAPTCHA_TIMEOUT`  |  нет  | `180` (сек на решение) |
| `RENAPER_BASE`        |  нет  | `https://mitramite.renaper.gob.ar` |
| `RENAPER_SITE_KEY`    |  нет  | site key reCAPTCHA v3 сайта |
| `RECAPTCHA_ACTION`    |  нет  | `submit_tramite` |
| `HTTP_TIMEOUT`        |  нет  | `25` (сек) |

\* не нужен в режиме `CAPTCHA_MODE=browser`.

`requests` сам подхватывает `HTTP_PROXY` / `HTTPS_PROXY`, если нужен прокси.

## Режим browser (без 2captcha)

```bash
docker build -f Dockerfile.browser -t mitramite:browser .
docker run -d --name mitramite --restart unless-stopped --memory=256m --shm-size=64m \
  -e CAPTCHA_MODE=browser \
  -e TELEGRAM_BOT_TOKEN='123456:ABC...' \
  mitramite:browser
```

## Как узнать свой chat id

Запусти бота и отправь `/id` — он ответит числом. Впиши его в
`ALLOWED_CHAT_IDS`, чтобы ботом не пользовались посторонние.

## Диагностика

- `2captcha: ERROR_WRONG_USER_KEY` — неверный ключ.
- `2captcha: ERROR_ZERO_BALANCE` — пополни баланс на 2captcha.
- `2captcha: таймаут ожидания токена` — сервис не успел; увеличь
  `TWOCAPTCHA_TIMEOUT` или попробуй позже.
- `RENAPER: {"title": "Error reCAPTCHA..."}` — токен отклонён (низкий score).
  Попробуй поднять `TWOCAPTCHA_MIN_SCORE` (например, `0.7`).
- `RENAPER вернул не-JSON` — нет доступа к сайту (файрвол,
  старые CA на хосте). В контейнере CA свежие, там проблемы быть не должно.
- `Не вижу корректного номера трамите` — пришло не число.

## Безопасность

- Токен и `.env` не коммитить (`.gitignore` уже настроен).
- Публичный бот тратит деньги на 2captcha за любого, кто его найдёт — обязательно
  задай `ALLOWED_CHAT_IDS`.
- Бот stateless: логи выключены, FS только для чтения, лишние capabilities
  сброшены (`cap_drop: ALL`, `no-new-privileges`), входящие порты не слушаются.

## Лицензия

MIT — см. [LICENSE](LICENSE).
