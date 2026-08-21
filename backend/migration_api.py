"""Short-lived, password-protected read-only export for SDP Hub migration."""

import base64
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request, send_file

from backend import config
from backend.db import DB_PATH


migration_api = Blueprint("migration_api", __name__)

MIGRATION_SCHEMA = "cwaa-service-v1"
SESSION_LIFETIME_HOURS = 24
MAX_PAGE_SIZE = 250
_PBKDF2_ROUNDS = 200_000
_sessions = {}
_lock = threading.RLock()
_SESSION_ROOT = Path(os.getcwd()).resolve() / "temp" / "migration_sessions"
# A clear password only exists in the previous process memory. Snapshots left
# by an unclean shutdown can never be used again and should not remain on disk.
shutil.rmtree(_SESSION_ROOT, ignore_errors=True)


class MigrationExportError(RuntimeError):
    pass


def _utcnow():
    return datetime.utcnow()


def _iso(value):
    return value.isoformat(timespec="seconds") + "Z" if value else ""


def _inside(path, root):
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except (TypeError, ValueError):
        return False


def _safe_relative(value):
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _display_path(value, variants):
    relative = _safe_relative(value)
    if relative:
        return relative
    for variant in variants:
        try:
            return Path(variant["path"]).relative_to(variant["root"]).as_posix()
        except (OSError, ValueError):
            continue
    return Path(str(value or "audio")).name or "audio"


def _inventory_key(value):
    return os.path.normcase(os.path.normpath(str(value or "")))


def _scan_storage_inventory(progress=None):
    """Read directory metadata once; never open or hash archive contents."""
    inventories = {}
    total_files = 0
    for access_level, root_value in (
        ("open", config.get("public_audio_path")),
        ("restricted", config.get("closed_audio_path")),
    ):
        if not root_value:
            continue
        root = Path(root_value).expanduser().resolve()
        inventory = {"root": str(root), "by_relative": {}, "by_absolute": {}, "files": []}
        inventories[access_level] = inventory
        if not root.is_dir():
            raise MigrationExportError(f"Корень CWAA недоступен для текущего пользователя: {root}")
        if progress:
            progress(f"Инвентаризация {access_level}: {root.name or root}")
        pending = [root]
        scanned_directories = 0
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as scanner:
                    entries = list(scanner)
            except OSError as exc:
                raise MigrationExportError(f"Не удалось прочитать каталог CWAA: {current}") from exc
            scanned_directories += 1
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    suffix = Path(entry.name).suffix.lower()
                    if suffix not in {".mp3", ".txt"}:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MigrationExportError(f"Не удалось прочитать файл CWAA: {entry.path}") from exc
                path = Path(os.path.abspath(entry.path))
                relative = Path(os.path.relpath(path, root)).as_posix()
                variant = {
                    "access_level": access_level,
                    "path": str(path),
                    "root": str(root),
                    "relative": relative,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "filename": path.name,
                }
                inventory["files"].append(variant)
                inventory["by_relative"][_inventory_key(relative)] = variant
                inventory["by_absolute"][_inventory_key(path)] = variant
                total_files += 1
            if progress and scanned_directories % 25 == 0:
                progress(f"Инвентаризация каталогов CWAA: найдено файлов {total_files}")
    if progress:
        progress(f"Метаинвентарь CWAA готов: файлов {total_files}; содержимое не читалось")
    return inventories


def _storage_variants(value, inventories):
    if not value:
        return []
    supplied = Path(str(value)).expanduser()
    variants = []
    for access_level, inventory in inventories.items():
        root = Path(inventory["root"])
        if supplied.is_absolute():
            variant = inventory["by_absolute"].get(_inventory_key(os.path.abspath(supplied)))
        else:
            relative = _safe_relative(value)
            if not relative:
                continue
            variant = inventory["by_relative"].get(_inventory_key(relative))
        if variant and _inside(variant["path"], root):
            variants.append(dict(variant))
    return variants


def _snapshot_database(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DB_PATH, timeout=60)
    destination = sqlite3.connect(str(target), timeout=60)
    try:
        source.execute("PRAGMA busy_timeout=60000")
        source.backup(destination, pages=1024)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
    finally:
        destination.close()
        source.close()


def _read_snapshot_rows(snapshot_path):
    connection = sqlite3.connect(str(snapshot_path), timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "audio_records" not in tables:
            raise MigrationExportError("В snapshot CWAA отсутствует audio_records")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audio_records)")}
        required = {
            "id", "user_folder", "case_number", "audio_date", "file_path", "comment",
            "courtroom", "recognize_text", "recognized_text_path", "uploaded_at", "uploaded_ip",
        }
        missing = sorted(required - columns)
        if missing:
            raise MigrationExportError("CWAA требует обновления; отсутствуют поля: " + ", ".join(missing))
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(audio_records)")}
        if "ix_audio_records_recognition_queue" not in indexes:
            raise MigrationExportError("CWAA требует patched-схему 2.4 с индексом очереди распознавания")
        has_fts = "record_texts" in tables
        for row in connection.execute("SELECT * FROM audio_records ORDER BY id"):
            payload = dict(row)
            payload["fts_available"] = bool(
                has_fts and connection.execute(
                    "SELECT 1 FROM record_texts WHERE audio_id=? LIMIT 1", (payload["id"],)
                ).fetchone()
            )
            yield payload
    finally:
        connection.close()


def _public_variant(variant, *, audio=False):
    payload = {
        "access_level": variant["access_level"],
        "size_bytes": variant["size_bytes"],
        "mtime_ns": variant["mtime_ns"],
        "filename": variant["filename"],
    }
    if audio:
        # Exactly the URL used by the regular CWAA archive player.
        payload["audio_url"] = f"/api/audio/{variant['relative']}"
    return payload


def _entry_from_row(row, inventories):
    audio_variants = _storage_variants(row.get("file_path"), inventories)
    text_variants = _storage_variants(row.get("recognized_text_path"), inventories)
    return {
        "id": int(row["id"]),
        "user_folder": str(row.get("user_folder") or ""),
        "case_number": str(row.get("case_number") or ""),
        "audio_date": str(row.get("audio_date") or ""),
        "comment": str(row.get("comment") or ""),
        "courtroom": str(row.get("courtroom") or ""),
        "recognize_text": bool(row.get("recognize_text")),
        "uploaded_at": str(row.get("uploaded_at") or ""),
        "uploaded_ip": str(row.get("uploaded_ip") or ""),
        "original_path": _display_path(row.get("file_path"), audio_variants),
        "audio_variants": audio_variants,
        "text_variants": text_variants,
        "fts_available": bool(row.get("fts_available")),
        "unindexed": False,
    }


def _closed_unindexed_entries(referenced_paths, inventories):
    inventory = inventories.get("restricted")
    if not inventory:
        return []
    root = Path(inventory["root"])
    result = []
    candidates = sorted(
        (item for item in inventory["files"] if Path(item["filename"]).suffix.lower() == ".mp3"),
        key=lambda item: item["relative"].casefold(),
    )
    for variant in candidates:
        resolved = Path(variant["path"])
        if os.path.normcase(str(resolved)) in referenced_paths:
            continue
        relative = PurePosixPath(variant["relative"])
        try:
            audio_at = datetime.strptime(resolved.stem, "%Y-%m-%d_%H-%M").isoformat(sep=" ")
        except ValueError:
            audio_at = ""
        text_path = resolved.with_suffix(".txt")
        text_variants = _storage_variants(str(text_path), inventories)
        legacy_id = -int(hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:12], 16)
        result.append({
            "id": legacy_id,
            "user_folder": relative.parts[0] if len(relative.parts) > 1 else "closed",
            "case_number": resolved.parent.name,
            "audio_date": audio_at,
            "comment": "Обнаружено в закрытом дереве без строки CWAA",
            "courtroom": "",
            "recognize_text": bool(text_variants),
            "uploaded_at": "",
            "uploaded_ip": "",
            "original_path": relative.as_posix(),
            "audio_variants": [variant],
            "text_variants": text_variants,
            "fts_available": False,
            "unindexed": True,
        })
    return result


def _public_entry(entry):
    payload = {
        key: value for key, value in entry.items()
        if key not in {"audio_variants", "text_variants"}
    }
    payload.update({
        "audio": [_public_variant(item, audio=True) for item in entry["audio_variants"]],
        "text": [_public_variant(item) for item in entry["text_variants"]],
        "text_available": bool(entry["text_variants"] or entry["fts_available"]),
    })
    return payload


def _session_fingerprint(entries):
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(str(entry["id"]).encode("ascii"))
        digest.update(entry["original_path"].encode("utf-8", errors="replace"))
        for variant in entry["audio_variants"]:
            digest.update(f"{variant['access_level']}:{variant['size_bytes']}:{variant['mtime_ns']}".encode("ascii"))
    return digest.hexdigest()


def _password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)


def _cleanup_expired_locked():
    now = _utcnow()
    expired = [key for key, value in _sessions.items() if value["expires_at"] <= now]
    for key in expired:
        session = _sessions.pop(key)
        shutil.rmtree(session["root"], ignore_errors=True)


def create_migration_session(hours=SESSION_LIFETIME_HOURS, progress=None):
    """Create one immutable export session. The clear password is returned once."""
    session_id = secrets.token_urlsafe(24)
    password = secrets.token_urlsafe(12)
    root = _SESSION_ROOT / session_id
    snapshot = root / "audio_archive.db"
    try:
        if progress:
            progress("Создаётся консистентный snapshot базы CWAA…")
        _snapshot_database(snapshot)
        rows = list(_read_snapshot_rows(snapshot))
        if progress:
            progress(f"Snapshot базы готов: записей {len(rows)}; читается метаинвентарь каталогов…")
        inventories = _scan_storage_inventory(progress=progress)
        entries = [_entry_from_row(row, inventories) for row in rows]
        referenced = {
            os.path.normcase(item["path"])
            for entry in entries for item in entry["audio_variants"]
        }
        entries.extend(_closed_unindexed_entries(referenced, inventories))
        entries.sort(key=lambda item: (int(item["id"] < 0), item["id"]))
        folders = {}
        for entry in entries:
            folder = entry["user_folder"].strip()
            if not folder:
                continue
            levels = folders.setdefault(folder.casefold(), {"folder": folder, "access_levels": set()})
            levels["access_levels"].update(item["access_level"] for item in entry["audio_variants"])
        salt = secrets.token_bytes(16)
        expires_at = _utcnow() + timedelta(hours=max(1, min(int(hours), 72)))
        state = {
            "id": session_id,
            "root": str(root),
            "snapshot": str(snapshot),
            "created_at": _utcnow(),
            "expires_at": expires_at,
            "salt": salt,
            "password_hash": _password_hash(password, salt),
            "failed_attempts": 0,
            "entries": entries,
            "entry_by_id": {str(item["id"]): item for item in entries},
            "folders": [
                {"folder": item["folder"], "access_levels": sorted(item["access_levels"])}
                for item in sorted(folders.values(), key=lambda item: item["folder"].casefold())
            ],
            "fingerprint": _session_fingerprint(entries),
        }
        with _lock:
            revoke_all_migration_sessions()
            _sessions[session_id] = state
        return {
            "session_id": session_id,
            "path": f"/api/migration/v1/sessions/{session_id}",
            "password": password,
            "created_at": _iso(state["created_at"]),
            "expires_at": _iso(expires_at),
            "records": len(entries),
            "bytes": sum(
                min((variant["size_bytes"] for variant in item["audio_variants"]), default=0)
                for item in entries
            ),
        }
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def revoke_all_migration_sessions():
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        shutil.rmtree(session["root"], ignore_errors=True)


def _basic_password():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return ""
    try:
        value = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        _username, separator, password = value.partition(":")
        return password if separator else ""
    except (ValueError, UnicodeDecodeError):
        return ""


def _authorized_session(session_id):
    with _lock:
        _cleanup_expired_locked()
        session = _sessions.get(str(session_id))
        if not session:
            return None, (jsonify({"error": "Сессия миграции не найдена или истекла"}), 404)
        if session["failed_attempts"] >= 20:
            return None, (jsonify({"error": "Сессия заблокирована после ошибок пароля"}), 429)
        supplied = _basic_password()
        expected = _password_hash(supplied, session["salt"]) if supplied else b""
        if not supplied or not hmac.compare_digest(expected, session["password_hash"]):
            session["failed_attempts"] += 1
            response = jsonify({"error": "Неверный пароль миграции"})
            response.headers["WWW-Authenticate"] = 'Basic realm="CWAA migration"'
            return None, (response, 401)
        session["failed_attempts"] = 0
        return session, None


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@migration_api.get("/sessions/<session_id>")
def migration_session_info(session_id):
    session, error = _authorized_session(session_id)
    if error:
        return error
    return _no_store(jsonify({
        "schema": MIGRATION_SCHEMA,
        "source": "CWAA",
        "source_version": "2.5.1",
        "snapshot_id": session["fingerprint"],
        "created_at": _iso(session["created_at"]),
        "expires_at": _iso(session["expires_at"]),
        "records": len(session["entries"]),
        "folders": len(session["folders"]),
        "capabilities": [
            "paged_manifest", "metadata_only_manifest", "archive_audio_url", "range_download",
            "transcripts", "unindexed_closed"
        ],
    }))


@migration_api.get("/sessions/<session_id>/folders")
def migration_session_folders(session_id):
    session, error = _authorized_session(session_id)
    if error:
        return error
    return _no_store(jsonify({"items": session["folders"]}))


@migration_api.get("/sessions/<session_id>/records")
def migration_session_records(session_id):
    session, error = _authorized_session(session_id)
    if error:
        return error
    try:
        cursor = max(0, int(request.args.get("cursor", 0)))
        limit = max(1, min(int(request.args.get("limit", 100)), MAX_PAGE_SIZE))
    except ValueError:
        return jsonify({"error": "Некорректная пагинация"}), 400
    rows = session["entries"][cursor:cursor + limit]
    next_cursor = cursor + len(rows)
    return _no_store(jsonify({
        "items": [_public_entry(item) for item in rows],
        "next_cursor": next_cursor if next_cursor < len(session["entries"]) else None,
        "total": len(session["entries"]),
        "snapshot_id": session["fingerprint"],
    }))


def _select_variant(entry, kind):
    variants = entry["audio_variants" if kind == "audio" else "text_variants"]
    requested_access = str(request.args.get("access") or "").strip().lower()
    if requested_access:
        variants = [item for item in variants if item["access_level"] == requested_access]
    if not variants:
        raise MigrationExportError("Файл недоступен в выбранном уровне доступа")
    if len(variants) > 1 and not requested_access:
        raise MigrationExportError("Укажите уровень доступа open или restricted")
    variant = variants[0]
    path = Path(variant["path"])
    if not path.is_file():
        raise MigrationExportError("Файл CWAA исчез после создания сессии")
    stat = path.stat()
    if stat.st_size != variant["size_bytes"] or stat.st_mtime_ns != variant["mtime_ns"]:
        raise MigrationExportError("Файл CWAA изменился после создания сессии; создайте новую ссылку")
    return path, variant


@migration_api.get("/sessions/<session_id>/records/<record_id>/<kind>")
def migration_session_file(session_id, record_id, kind):
    session, error = _authorized_session(session_id)
    if error:
        return error
    if kind not in {"audio", "text"}:
        return jsonify({"error": "Неизвестный вид файла"}), 404
    entry = session["entry_by_id"].get(str(record_id))
    if not entry:
        return jsonify({"error": "Запись snapshot не найдена"}), 404
    if kind == "text" and not entry["text_variants"] and entry["fts_available"] and entry["id"] > 0:
        with sqlite3.connect(session["snapshot"], timeout=60) as connection:
            row = connection.execute("SELECT content FROM record_texts WHERE audio_id=?", (entry["id"],)).fetchone()
        response = Response(str(row[0] or "") if row else "", content_type="text/plain; charset=utf-8")
        return _no_store(response)
    try:
        path, variant = _select_variant(entry, kind)
    except MigrationExportError as exc:
        return jsonify({"error": str(exc)}), 409
    if kind == "audio":
        # Keep Flask as the authorization/control plane and let nginx serve
        # large files whenever its managed archive alias is available.
        from backend.api import _x_accel_redirect
        prefix = "/protected_public_audio/" if variant["access_level"] == "open" else "/protected_closed_audio/"
        response = _x_accel_redirect(
            prefix,
            variant["root"],
            path,
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name=variant["filename"],
        )
        if isinstance(response, tuple):
            return response
    else:
        response = send_file(
            path,
            mimetype="text/plain",
            as_attachment=False,
            download_name=variant["filename"],
            conditional=True,
            max_age=0,
        )
    response.headers["X-CWAA-Size"] = str(variant["size_bytes"])
    response.headers["X-CWAA-Mtime-Ns"] = str(variant["mtime_ns"])
    return _no_store(response)
