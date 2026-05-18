import os
import sys
from unittest.mock import patch, MagicMock

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("RAPIDAPI_KEY", "fake")
os.environ.setdefault("VT_API_KEY", "fake")

sys.modules["redis"] = MagicMock()
sys.modules["celery"] = MagicMock()

from tasks import check_scamdoc  # noqa: E402


def nolog(*a, **kw):
    """Фейковый логгер — игнорирует все вызовы."""
    pass


def mock_response(status=200, json_data=None):
    """Создать фейковый HTTP ответ."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    return resp


def test_risk_015_gives_85():
    """final_score=0.15 → trust = (1 - 0.15) * 100 = 85."""
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"final_score": 0.15})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "example.com") == 85


def test_risk_0_gives_100():
    """final_score=0 — домен безопасный → trust = 100."""
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"final_score": 0.0})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "safe.com") == 100


def test_risk_1_gives_0():
    """final_score=1 — домен опасный → trust = 0."""
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"final_score": 1.0})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "danger.com") == 0


def test_no_final_score_returns_none():
    """API вернул 200 но без final_score → None."""
    with patch("tasks.http_requests.get",
               return_value=mock_response(200, {"other": 123})), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "example.com") is None


def test_http_500_returns_none():
    """HTTP 500 → None после всех попыток retry."""
    with patch("tasks.http_requests.get",
               return_value=mock_response(500)), \
         patch("tasks.log", nolog):
        assert check_scamdoc("t", "example.com") is None