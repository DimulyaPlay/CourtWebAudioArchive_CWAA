from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class AudioRecord(Base):
    __tablename__ = 'audio_records'

    id = Column(Integer, primary_key=True)
    user_folder = Column(String, nullable=False)
    case_number = Column(String, nullable=False)
    audio_date = Column(DateTime, nullable=False)
    file_path = Column(String, nullable=False)
    comment = Column(String)
    courtroom = Column(String)
    recognize_text = Column(Boolean, default=False)
    recognized_text_path = Column(String, nullable=True)