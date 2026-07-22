from PySide2.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QCheckBox, QTimeEdit, QSpinBox,
    QPushButton, QFileDialog, QGroupBox, QLineEdit, QProgressBar, QHBoxLayout, QMessageBox
)
from PySide2.QtCore import Qt, QTime, QObject, Signal
from . import config
from .db import checkpoint_wal, sqlite_backup_snapshot
from contextlib import ExitStack
import os
import shutil
import sqlite3
import tempfile
import zipfile
import datetime
import threading
import schedule
import time as time_module
from pathlib import PurePosixPath

BACKUP_CONFIG_PATH = './backup_config.txt'
BACKUP_JOB_TAG = 'cwaa_backup'
BACKUP_RETRY_COUNT = 3
BACKUP_RETRY_DELAY_SECONDS = 2
CHUNK_SIZE = 1024 * 1024


class BackupError(Exception):
    pass


class BackupSignals(QObject):
    started = Signal()
    status = Signal(str)
    progress = Signal(int)
    finished = Signal(bool)


def _normalize_backup_path(path):
    cleaned = (path or '').strip()
    if not cleaned:
        return ''
    return os.path.abspath(os.path.expandvars(os.path.expanduser(cleaned)))


def _same_or_inside(path, root):
    try:
        common = os.path.commonpath([os.path.abspath(path), os.path.abspath(root)])
        return common == os.path.abspath(root)
    except ValueError:
        return False


def _zip_arcname(*parts):
    return os.path.join(*[str(part) for part in parts if part]).replace('\\', '/')


def _copy_file_with_retries(src, dst):
    last_error = None
    for attempt in range(1, BACKUP_RETRY_COUNT + 1):
        try:
            with open(src, 'rb') as source, open(dst, 'wb') as target:
                shutil.copyfileobj(source, target, CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            return
        except OSError as exc:
            last_error = exc
            try:
                if os.path.exists(dst):
                    os.remove(dst)
            except OSError:
                pass
            if attempt < BACKUP_RETRY_COUNT:
                time_module.sleep(BACKUP_RETRY_DELAY_SECONDS * attempt)
    raise BackupError(f"Не удалось записать архив в папку бэкапа: {last_error}")


def _replace_with_retries(src, dst):
    last_error = None
    for attempt in range(1, BACKUP_RETRY_COUNT + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_error = exc
            if attempt < BACKUP_RETRY_COUNT:
                time_module.sleep(BACKUP_RETRY_DELAY_SECONDS * attempt)
    raise BackupError(f"Не удалось завершить запись архива: {last_error}")


def _ensure_writable_directory(path):
    if not path:
        raise BackupError("Не указана папка для бэкапов")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Не удалось создать папку бэкапов: {exc}") from exc
    if not os.path.isdir(path):
        raise BackupError("Путь для бэкапов не является папкой")

    probe = os.path.join(path, f".cwaa_backup_write_test_{os.getpid()}.tmp")
    try:
        with open(probe, 'wb') as f:
            f.write(b'ok')
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise BackupError(f"Нет доступа на запись в папку бэкапов: {exc}") from exc
    finally:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass


def _parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'да'}


def _read_backup_config():
    if not os.path.exists(BACKUP_CONFIG_PATH):
        return {}
    data = {}
    with open(BACKUP_CONFIG_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, value = line.split('=', 1)
            data[key.strip()] = value.strip()
    return data


def _parse_key_value_text(text):
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key.strip()] = value.strip()
    return data


def _parse_restored_app_config(text):
    restored = config.copy()
    restored.update(_parse_key_value_text(text))
    if 'server_port' in restored:
        try:
            restored['server_port'] = int(restored['server_port'])
        except (TypeError, ValueError):
            raise BackupError("В архиве некорректный server_port в settings/config.txt")
    return restored


def _selected_day_names(raw_days, day_names):
    aliases = {
        'mon': 'monday',
        'monday': 'monday',
        'tue': 'tuesday',
        'tuesday': 'tuesday',
        'wed': 'wednesday',
        'wednesday': 'wednesday',
        'thu': 'thursday',
        'thursday': 'thursday',
        'fri': 'friday',
        'friday': 'friday',
        'sat': 'saturday',
        'saturday': 'saturday',
        'sun': 'sunday',
        'sunday': 'sunday',
    }
    normalized = {
        aliases.get(day.strip().lower(), day.strip().lower())
        for day in raw_days.split(',')
        if day.strip()
    }
    return [day in normalized for day in day_names]


def _is_safe_zip_name(name):
    normalized = str(name).replace('\\', '/')
    if not normalized or normalized.startswith('/'):
        return False
    if ':' in PurePosixPath(normalized).parts[0]:
        return False
    return '..' not in PurePosixPath(normalized).parts


def _has_zip_prefix(zipf, prefix):
    normalized_prefix = prefix.rstrip('/') + '/'
    return any(item.filename.replace('\\', '/').startswith(normalized_prefix) for item in zipf.infolist())


def _read_zip_text(zipf, name):
    try:
        with zipf.open(name) as f:
            return f.read().decode('utf-8')
    except KeyError as exc:
        raise BackupError(f"В архиве нет обязательного файла {name}") from exc
    except UnicodeDecodeError as exc:
        raise BackupError(f"Файл {name} в архиве не читается как UTF-8") from exc


def _ensure_restore_target_empty(path, label):
    if not path:
        raise BackupError(f"В конфиге бэкапа не указан путь для {label}")
    if not os.path.isabs(path):
        raise BackupError(f"Путь для {label} в конфиге бэкапа должен быть абсолютным: {path}")
    if os.path.exists(path):
        if not os.path.isdir(path):
            raise BackupError(f"Путь для {label} не является папкой: {path}")
        try:
            if any(os.scandir(path)):
                raise BackupError(f"Папка для {label} должна быть пустой: {path}")
        except OSError as exc:
            raise BackupError(f"Не удалось проверить папку для {label}: {exc}") from exc


def _database_has_records(db_path):
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return False
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            for table in ('audio_records', 'download_logs'):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                ).fetchone()
                if not exists:
                    continue
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if count:
                    return True
    except sqlite3.Error as exc:
        raise BackupError(f"Не удалось проверить текущую базу данных: {exc}") from exc
    return False


def _ensure_database_empty_for_restore(db_path):
    if _database_has_records(db_path):
        raise BackupError("Восстановление разрешено только если текущая база данных пустая")


def _write_zip_member_to_file(zipf, member, target_path):
    tmp_path = target_path + '.restore_tmp'
    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    with zipf.open(member) as source, open(tmp_path, 'wb') as target:
        shutil.copyfileobj(source, target, CHUNK_SIZE)
        target.flush()
        os.fsync(target.fileno())
    os.replace(tmp_path, target_path)


def _restore_tree_from_zip(zipf, prefix, target_root, created_paths):
    normalized_prefix = prefix.rstrip('/') + '/'
    os.makedirs(target_root, exist_ok=True)
    created_paths.append(target_root)
    for item in zipf.infolist():
        name = item.filename.replace('\\', '/')
        if item.is_dir() or not name.startswith(normalized_prefix):
            continue
        if not _is_safe_zip_name(name):
            raise BackupError(f"Небезопасный путь в архиве: {item.filename}")
        rel_name = name[len(normalized_prefix):]
        if not rel_name:
            continue
        target_path = os.path.abspath(os.path.join(target_root, *PurePosixPath(rel_name).parts))
        if not _same_or_inside(target_path, target_root):
            raise BackupError(f"Файл из архива выходит за пределы папки назначения: {item.filename}")
        if os.path.exists(target_path):
            raise BackupError(f"Файл уже существует при восстановлении: {target_path}")
        _write_zip_member_to_file(zipf, item, target_path)
        created_paths.append(target_path)


def _cleanup_created_restore_paths(paths):
    for path in sorted(set(paths), key=lambda item: len(os.path.abspath(item)), reverse=True):
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass


def restore_backup_archive(archive_path):
    archive_path = os.path.abspath(archive_path)
    if not os.path.isfile(archive_path):
        raise BackupError(f"Файл бэкапа не найден: {archive_path}")

    created_paths = []
    try:
        with zipfile.ZipFile(archive_path, 'r') as zipf:
            bad_file = zipf.testzip()
            if bad_file:
                raise BackupError(f"Архив поврежден, первый проблемный файл: {bad_file}")
            for item in zipf.infolist():
                if not _is_safe_zip_name(item.filename):
                    raise BackupError(f"Небезопасный путь в архиве: {item.filename}")

            restored_config = _parse_restored_app_config(_read_zip_text(zipf, 'settings/config.txt'))
            has_db = any(item.filename.replace('\\', '/') == 'audio_archive.db' for item in zipf.infolist())
            has_public = _has_zip_prefix(zipf, 'public_audio')
            has_closed = _has_zip_prefix(zipf, 'closed_audio')

            if has_public:
                _ensure_restore_target_empty(restored_config.get('public_audio_path'), 'открытых аудиопротоколов')
            if has_closed:
                _ensure_restore_target_empty(restored_config.get('closed_audio_path'), 'закрытых аудиопротоколов')
            if has_public and has_closed:
                public_path = os.path.abspath(restored_config.get('public_audio_path') or '')
                closed_path = os.path.abspath(restored_config.get('closed_audio_path') or '')
                if public_path == closed_path:
                    raise BackupError("В конфиге бэкапа открытый и закрытый архив указывают на одну папку")

            db_path = os.path.abspath('audio_archive.db')
            if has_db:
                _ensure_database_empty_for_restore(db_path)

            if has_public:
                _restore_tree_from_zip(zipf, 'public_audio', restored_config['public_audio_path'], created_paths)
            if has_closed:
                _restore_tree_from_zip(zipf, 'closed_audio', restored_config['closed_audio_path'], created_paths)

            if has_db:
                from .db import engine
                engine.dispose()
                for suffix in ('', '-wal', '-shm'):
                    path = db_path + suffix
                    if os.path.exists(path):
                        os.remove(path)
                _write_zip_member_to_file(zipf, zipf.getinfo('audio_archive.db'), db_path)

            restored_settings = []
            for archive_name, target_name in (
                ('settings/config.txt', 'config.txt'),
                ('settings/backup_config.txt', BACKUP_CONFIG_PATH),
                ('settings/courtrooms.txt', 'courtrooms.txt'),
                ('settings/import_sources.txt', 'import_sources.txt'),
            ):
                if archive_name in zipf.namelist():
                    _write_zip_member_to_file(zipf, zipf.getinfo(archive_name), target_name)
                    restored_settings.append(target_name)

            config.clear()
            config.update(restored_config)
            return {
                'archive_path': archive_path,
                'restored_config': restored_config,
                'restored_db': has_db,
                'restored_public': has_public,
                'restored_closed': has_closed,
                'restored_settings': restored_settings,
            }
    except Exception:
        _cleanup_created_restore_paths(created_paths)
        raise


def _collect_source_files(src, arc_root, backup_folder):
    if not src:
        raise BackupError(f"Не указан путь для раздела {arc_root}")
    if not os.path.exists(src):
        raise BackupError(f"Источник недоступен: {src}")

    backup_folder_abs = os.path.abspath(backup_folder)
    collected = []
    if os.path.isdir(src):
        for root, dirs, files in os.walk(src):
            dirs[:] = [
                item for item in dirs
                if not _same_or_inside(os.path.join(root, item), backup_folder_abs)
            ]
            for file in files:
                full_path = os.path.join(root, file)
                if _same_or_inside(full_path, backup_folder_abs):
                    continue
                rel_path = os.path.relpath(full_path, src)
                collected.append((full_path, _zip_arcname(arc_root, rel_path)))
    elif os.path.isfile(src):
        collected.append((src, _zip_arcname(arc_root)))
    else:
        raise BackupError(f"Источник не является файлом или папкой: {src}")
    return collected


def _prune_old_backups(backup_folder, keep_count):
    backups = []
    for name in os.listdir(backup_folder):
        if not name.startswith("backup_") or not name.endswith(".zip"):
            continue
        path = os.path.join(backup_folder, name)
        if os.path.isfile(path):
            backups.append((os.path.getmtime(path), path))

    backups.sort(reverse=True)
    for _, old_path in backups[keep_count:]:
        try:
            os.remove(old_path)
        except OSError as exc:
            print("Ошибка при удалении старой копии:", old_path, exc)


def create_backup_archive(settings, progress_callback=None):
    def emit_progress(value):
        if progress_callback:
            progress_callback(value)

    backup_folder = _normalize_backup_path(settings['backup_path'])
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    archive_path = os.path.join(backup_folder, f"backup_{timestamp}.zip")
    archive_tmp_path = os.path.join(backup_folder, f".backup_{timestamp}.zip.tmp")

    include_paths = []
    if settings['include_public']:
        include_paths.append((config['public_audio_path'], "public_audio"))

    if settings['include_closed']:
        include_paths.append((config['closed_audio_path'], "closed_audio"))

    try:
        _ensure_writable_directory(backup_folder)
        with tempfile.TemporaryDirectory(prefix='cwaa_backup_') as temp_dir, ExitStack() as stack:
            if settings['include_db']:
                db_path = os.path.abspath("audio_archive.db")
                if os.path.exists(db_path):
                    snapshot_path = os.path.join(temp_dir, f"_audio_archive_snapshot_{timestamp}.db")
                    db_snapshot = stack.enter_context(sqlite_backup_snapshot(snapshot_path))
                    include_paths.insert(0, (db_snapshot, "audio_archive.db"))
                else:
                    raise BackupError("Файл базы audio_archive.db не найден")

            all_files = []
            for src, arcname in include_paths:
                all_files.extend(_collect_source_files(src, arcname, backup_folder))

            for metadata_file in ('config.txt', BACKUP_CONFIG_PATH, 'courtrooms.txt', 'import_sources.txt'):
                if os.path.isfile(metadata_file):
                    all_files.append((metadata_file, _zip_arcname('settings', metadata_file)))

            if not all_files:
                raise BackupError("Нет файлов для резервного копирования")

            total_files = len(all_files)
            progress = 0
            local_archive_path = os.path.join(temp_dir, f"backup_{timestamp}.zip")

            with zipfile.ZipFile(local_archive_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zipf:
                for full_path, arcname in all_files:
                    try:
                        zipf.write(full_path, arcname)
                    except OSError as exc:
                        raise BackupError(f"Не удалось прочитать файл для бэкапа: {full_path}. {exc}") from exc
                    progress += 1
                    emit_progress(int(progress / total_files * 90))

            _copy_file_with_retries(local_archive_path, archive_tmp_path)
            _replace_with_retries(archive_tmp_path, archive_path)
            emit_progress(100)

        if settings['include_db']:
            try:
                checkpoint_wal('TRUNCATE')
            except Exception as exc:
                print("Ошибка SQLite WAL checkpoint после бэкапа:", exc)

        _prune_old_backups(backup_folder, settings['keep_count'])
        return archive_path
    except Exception:
        try:
            if os.path.exists(archive_tmp_path):
                os.remove(archive_tmp_path)
        except OSError:
            pass
        raise


class BackupSettingsWindow(QWidget):
    _scheduler_lock = threading.Lock()
    _scheduler_started = False
    _backup_lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Настройки резервного копирования")
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        # Блок "Основные настройки"
        main_group = QGroupBox("Основные параметры")
        main_layout = QVBoxLayout()

        self.enable_backup = QCheckBox("Резервное копирование по расписанию")
        main_layout.addWidget(self.enable_backup)

        days_layout = QHBoxLayout()
        self.day_checkboxes = []
        self.day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        days_rus = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        for i, day in enumerate(days_rus):
            cb = QCheckBox(day)
            self.day_checkboxes.append(cb)
            days_layout.addWidget(cb)
        main_layout.addLayout(days_layout)

        time_layout = QHBoxLayout()
        time_layout.addWidget(QLabel("🕒 Время запуска:"))
        self.time_edit = QTimeEdit(QTime(3, 0))
        self.time_edit.setDisplayFormat("HH:mm")
        time_layout.addWidget(self.time_edit)
        main_layout.addLayout(time_layout)

        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("📁 Папка для хранения:"))
        self.path_edit = QLineEdit("C:\\Backups")
        path_btn = QPushButton("🔍")
        path_btn.setMaximumWidth(38)
        path_btn.clicked.connect(self.select_folder)
        path_layout.addWidget(self.path_edit)
        path_layout.addWidget(path_btn)
        main_layout.addLayout(path_layout)

        keep_layout = QHBoxLayout()
        keep_layout.addWidget(QLabel("🗂 Хранить копий:"))
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(1, 100)
        self.keep_spin.setValue(5)
        keep_layout.addWidget(self.keep_spin)
        main_layout.addLayout(keep_layout)

        main_group.setLayout(main_layout)
        layout.addWidget(main_group)

        # Блок "Что включать в архив"
        include_group = QGroupBox("Содержимое архива")
        include_layout = QVBoxLayout()
        self.include_db = QCheckBox("🧠 База данных (audio_archive.db)")
        self.include_public = QCheckBox("🌐 Открытые аудиопротоколы")
        self.include_closed = QCheckBox("🔒 Закрытые аудиопротоколы")
        self.include_db.setChecked(True)
        self.include_public.setChecked(True)
        self.include_closed.setChecked(True)
        include_layout.addWidget(self.include_db)
        include_layout.addWidget(self.include_public)
        include_layout.addWidget(self.include_closed)
        include_group.setLayout(include_layout)
        layout.addWidget(include_group)

        # Кнопки управления
        self.save_btn = QPushButton("💾 Сохранить настройки")
        self.run_backup_btn = QPushButton("🛠 Создать бэкап сейчас")
        layout.addWidget(self.save_btn)
        layout.addWidget(self.run_backup_btn)

        # Прогресс и статус
        self.status_label = QLabel("⏳ Ожидание...")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

        self.signals = BackupSignals()
        self.signals.started.connect(self.backup_started)
        self.signals.status.connect(self.status_label_safe)
        self.signals.progress.connect(self.progress_bar.setValue)
        self.signals.finished.connect(self.backup_finished)

        self.setLayout(layout)

        self.save_btn.clicked.connect(self.save_config)
        self.run_backup_btn.clicked.connect(self.confirm_and_run_backup)

        self.load_config()
        self.configure_schedule()
        self.ensure_scheduler_thread()
        self.update_next_backup_status()

    def status_label_safe(self, text):
        self.status_label.setText(text)

    def backup_started(self):
        self.run_backup_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

    def backup_finished(self, success):
        self.run_backup_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        if success:
            self.update_next_backup_status()

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку для бэкапов")
        if folder:
            self.path_edit.setText(folder)

    def confirm_and_run_backup(self):
        reply = QMessageBox.question(self, "Подтвердите запуск",
                                     "Вы действительно хотите запустить бэкап прямо сейчас? Это может занять несколько минут.",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            settings = self.current_backup_settings()
            threading.Thread(target=self.run_backup_now, args=(settings,), daemon=True, name='backup_manual').start()

    def current_backup_settings(self):
        return {
            'backup_path': self.path_edit.text(),
            'keep_count': self.keep_spin.value(),
            'include_db': self.include_db.isChecked(),
            'include_public': self.include_public.isChecked(),
            'include_closed': self.include_closed.isChecked(),
            'selected_days': [day for day, cb in zip(self.day_names, self.day_checkboxes) if cb.isChecked()],
            'time': self.time_edit.time().toString("HH:mm"),
            'enabled': self.enable_backup.isChecked(),
        }

    def update_next_backup_status(self):
        if not self.enable_backup.isChecked():
            self.status_label.setText("⛔ Резервное копирование по расписанию отключено.")
            return

        selected_days = [i for i, cb in enumerate(self.day_checkboxes) if cb.isChecked()]
        if not selected_days:
            self.status_label.setText("⚠ Расписание включено, но дни не выбраны")
            return

        now = datetime.datetime.now()
        target_time = self.time_edit.time()
        target_dt = now.replace(hour=target_time.hour(), minute=target_time.minute(), second=0, microsecond=0)

        days_ahead = [(d - now.weekday()) % 7 for d in selected_days]
        deltas = []
        for delta_day in days_ahead:
            dt_candidate = target_dt + datetime.timedelta(days=delta_day)
            if dt_candidate < now:
                dt_candidate += datetime.timedelta(days=7)
            deltas.append(dt_candidate - now)

        if deltas:
            nearest = min(deltas)
            hours, remainder = divmod(nearest.total_seconds(), 3600)
            minutes = remainder // 60
            self.status_label.setText(f"📅 Следующий запуск через {int(hours)}ч {int(minutes)}м")
        else:
            self.status_label.setText("⏳ Ожидание...")

    def load_config(self):
        config_data = _read_backup_config()
        if not config_data:
            return

        self.enable_backup.setChecked(_parse_bool(config_data.get('backup_enabled'), False))
        for i, checked in enumerate(_selected_day_names(config_data.get('backup_days', ''), self.day_names)):
            self.day_checkboxes[i].setChecked(checked)

        time_parts = config_data.get('backup_time', '03:00').split(':')
        if len(time_parts) == 2:
            try:
                hour, minute = int(time_parts[0]), int(time_parts[1])
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    self.time_edit.setTime(QTime(hour, minute))
            except ValueError:
                pass

        self.path_edit.setText(config_data.get('backup_path', 'C:\\CWAA_Backups'))
        try:
            self.keep_spin.setValue(int(config_data.get('backup_keep', 5)))
        except ValueError:
            self.keep_spin.setValue(5)
        self.include_db.setChecked(_parse_bool(config_data.get('backup_include_db'), True))
        self.include_public.setChecked(_parse_bool(config_data.get('backup_include_public'), True))
        self.include_closed.setChecked(_parse_bool(config_data.get('backup_include_closed'), True))

    def save_config(self):
        config_lines = [
            f"backup_enabled={'true' if self.enable_backup.isChecked() else 'false'}",
            f"backup_days={','.join([day for day, cb in zip(self.day_names, self.day_checkboxes) if cb.isChecked()])}",
            f"backup_time={self.time_edit.time().toString('HH:mm')}",
            f"backup_path={self.path_edit.text()}",
            f"backup_keep={self.keep_spin.value()}",
            f"backup_include_db={'true' if self.include_db.isChecked() else 'false'}",
            f"backup_include_public={'true' if self.include_public.isChecked() else 'false'}",
            f"backup_include_closed={'true' if self.include_closed.isChecked() else 'false'}",
        ]
        with open(BACKUP_CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write('\n'.join(config_lines))
        self.status_label.setText("✅ Настройки сохранены.")
        self.configure_schedule()
        self.ensure_scheduler_thread()
        self.update_next_backup_status()

    def configure_schedule(self):
        settings = self.current_backup_settings()
        with self._scheduler_lock:
            schedule.clear(BACKUP_JOB_TAG)
            if not settings['enabled']:
                return
            for day in settings['selected_days']:
                getattr(schedule.every(), day).at(settings['time']).do(self.run_scheduled_backup, settings.copy()).tag(BACKUP_JOB_TAG)
            print("Бэкапы по расписанию запущены")

    @classmethod
    def ensure_scheduler_thread(cls):
        with cls._scheduler_lock:
            if cls._scheduler_started:
                return
            cls._scheduler_started = True

        def run_scheduler():
            while True:
                with cls._scheduler_lock:
                    schedule.run_pending()
                time_module.sleep(30)

        thread = threading.Thread(target=run_scheduler, daemon=True, name='backup_scheduler')
        thread.start()

    def run_scheduled_backup(self, settings):
        threading.Thread(target=self.run_backup_now, args=(settings,), daemon=True, name='backup_scheduled').start()

    def run_backup_now(self, settings=None):
        settings = settings or self.current_backup_settings()
        if not self._backup_lock.acquire(blocking=False):
            self.signals.status.emit("⚠ Бэкап уже выполняется.")
            return

        self.signals.started.emit()
        self.signals.status.emit("⏳ Создание бэкапа...")
        self.signals.progress.emit(0)

        try:
            archive_path = create_backup_archive(settings, self.signals.progress.emit)
            self.signals.status.emit(f"✅ Бэкап завершён: {archive_path}")
            self.signals.finished.emit(True)
        except Exception as e:
            self.signals.status.emit(f"❌ Ошибка: {str(e)}")
            self.signals.finished.emit(False)
        finally:
            self._backup_lock.release()
