import os
import csv
import io
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import sys
import tempfile

# Мокаем внешние зависимости ДО импорта tasks
# Иначе tasks.py попытается подключиться к реальному Redis при импорте
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("RAPIDAPI_KEY", "fake")
os.environ.setdefault("VT_API_KEY", "fake")

# Заменяем модули redis и celery фейковыми объектами
# чтобы не нужно было реальных сервисов для тестов
sys.modules["redis"] = MagicMock()
sys.modules["celery"] = MagicMock()

from tasks import load_and_filter


# ── Хелперы ─────────────────────────────────────────────────


def make_row(domain="example.com", hours_from_now=12,
             price=10.0, reg_year=2010) -> list:
    """
    Создать строку CSV с нужными параметрами.
    Индексы колонок соответствуют формату Namecheap:
      0  = url
      1  = domain
      3  = auction end datetime
      4  = price
      7  = bid_count
      8  = ahrefs_dr
      14 = registration date
      20 = backlinks
      23 = majestic_tf
    """
    now = datetime.now(timezone.utc)
    end_dt = now + timedelta(hours=hours_from_now)
    row = [""] * 25
    row[0] = f"https://namecheap.com/marketplace/{domain}"
    row[1] = domain
    row[3] = end_dt.isoformat()
    row[4] = str(price)
    row[7] = "0"
    row[8] = "0"
    row[14] = f"{reg_year}-01-01"
    row[20] = "0"
    row[23] = "0"
    return row


def make_mocks():
    """
    Создать фейковые функции для работы с Redis.
    load_and_filter вызывает get_state/set_state/update_state/log —
    все они замокаем чтобы не нужен был реальный Redis.
    """
    state = {}

    def get_state(tid): return state.get(tid, {})
    def set_state(tid, s): state[tid] = s
    def update_state(tid, **kw):
        s = get_state(tid)
        s.update(kw)
        set_state(tid, s)
    def log_fn(tid, msg, level="info"): pass  # логи игнорируем в тестах

    return get_state, set_state, update_state, log_fn


def run_filter(rows, min_hours=6, max_hours=24, max_price=0):
    """
    Хелпер — создать временный CSV файл на диске и запустить load_and_filter.
    app/tasks.py читает файл с диска через parse_rows_streaming,
    поэтому нужен реальный путь, не bytes в памяти.
    """
    # Создаём временный файл — он будет удалён после выхода из блока with
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        writer = csv.writer(f)
        writer.writerow(["url"] * 25)  # заголовок
        for row in rows:
            writer.writerow(row)
        tmp_path = f.name

    gs, ss, us, lf = make_mocks()
    try:
        with patch("tasks.get_state", gs), \
             patch("tasks.set_state", ss), \
             patch("tasks.update_state", us), \
             patch("tasks.log", lf):
            return load_and_filter(
                "test", tmp_path, "test.csv",
                min_hours, max_hours, max_price
            )
    finally:
        # Удалить временный файл в любом случае — даже если тест упал
        os.unlink(tmp_path)

# ── Тесты ───────────────────────────────────────────────────

def test_valid_domain_passes():
    """Валидный домен — .com, год 2010, в окне 12ч, цена 10 — должен пройти."""
    rows = [make_row("good.com", hours_from_now=12, price=10, reg_year=2010)]
    result = run_filter(rows)
    assert len(result) == 1
    assert result[0]["domain"] == "good.com"


def test_filter_non_com():
    """Домен не .com — должен быть отфильтрован."""
    rows = [make_row("bad.net")]
    assert run_filter(rows) == []


def test_filter_year_before_2000():
    """Год регистрации до 2000 — не проходит фильтр."""
    rows = [make_row("old.com", reg_year=1999)]
    assert run_filter(rows) == []


def test_filter_year_after_2016():
    """Год регистрации после 2016 — не проходит фильтр."""
    rows = [make_row("new.com", reg_year=2020)]
    assert run_filter(rows) == []


def test_filter_outside_auction_window():
    """
    Аукцион заканчивается через 48ч при max_hours=24 — не проходит.
    Домен уже вне временного окна которое нас интересует.
    """
    rows = [make_row("late.com", hours_from_now=48)]
    assert run_filter(rows, max_hours=24) == []


def test_filter_price_too_high():
    """Цена 50 при max_price=30 — не проходит фильтр по цене."""
    rows = [make_row("expensive.com", price=50)]
    assert run_filter(rows, max_price=30) == []


def test_zero_price_means_no_limit():
    """
    max_price=0 означает без ограничения по цене.
    Любая цена проходит.
    """
    rows = [make_row("any.com", price=9999)]
    assert len(run_filter(rows, max_price=0)) == 1


def test_sorted_by_hours_left():
    """
    Результаты должны быть отсортированы по hours_left по возрастанию —
    сначала те что скоро заканчиваются.
    """
    rows = [
        make_row("late.com", hours_from_now=20),
        make_row("early.com", hours_from_now=8),
        make_row("mid.com", hours_from_now=14),
    ]
    result = run_filter(rows)
    assert result[0]["domain"] == "early.com"
    assert result[-1]["domain"] == "late.com"


def test_multiple_domains_correct_count():
    """
    Из 4 доменов проходят только 2 валидных .com с правильным годом.
    bad.net и old.com (1998) должны быть отфильтрованы.
    """
    rows = [
        make_row("first.com", hours_from_now=10, reg_year=2005),
        make_row("second.com", hours_from_now=15, reg_year=2012),
        make_row("bad.net"),           # не .com
        make_row("old.com", reg_year=1998),  # слишком старый
    ]
    assert len(run_filter(rows)) == 2