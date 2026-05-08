import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from backend.models import Base
from datetime import datetime

DB_PATH = os.path.abspath("audio_archive.db")
CHECKPOINT_INTERVAL_SECONDS = 60 * 60

engine = create_engine(
    'sqlite:///audio_archive.db',
    connect_args={'check_same_thread': False, 'timeout': 60},
    pool_pre_ping=True,
    future=True,
)
Session = sessionmaker(bind=engine, future=True)

@event.listens_for(engine, 'connect')
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA synchronous=NORMAL;')
    cursor.execute('PRAGMA foreign_keys=ON;')
    cursor.execute('PRAGMA wal_autocheckpoint=1000;')
    cursor.close()


def checkpoint_wal(mode='PASSIVE'):
    """Переносит накопленные WAL-страницы в основной файл базы без остановки сервера."""
    allowed_modes = {'PASSIVE', 'FULL', 'RESTART', 'TRUNCATE'}
    mode = mode.upper()
    if mode not in allowed_modes:
        raise ValueError(f"Unsupported checkpoint mode: {mode}")
    if not os.path.exists(DB_PATH):
        return None

    with sqlite3.connect(DB_PATH, timeout=60) as conn:
        conn.execute('PRAGMA busy_timeout=60000;')
        return conn.execute(f'PRAGMA wal_checkpoint({mode});').fetchone()


@contextmanager
def sqlite_backup_snapshot(snapshot_path):
    """
    Создает консистентный снимок SQLite через backup API.
    Такой снимок включает изменения из WAL и не требует остановки сервера.
    """
    if os.path.exists(snapshot_path):
        os.remove(snapshot_path)

    source = sqlite3.connect(DB_PATH, timeout=60)
    target = sqlite3.connect(snapshot_path)
    try:
        source.execute('PRAGMA busy_timeout=60000;')
        source.backup(target)
        target.execute('PRAGMA journal_mode=DELETE;')
        target.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        target.commit()
        yield snapshot_path
    finally:
        target.close()
        source.close()
        try:
            if os.path.exists(snapshot_path):
                os.remove(snapshot_path)
        except OSError as exc:
            print("Не удалось удалить временный снимок SQLite:", exc)


def start_periodic_wal_checkpoint():
    def run():
        while True:
            time.sleep(CHECKPOINT_INTERVAL_SECONDS)
            try:
                checkpoint_wal('TRUNCATE')
            except Exception as exc:
                print("Ошибка SQLite WAL checkpoint:", exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


def patch_existing_db(engine):
    """Проверяет наличие новых колонок и добавляет их при необходимости."""
    with engine.begin() as conn:
        inspector = inspect(conn)
        columns = [col['name'] for col in inspector.get_columns('audio_records')]

        # uploaded_at — добавляем, если отсутствует
        if 'uploaded_at' not in columns:
            print("🩹 Добавляем колонку uploaded_at в audio_records...")
            conn.execute(text("ALTER TABLE audio_records ADD COLUMN uploaded_at DATETIME"))
            conn.execute(text("UPDATE audio_records SET uploaded_at = :ts"), {"ts": datetime.utcnow()})

        # uploaded_ip — добавляем, если отсутствует
        if 'uploaded_ip' not in columns:
            print("🩹 Добавляем колонку uploaded_ip в audio_records...")
            conn.execute(text("ALTER TABLE audio_records ADD COLUMN uploaded_ip TEXT"))
            conn.execute(text("UPDATE audio_records SET uploaded_ip = 'unknown'"))

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audio_records_audio_date ON audio_records (audio_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audio_records_case_number ON audio_records (case_number)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audio_records_user_folder ON audio_records (user_folder)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_audio_records_recognition_queue "
            "ON audio_records (recognize_text, recognized_text_path)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_download_logs_record_ip_timestamp "
            "ON download_logs (record_id, ip, timestamp)"
        ))

Base.metadata.create_all(bind=engine)
patch_existing_db(engine)
start_periodic_wal_checkpoint()
