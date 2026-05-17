import os
from unittest.mock import patch, MagicMock
import sys

# Мокаем зависимости ДО импорта tasks
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("RAPIDAPI_KEY", "fake")
os.environ.setdefault("VT_API_KEY", "fake")

# redis и celery заменяем фейками — не нужны реальные сервисы
sys.modules["redis"] = MagicMock()
sys.modules["celery"] = MagicMock()

from tasks import check_scamdoc


def nolog(*a, **kw):
    """Фейковый логгер — игнорирует все вызовы."""
    pass


def mock_response(status=200, json_data=None):
    """
    Создать фейковый HTTP ответ.
    check_scamdoc делает http_requests.get() — мы подменяем его
    этим объектом чтобы не делать реальных запросов к API.
    """
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    return resp


def test_risk_015_gives_85():
    """
    ScamDoc возвращает final_score=0.15 (риск 15%).
    Конвертация: trust = (1 - 0.15) * 100 = 85.
    """
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"final_score": 0.15})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "example.com") == 85


def test_risk_0_gives_100():
    """
    final_score=0 — домен абсолютно безопасный.
    trust = (1 - 0) * 100 = 100.
    """
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"final_score": 0.0})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "safe.com") == 100


def test_risk_1_gives_0():
    """
    final_score=1 — домен максимально опасный.
    trust = (1 - 1) * 100 = 0.
    """
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"final_score": 1.0})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "danger.com") == 0


def test_no_final_score_returns_none():
    """
    API вернул 200 но без поля final_score.
    Функция должна вернуть None — непонятный ответ.
    """
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"other": 123})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "example.com") is None


def test_http_500_returns_none():
    """
    Сервер вернул 500 — ошибка на стороне API.
    Функция должна вернуть None после всех попыток retry.
    """
    with patch("tasks.http_requests.get",
               return_value=mock_response(500)), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "example.com") is None