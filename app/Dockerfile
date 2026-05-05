# Базовый образ — slim чтобы не тащить лишнее
FROM python:3.12-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Сначала копируем только requirements — слой кешируется
# и не пересобирается при изменении кода
COPY requirements.txt .

# Устанавливаем зависимости
# --no-cache-dir — не сохранять кеш pip внутри образа (меньше размер)
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код приложения
COPY . .

# Порт на котором будет слушать Gunicorn
EXPOSE 8000

# Запуск Gunicorn:
# --workers 4       — 4 воркера (state теперь в Redis, не в памяти)
# --timeout 120     — таймаут воркера (сканы долгие)
# --bind 0.0.0.0    — слушаем на всех интерфейсах контейнера
# app:app           — файл app.py, объект Flask называется app
CMD ["gunicorn", "--workers", "4", "--timeout", "120", "--bind", "0.0.0.0:8000", "app:app"]