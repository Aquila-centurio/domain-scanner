# Domain Scanner

Инструмент для анализа дропающихся доменов на аукционах Namecheap.

**Стек:** Flask · Celery · Redis · Nginx · Docker Compose · GitHub Actions

## Как работает

1. Загружаешь `.xlsx` или `.csv` экспорт из Namecheap Marketplace
2. JS фильтрует строки прямо в браузере — отправляет только подходящие (KB вместо MB)
3. Сервер фильтрует: только `.com`, зарегистрированные в 2000–2016, аукцион в заданном окне, цена в лимите
4. Каждый домен проверяется через **ScamDoc** (trust score 0–100%)
5. Прошедшие порог — через **VirusTotal** (malicious/suspicious флаги)
6. Результаты появляются в реальном времени через polling, выгрузка в `.xlsx`

Несколько пользователей могут запускать сканы одновременно — у каждого свой `task_id`.

## Архитектура

```
browser → nginx:80/443 → gunicorn:8000 → redis:6379
                                       ↓
                              celery worker (фоновый скан)
```

## Структура

```
domain-scanner/
├── app/
│   ├── app.py              # Flask роуты: /scan /status /stop /download
│   ├── tasks.py            # Celery — фильтрация, ScamDoc, VirusTotal
│   ├── config.py           # Настройки из env
│   ├── requirements.txt
│   ├── Dockerfile
│   └── templates/
│       └── index.html      # Весь фронтенд (HTML + CSS + JS)
├── nginx/
│   ├── nginx.conf          # Продакшен (SSL + Basic Auth)
│   └── nginx.dev.conf      # Dev (без SSL)
├── redis/
│   └── redis.conf          # maxmemory, persistence
├── .github/
│   └── workflows/
│       └── ci.yml          # CI/CD pipeline
├── docker-compose.yml      # Продакшен
├── docker-compose.dev.yml  # Локальная разработка
└── .env.example
```

## CI/CD

**Автоматически при каждом push в main:**

```
push → lint (flake8) → tests (pytest) → build образа → push на Docker Hub
```

**Деплой на сервер — одна кнопка:**

```
GitHub Actions → Run workflow → deploy: true
```

Pipeline сам подключается к серверу по SSH, скачивает новый образ с Docker Hub
и перезапускает контейнеры. На сервер заходить не нужно.

## Локальный запуск

```bash
cp .env.example .env
nano .env  # вписать API ключи

docker compose -f docker-compose.dev.yml up --build
# открыть http://localhost
```

## Деплой на VPS (первый раз)

```bash
# Docker
apt update && apt install -y docker.io docker-compose-plugin

# Проект
git clone <repo> /opt/domain-scanner
cd /opt/domain-scanner

cp .env.example .env
nano .env  # вписать API ключи

# Basic Auth
apt install -y apache2-utils
htpasswd -c nginx/htpasswd username

# Запуск
docker compose up -d
```

После первого запуска все последующие обновления деплоятся через GitHub Actions.

## SSL

```bash
docker compose up -d nginx

docker compose run --rm certbot certonly \
  --webroot --webroot-path=/var/www/certbot \
  -d yourdomain.com \
  --email your@email.com \
  --agree-tos

docker compose restart nginx
```

## Управление

```bash
docker compose ps                      # статус контейнеров
docker compose logs -f app             # логи приложения
docker compose logs -f celery_worker   # логи воркера
docker compose restart nginx           # перезапустить nginx
docker compose down                    # остановить всё
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `REDIS_URL` | `redis://redis:6379/0` |
| `RAPIDAPI_KEY` | Ключ RapidAPI (ScamDoc) |
| `VT_API_KEY` | Ключ VirusTotal |

## Ограничения API

| API | Лимит | Пауза между запросами |
|-----|-------|-----------------------|
| ScamDoc | ~40 req/min | 1.5с |
| VirusTotal free | 4 req/min | 16с |