from sqlalchemy import create_engine, event
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

Base.metadata.create_all(bind=engine)