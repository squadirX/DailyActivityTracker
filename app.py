from flask import Flask, jsonify, render_template
from config import HOST, PORT, REFRESH_SECONDS, EXCEL_FILE
from excel_reader import read_applications
from analytics import build_metrics
from job_news_feed import get_job_news

from resume_library import resume_bp
from certificates_library import certificates_bp
from profile_photo import profile_photo_bp

app = Flask(__name__)
app.register_blueprint(resume_bp)
app.register_blueprint(certificates_bp)
app.register_blueprint(profile_photo_bp)
app.config["TEMPLATES_AUTO_RELOAD"] = True

@app.get("/")
def index():
    return render_template("dashboard.html", refresh_seconds=REFRESH_SECONDS)

@app.get("/api/dashboard")
def dashboard_data():
    data = read_applications()
    if not data["available"]:
        return jsonify({"ok": False, "message": data["message"], "path": str(EXCEL_FILE), "metrics": {}, "headers": [], "rows": []})
    return jsonify({
        "ok": True,
        "message": data["message"],
        "modified": data["modified"],
        "metrics": build_metrics(data["rows"]),
        "headers": data["headers"],
        "rows": data["rows"],
    })

@app.get("/api/job-news")
def job_news_data():
    return jsonify(get_job_news())


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
