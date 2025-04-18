from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.models import Base

engine = create_engine('sqlite:///audio_archive.db')
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)
with engine.connect() as conn:
    conn.exec_driver_sql("""
        CREATE VIRTUAL TABLE IF NOT EXISTS record_texts
        USING fts5(audio_id UNINDEXED, content);
    """)