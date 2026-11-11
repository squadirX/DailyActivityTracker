from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

profile_photo_bp = Blueprint("profile_photo", __name__)

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "static" / "profile"
PROFILE_FILE = PROFILE_DIR / "profile.jpg"

MAX_PHOTO_SIZE = 5 * 1024 * 1024


@profile_photo_bp.get("/profile-photo")
def get_profile_photo():
    if not PROFILE_FILE.is_file():
        return jsonify({"ok": False, "message": "Profile photo not found"}), 404

    return send_file(
        PROFILE_FILE,
        mimetype="image/jpeg",
        conditional=True,
        max_age=0
    )


@profile_photo_bp.post("/api/profile-photo")
def upload_profile_photo():
    uploaded = request.files.get("photo")

    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "message": "Select a JPG photo"}), 400

    if not uploaded.filename.lower().endswith((".jpg", ".jpeg")):
        return jsonify({"ok": False, "message": "Only JPG/JPEG images are accepted"}), 400

    data = uploaded.stream.read(MAX_PHOTO_SIZE + 1)

    if not data:
        return jsonify({"ok": False, "message": "The selected file is empty"}), 400

    if len(data) > MAX_PHOTO_SIZE:
        return jsonify({"ok": False, "message": "Maximum photo size is 5 MB"}), 413

    if not data.startswith(b"\xff\xd8\xff"):
        return jsonify({"ok": False, "message": "Invalid JPEG image"}), 400

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    temporary_file = PROFILE_DIR / "profile.jpg.tmp"

    try:
        temporary_file.write_bytes(data)
        temporary_file.replace(PROFILE_FILE)
    finally:
        if temporary_file.exists():
            temporary_file.unlink()

    return jsonify({"ok": True, "message": "Profile photo updated"}), 200
