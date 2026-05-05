"""
config.py — все настройки в одном месте.
В продакшене API ключи лучше передавать через environment variables,
а не хардкодить в коде. Здесь оставлены оригинальные ключи клиента.
"""

import os

# Redis — читаем из env (docker-compose задаёт REDIS_URL)
# Fallback для локального запуска без Docker
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# API ключи
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "db6d4987ecmshd4a7aa2c1fc0ca7p1b4c58jsn7cc388f1d736")
VT_API_KEY = os.getenv("VT_API_KEY", "0be1f79cdcaf7a46a9c9c86e4007ff64aad993d5b5a89897d795bec0934f32be")