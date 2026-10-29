# Gym Bro: полная инструкция по запуску

Порядок: получить токен и ключи → проверить на своём компьютере → вынести на сервер → обслуживать.
Первые два шага обязательны. Сервер нужен, только чтобы бот работал, когда твой компьютер выключен.

---

## 1. Что понадобится

| Что | Где взять | Цена |
|---|---|---|
| Токен бота (`BOT_TOKEN`) | Telegram, [@BotFather](https://t.me/BotFather) | бесплатно |
| Ключ Gemini (`GEMINI_API_KEY`) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | бесплатный тариф, лимиты смотри в AI Studio |
| Ключ Groq (`GROQ_API_KEY`) | [console.groq.com/keys](https://console.groq.com/keys) | бесплатный тариф |
| Твой Telegram ID | Telegram, [@userinfobot](https://t.me/userinfobot) | бесплатно |
| Сервер (для постоянной работы) | любой VPS с Linux, 512 МБ ОЗУ хватает | от ~3–5 $/мес |

Без ключей бот работает, кроме ИИ-чата, голоса и разбора фото. Тренировки, напоминания и добавки не зависят от ключей.

## 2. Токен бота

1. Открой [@BotFather](https://t.me/BotFather), отправь `/newbot`.
2. Имя: любое («Gym Bro»). Юзернейм: уникальный, на `bot` в конце (`my_gym_bro_bot`).
3. BotFather пришлёт токен вида `123456789:AA...`. Это пароль от бота: не публикуй его, не отправляй в чаты, не коммить в git.
4. Утёк токен — в BotFather: `/revoke`, и обнови `.env`.

## 3. Ключи ИИ

**Gemini.** AI Studio → «Get API key» → создать ключ. Модель по умолчанию `gemini-3.8-flash`. Если в AI Studio она недоступна на бесплатном тарифе или упёрся в лимит, бот сам переключится на Groq. Другую модель можно задать в `.env` (`GEMINI_MODEL`).

**Groq.** Console → API Keys → Create. Он нужен для голоса (Whisper) и как запасной чат и разбор фото.

## 4. Запуск на своём компьютере (проверка)

Нужны `git` и [`uv`](https://docs.astral.sh/uv/) (менеджер Python-пакетов). Установка uv на Arch/CachyOS:

```bash
sudo pacman -S uv
```

Дальше:

```bash
git clone https://github.com/scorostinncostja-lang/Gym-Bro.git
cd Gym-Bro
cp .env.example .env
nano .env
```

В `.env` впиши:

```
BOT_TOKEN=токен_от_BotFather
GEMINI_API_KEY=ключ_gemini
GROQ_API_KEY=ключ_groq
ALLOWED_USER_IDS=твой_telegram_id
```

`ALLOWED_USER_IDS` держи заполненным, пока тестируешь: чужие люди не смогут тратить твои лимиты. Несколько человек: `111,222,333`.

Запуск:

```bash
uv sync
uv run python -m bot
```

В логе должна появиться строка «Бот запущен. ИИ: gemini=True groq=True». Остановить: `Ctrl+C`.

**Чек-лист проверки (5 минут):**
1. В Telegram открой своего бота, `/start`, пройди анкету.
2. `/today` → «Начать». Должна прийти карточка упражнения с GIF.
3. Напиши `40 10`, нажми «нормально». Бот даст подсказку на следующий подход.
4. Напиши вопрос «как не болели колени после приседа?»: ответит ИИ.
5. Отправь голосовое «двадцать на двенадцать»: бот расшифрует.
6. Отправь фото еды: бот спросит «Еда или техника».
7. `/supplements`: список добавок с вердиктами; спроси «сколько креатина?» и проверь, что бот называет малую дозу.
8. `/measure` → «Фото или кружок»: пришли 4 фото или кружок, затем `/progress` покажет их.
9. `/settings`: поставь время на ближайшие 2–3 минуты и проверь, что пришло напоминание.

Если что-то не работает, смотри раздел 8.

## 5. Деплой на сервер

Вариант А: systemd (проще и легче по памяти). Вариант Б: Docker.

Бот работает через polling: входящие порты открывать не нужно, домен и HTTPS не нужны.

### Вариант А: systemd (Debian/Ubuntu)

```bash
# 1. Подключись и поставь инструменты
ssh root@IP_СЕРВЕРА
apt update && apt install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# 2. Отдельный пользователь без прав администратора
useradd --system --create-home --home-dir /opt/gym-bro --shell /usr/sbin/nologin gymbro

# 3. Код
git clone https://github.com/scorostinncostja-lang/Gym-Bro.git /opt/gym-bro
cd /opt/gym-bro
cp .env.example .env && nano .env        # впиши токен и ключи
chmod 600 .env
mkdir -p var
# Python кладём внутрь /opt/gym-bro, иначе пользователь gymbro не доберётся до /root/.local
UV_PYTHON_INSTALL_DIR=/opt/gym-bro/.python uv sync --no-dev
chown -R gymbro:gymbro /opt/gym-bro

# 4. Сервис
cp deploy/gymbro.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gymbro

# 5. Проверка
systemctl status gymbro
journalctl -u gymbro -f
```

Сервис запускает `/opt/gym-bro/.venv/bin/python`, сам `uv` ему не нужен.

### Вариант Б: Docker

```bash
git clone https://github.com/scorostinncostja-lang/Gym-Bro.git && cd Gym-Bro
cp .env.example .env && nano .env && chmod 600 .env
docker build -t gym-bro .
docker run -d --name gym-bro --restart=always --env-file .env -v gymbro-data:/app/var gym-bro
docker logs -f gym-bro
```

Том `gymbro-data` хранит базу SQLite: без него данные пропадут при пересоздании контейнера.

## 6. Обновление бота

systemd:

```bash
cd /opt/gym-bro
git pull
UV_PYTHON_INSTALL_DIR=/opt/gym-bro/.python uv sync --no-dev
chown -R gymbro:gymbro /opt/gym-bro
systemctl restart gymbro
```

Docker:

```bash
cd Gym-Bro && git pull
docker build -t gym-bro .
docker rm -f gym-bro
docker run -d --name gym-bro --restart=always --env-file .env -v gymbro-data:/app/var gym-bro
```

## 7. Бэкап и приватность

- Все данные пользователей лежат в одном файле `var/gymbro.db`. Это единственное, что надо бэкапить:
  ```bash
  cp /opt/gym-bro/var/gymbro.db ~/gymbro-$(date +%F).db
  ```
- Индекс упражнений кэшируется в `var/cache/` и восстанавливается сам.
- Бот хранит данные о подростках (возраст, вес, замеры). Держи `.env` и `var/` закрытыми (`chmod 600`), сервер обновлённым, SSH по ключу.
- `/delete_me` стирает все данные пользователя, `/privacy` объясняет, что уходит наружу.
- Если пустишь в бота чужих людей, прочитай раздел «Лицензии» в `README.md`.

## 8. Если не работает

| Симптом | Причина и решение |
|---|---|
| «Не хватает настроек в .env» | Нет `.env` или пустой `BOT_TOKEN`. `cp .env.example .env` и заполни |
| «Telegram отклонил BOT_TOKEN» | Токен неверный или отозван: возьми новый у BotFather |
| Бот молчит | Идёт второй экземпляр с тем же токеном (ошибка `Conflict: terminated by other getUpdates`). Останови лишний процесс или контейнер |
| «Бот пока закрытый» | Твой ID нет в `ALLOWED_USER_IDS`. Добавь или оставь переменную пустой |
| «ИИ сейчас недоступен» | Смотри лог: `journalctl -u gymbro -n 50`. Частые причины: неверный ключ, исчерпан лимит, неверное имя модели в `GEMINI_MODEL` |
| Нет GIF, только картинки | Нет сети до GitHub при первом запуске и нет кэша. Проверь интернет на сервере и перезапусти |
| Напоминания приходят в другое время | Неверный часовой пояс: `/settings` → часовой пояс |
| Не приходит голосовое | Нет `GROQ_API_KEY` или голосовое длиннее 60 секунд |

Лимиты на человека в сутки задаются в `.env`: `LIMIT_LLM_PER_DAY`, `LIMIT_STT_PER_DAY`, `LIMIT_VISION_PER_DAY`.

## 9. Быстрые команды

```bash
uv run pytest                    # тесты
journalctl -u gymbro -f          # логи (systemd)
systemctl restart gymbro         # перезапуск
docker logs -f gym-bro           # логи (Docker)
```
