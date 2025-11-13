from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from backend.models import Base

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
    cursor.close()


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

Base.metadata.create_all(bind=engine)
patch_existing_db(engine)