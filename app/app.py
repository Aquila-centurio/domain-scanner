import openpyxl
import io
import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, current_app
from werkzeug.utils import secure_filename

from tasks import get_state, run_scan, update_state

app = Flask(__name__)

# ====================== НАСТРОЙКИ ======================
UPLOAD_DIR = "/tmp/scanner_uploads"
ALLOWED_EXTENSIONS = {'.csv', '.tsv', '.xlsx'}

# Создаём папку при старте
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ====================== УТИЛИТЫ ======================
def allowed_file(filename: str) -> bool:
    """Проверка разрешённых расширений"""
    if not filename:
        return False
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


# ====================== РОУТЫ ======================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scan", methods=["POST"])
def start_scan():
    """Принимает файл и параметры, сохраняет файл на диск и ставит задачу в Celery."""
    if 'file' not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"File type not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    secure_name = secure_filename(file.filename)
    task_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{task_id}_{secure_name}")

    try:
        file.save(file_path)
        current_app.logger.info(
            f"File saved: {file_path} ({os.path.getsize(file_path) / (1024 * 1024):.1f} MB)"
        )
    except Exception as exc:
        current_app.logger.error(f"Failed to save file: {exc}")
        return jsonify({"error": "Failed to save uploaded file"}), 500

    try:
        min_hours = float(request.form.get("min_hours", 6))
        max_hours = float(request.form.get("max_hours", 24))
        limit = int(request.form.get("limit", 50))
        min_sd_score = int(request.form.get("min_sd_score", 80))
        max_sd_score = int(request.form.get("max_sd_score", 100))
        max_price = float(request.form.get("max_price", 0))

        if min_hours >= max_hours:
            return jsonify({"error": "min_hours must be less than max_hours"}), 400
        if min_sd_score > max_sd_score:
            return jsonify({"error": "min_sd_score must be <= max_sd_score"}), 400

        run_scan.delay(
            task_id=task_id,
            file_path=file_path,
            filename=secure_name,
            min_hours=min_hours,
            max_hours=max_hours,
            limit=limit,
            min_sd_score=min_sd_score,
            max_sd_score=max_sd_score,
            max_price=max_price,
        )

        return jsonify({
            "status": "started",
            "task_id": task_id,
            "message": "File received and scan started"
        }), 202

    except ValueError:
        return jsonify({"error": "Invalid parameter value"}), 400
    except Exception as exc:
        current_app.logger.error(f"Error starting scan: {exc}")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/status")
def status():
    """Возвращает текущее состояние задачи."""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    state = get_state(task_id)
    if not state:
        return jsonify({"error": "Task not found"}), 404

    return jsonify({
        "running": state.get("running", False),
        "step": state.get("step", ""),
        "progress": state.get("progress", 0),
        "total": state.get("total", 0),
        "logs": state.get("logs", [])[-50:],
        "results_great": state.get("results_great", []),
        "results_good": state.get("results_good", []),
        "flagged": state.get("flagged", []),
        "stats": state.get("stats", {}),
    })


@app.route("/stop", methods=["POST"])
def stop_scan():
    """Мягкая остановка сканирования."""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    update_state(task_id, running=False)
    return jsonify({"status": "stopping"})


@app.route("/download")
def download_xlsx():
    """Скачивание результатов в Excel."""
    task_id = request.args.get("task_id")
    if not task_id:
        return "task_id is required", 400

    state = get_state(task_id)
    if not state:
        return "Task not found", 404

    results_great = state.get("results_great", [])
    results_good = state.get("results_good", [])

    if not results_great and not results_good:
        return "No results available", 404

    fields = [
        "domain", "scamdoc_score", "hours_left", "end_date", "price",
        "bid_count", "reg_date", "ahrefs_dr", "backlinks",
        "vt_malicious", "vt_suspicious", "url",
    ]

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Great Domains"
    ws1.append(fields)

    for d in sorted(results_great, key=lambda x: x.get("scamdoc_score", 0), reverse=True):
        ws1.append([d.get(f, "") for f in fields])

    ws2 = wb.create_sheet("Good Domains")
    ws2.append(fields)
    for d in sorted(results_good, key=lambda x: x.get("scamdoc_score", 0), reverse=True):
        ws2.append([d.get(f, "") for f in fields])

    mem = io.BytesIO()
    wb.save(mem)
    mem.seek(0)

    return send_file(
        mem,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"domain_results_{task_id[:8]}.xlsx",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)