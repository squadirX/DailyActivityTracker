from __future__ import annotations

import html
import hashlib
import io
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request, send_file, abort

try:
    from config import PROJECT_DIR
except Exception:
    PROJECT_DIR = Path(__file__).resolve().parent

resume_bp = Blueprint("resume_library", __name__)
ALLOWED = {".pdf", ".doc", ".docx"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_REQUEST_BYTES = 100 * 1024 * 1024
ROOT = Path(os.environ.get("RESUME_LIBRARY_ROOT", Path(PROJECT_DIR) / "Data" / "Resumes")).expanduser().resolve()
FILES_DIR = ROOT / "files"
DB_PATH = ROOT / "resume_library.sqlite3"
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS resumes (
        id TEXT PRIMARY KEY,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL UNIQUE,
        extension TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        uploaded_at TEXT NOT NULL
    )""")
    _ensure_hash_column(con, "resumes")
    con.commit()
    return con


def _read_upload(file_storage):
    original = (file_storage.filename or "").strip()
    ext = Path(original.replace("\\", "/")).suffix.lower()
    if not original or ext not in ALLOWED:
        raise ValueError(f"Unsupported file type: {original or '(unnamed)'}")
    data = file_storage.read(MAX_FILE_BYTES + 1)
    if not data:
        raise ValueError(f"Empty file: {original}")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"File exceeds 25 MB: {original}")
    if ext == ".pdf" and not data.startswith(b"%PDF-"):
        raise ValueError(f"File content is not a valid PDF: {original}")
    if ext == ".doc" and not data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise ValueError(f"File content is not a valid DOC document: {original}")
    if ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ValueError
                if zf.testzip() is not None:
                    raise ValueError
        except Exception:
            raise ValueError(f"File content is not a valid DOCX document: {original}")
    return original, ext, data


def _docx_text(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.replace("\n", " ") for cell in row.cells))
    return "\n\n".join(parts) or "(This document contains no readable text.)"


def _doc_text(path: Path) -> str:
    executable = "/usr/bin/textutil"
    if not Path(executable).exists():
        return "DOC preview is unavailable because macOS textutil was not found. The original file remains available to download."
    try:
        result = subprocess.run([executable, "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True, timeout=25, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return "DOC preview could not be generated on this system. The original file remains available to download."
    except Exception:
        return "DOC preview could not be generated on this system. The original file remains available to download."


def _html_preview(name: str, text: str):
    safe_name = html.escape(name)
    safe_text = html.escape(text)
    body = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{safe_name}</title><style>body{{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#182238;margin:28px;line-height:1.65}}h2{{font-size:18px;margin:0 0 18px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.65 inherit}}</style></head><body><h2>{safe_name}</h2><pre>{safe_text}</pre></body></html>"""
    response = Response(body, mimetype="text/html; charset=utf-8")
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _duplicate_upload(con, table, original, ext, data, action="", conflict_id=""):
    """Store one upload with explicit duplicate/conflict handling."""
    digest = hashlib.sha256(data).hexdigest()

    rows = con.execute(
        f"SELECT id, original_name, stored_name, hash_sha256 FROM {table}"
    ).fetchall()

    match = next((r for r in rows if r["hash_sha256"] == digest), None)
    match_type = "content"

    if match is None:
        match = next(
            (r for r in rows
             if (r["original_name"] or "").casefold() == original.casefold()),
            None
        )
        match_type = "filename"

    if match is not None and not action:
        return {
            "ok": False,
            "duplicate": True,
            "match_type": match_type,
            "existing_id": match["id"],
            "existing_name": match["original_name"],
            "message": (
                "An identical file already exists."
                if match_type == "content"
                else "A file with this name already exists, but its content is different."
            )
        }, 409

    if match is not None and action == "cancel":
        return {"ok": True, "skipped": True, "message": "Upload cancelled."}, 200

    if action not in ("", "replace", "keep_both", "cancel"):
        return {"ok": False, "message": "Invalid duplicate action."}, 400

    replacing = match is not None and action == "replace"

    if replacing and conflict_id != match["id"]:
        return {
            "ok": False,
            "message": "Conflict changed. Please retry and review the current match."
        }, 409

    rid = match["id"] if replacing else uuid.uuid4().hex
    stored = uuid.uuid4().hex + ext
    target = FILES_DIR / stored
    temp_path = None
    old_path = FILES_DIR / match["stored_name"] if replacing else None

    try:
        with tempfile.NamedTemporaryFile(
            dir=FILES_DIR, prefix=".upload-", delete=False
        ) as tmp:
            tmp.write(data)
            temp_path = Path(tmp.name)

        os.replace(temp_path, target)
        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if replacing:
            con.execute(
                f"""UPDATE {table}
                    SET original_name=?, stored_name=?, extension=?,
                        size_bytes=?, uploaded_at=?, hash_sha256=?
                    WHERE id=?""",
                (original, stored, ext, len(data), uploaded_at, digest, rid)
            )
        else:
            con.execute(
                f"""INSERT INTO {table}
                    (id, original_name, stored_name, extension,
                     size_bytes, uploaded_at, hash_sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (rid, original, stored, ext, len(data), uploaded_at, digest)
            )

        con.commit()

        if replacing and old_path and old_path != target:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                current_app.logger.warning(
                    "Could not remove replaced file: %s", old_path
                )

        return {
            "ok": True,
            "added": 0 if replacing else 1,
            "replaced": 1 if replacing else 0,
            "id": rid,
            "original_name": original,
            "message": "Existing file replaced." if replacing else "File added."
        }, 200 if replacing else 201

    except Exception:
        con.rollback()
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise

    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _ensure_hash_column(con, table):
    columns = {
        row["name"] for row in con.execute(f"PRAGMA table_info({table})")
    }
    if "hash_sha256" not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN hash_sha256 TEXT")

    rows = con.execute(
        f"""SELECT id, stored_name FROM {table}
            WHERE hash_sha256 IS NULL OR hash_sha256=''"""
    ).fetchall()

    for row in rows:
        path = FILES_DIR / row["stored_name"]
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            con.execute(
                f"UPDATE {table} SET hash_sha256=? WHERE id=?",
                (digest, row["id"])
            )

    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_sha256 ON {table}(hash_sha256)"
    )


@resume_bp.get("/api/resumes")
def list_resumes():
    con = _connect()
    try:
        rows = con.execute("SELECT id, original_name, extension, size_bytes, uploaded_at FROM resumes ORDER BY uploaded_at DESC, rowid DESC").fetchall()
        return jsonify({"ok": True, "resumes": [dict(r) for r in rows], "count": len(rows)})
    finally:
        con.close()



@resume_bp.post("/api/resumes")
def upload_resumes():
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        return jsonify({"ok": False, "message": "Upload exceeds 100 MB."}), 413

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if len(files) != 1:
        return jsonify({
            "ok": False,
            "message": "Upload exactly one file per request."
        }), 400

    try:
        original, ext, data = _read_upload(files[0])
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    con = _connect()
    try:
        result, status = _duplicate_upload(
            con, "resumes", original, ext, data,
            request.form.get("duplicate_action", "").strip(),
            request.form.get("conflict_id", "").strip()
        )
        if status >= 400:
            return jsonify(result), status
        if result.get("skipped"):
            return jsonify(result), status
        return jsonify({
            "ok": True,
            "added": result["added"],
            "replaced": result["replaced"],
            "resumes": [{
                "id": result["id"],
                "original_name": result["original_name"]
            }],
            "message": result["message"]
        }), status
    except Exception:
        con.rollback()
        current_app.logger.exception("Resume upload failed")
        return jsonify({"ok": False, "message": "Resume upload failed."}), 500
    finally:
        con.close()

def _get_record(rid):
    if not ID_RE.fullmatch(rid or ""):
        abort(404)
    con = _connect()
    try:
        row = con.execute("SELECT * FROM resumes WHERE id=?", (rid,)).fetchone()
        if row is None:
            abort(404)
        return dict(row)
    finally:
        con.close()


@resume_bp.get("/api/resumes/<rid>/preview")
def preview_resume(rid):
    rec = _get_record(rid)
    path = FILES_DIR / rec["stored_name"]
    if not path.is_file(): abort(404)
    if rec["extension"] == ".pdf":
        response = send_file(path, mimetype="application/pdf", as_attachment=False, download_name=rec["original_name"], conditional=True)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'; object-src 'self'"
        return response
    if rec["extension"] == ".docx":
        try: text = _docx_text(path.read_bytes())
        except Exception: text = "DOCX text preview could not be generated. The original file remains available to download."
    else:
        text = _doc_text(path)
    return _html_preview(rec["original_name"], text)


@resume_bp.get("/api/resumes/<rid>/download")
def download_resume(rid):
    rec = _get_record(rid)
    path = FILES_DIR / rec["stored_name"]
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=True, download_name=rec["original_name"], conditional=True)


@resume_bp.delete("/api/resumes/<rid>")
def delete_resume(rid):
    rec = _get_record(rid)
    con = _connect()
    try:
        con.execute("DELETE FROM resumes WHERE id=?", (rid,))
        con.commit()
    finally:
        con.close()
    try: (FILES_DIR / rec["stored_name"]).unlink(missing_ok=True)
    except OSError: current_app.logger.exception("Could not remove resume file %s", rid)
    return jsonify({"ok": True, "deleted": rec["original_name"]})
