from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Index, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class AudioRecord(Base):
    __tablename__ = 'audio_records'
    __table_args__ = (
        UniqueConstraint('file_path', name='uq_audio_records_file_path'),
        UniqueConstraint('user_folder', 'case_number', 'audio_date', name='uq_audio_records_case_datetime'),
        Index('ix_audio_records_audio_date', 'audio_date'),
        Index('ix_audio_records_case_number', 'case_number'),
        Index('ix_audio_records_user_folder', 'user_folder'),
        Index('ix_audio_records_recognition_queue', 'recognize_text', 'recognized_text_path'),
    )

    id = Column(Integer, primary_key=True)
    user_folder = Column(String, nullable=False)
    case_number = Column(String, nullable=False)
    audio_date = Column(DateTime, nullable=False)
    file_path = Column(String, nullable=False)
    comment = Column(String)
    courtroom = Column(String)
    recognize_text = Column(Boolean, default=False)
    recognized_text_path = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    uploaded_ip = Column(String, nullable=True)


class DownloadLog(Base):
    __tablename__ = 'download_logs'
    __table_args__ = (
        Index('ix_download_logs_record_ip_timestamp', 'record_id', 'ip', 'timestamp'),
    )

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer, ForeignKey('audio_records.id', ondelete='CASCADE'), nullable=False)
    ip = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
