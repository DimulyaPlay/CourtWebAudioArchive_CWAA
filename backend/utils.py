import os
import shutil
import time
import hashlib
import socket
from traceback import print_exc
import subprocess
from sqlalchemy import text
from backend.db import engine, Session
from backend.models import AudioRecord
import re
from datetime import datetime

COURTROOMS_FILE = 'courtrooms.txt'

def get_available_courtrooms():
    if not os.path.exists(COURTROOMS_FILE):
        return []
    with open(COURTROOMS_FILE, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]

def get_server_ip():
    hostname = socket.gethostname()
    ip_address = socket.gethostbyname(hostname)
    return ip_address

def get_all_public_ips():
    public_ips = []
    hostname = socket.gethostname()
    addresses = socket.getaddrinfo(hostname, None)
    for address in addresses:
        ip = address[4][0]
        if not ip.startswith("127."):  # Исключаем локалхост
            if ':' not in ip:  # Исключаем IPv6-адреса
                public_ips.append(ip)

    return list(set(public_ips))  # Убираем дубликаты


def read_create_config():
    default_configuration = {
        'server_ip': get_server_ip(),
        'server_port': 446,
        "public_audio_path": "",
        "closed_audio_path": "",
        "recognize_text_from_audio_path": '',
        'create_year_subfolders': 'false'
    }
    config = default_configuration.copy()
    if os.path.exists('config.txt'):
        with open('config.txt', 'r') as f:
            for line in f:
                key, value = line.strip().split('=', 1)
                if key in config:
                    if key in ['server_port']:
                        config[key] = int(value)
                    else:
                        config[key] = value
    with open('config.txt', 'w') as f:
        for key, value in config.items():
            f.write(f"{key}={value}\n")

    return config


def save_config(configuration):
    with open('config.txt', 'w') as f:
        for key, value in configuration.items():
            f.write(f"{key}={value}\n")


def get_file_hash(file_path):
    """Возвращает хэш файла для проверки изменений."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def compare_files(file1, file2):
    """Сравнивает два файла по их хэшу."""
    if not os.path.exists(file1) or not os.path.exists(file2):
        return False
    file1_hash = get_file_hash(file1)
    file2_hash = get_file_hash(file2)
    return file1_hash == file2_hash


def is_file_fully_copied(file_path, check_interval=2, retries=5):
    """Проверяет, завершено ли копирование файла, отслеживая изменение размера."""
    for _ in range(retries):
        size1 = os.path.getsize(file_path)
        time.sleep(check_interval)
        size2 = os.path.getsize(file_path)
        if size1 == size2:
            return True
        print(f"Файл {file_path} еще копируется, ждем...")
    print(f"Файл {file_path} возможно поврежден или не завершен, пропускаем.")
    return False


def index_record_text(audio_id: int, content: str):
    with engine.begin() as conn:
        print(f"[FTS] Индексация: ID={audio_id}, размер текста={len(content)}")
        conn.exec_driver_sql(
            "INSERT INTO record_texts (audio_id, content) VALUES (?, ?)",
            parameters=[(audio_id, content)]
        )


def parse_transcript_file(path):
    """
    Универсальный парсер для .srt и faster-whisper-like файлов.
    Возвращает список словарей: {'start': float, 'end': float, 'text': str}
    """
    entries = []
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    buffer = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        buffer.append(line)

    i = 0
    while i < len(buffer):
        line = buffer[i]

        # .srt формат: "00:00:01,000 --> 00:00:03,000"
        srt_match = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})', line)
        if srt_match and i + 1 < len(buffer):
            start = int(srt_match[1]) * 3600 + int(srt_match[2]) * 60 + int(srt_match[3]) + int(srt_match[4]) / 1000
            end = int(srt_match[5]) * 3600 + int(srt_match[6]) * 60 + int(srt_match[7]) + int(srt_match[8]) / 1000
            text = buffer[i + 1]
            entries.append({'start': start, 'end': end, 'text': text})
            i += 3
            continue

        # Faster-whisper формат: "[00:00.000 --> 00:05.060] Текст..."
        fw_match = re.match(r'\[(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d{2}):(\d{2})\.(\d{3})\]\s+(.*)', line)
        if fw_match:
            start = int(fw_match[1]) * 60 + int(fw_match[2]) + int(fw_match[3]) / 1000
            end = int(fw_match[4]) * 60 + int(fw_match[5]) + int(fw_match[6]) / 1000
            text = fw_match[7].strip()
            entries.append({'start': start, 'end': end, 'text': text})
        i += 1

    return entries

def scan_and_populate_database(base_path: str, user_folder: str):
    session = Session()
    new_records = 0
    for root, dirs, files in os.walk(base_path):
        for file in files:
            if not file.endswith(".mp3"):
                continue
            try:
                # Ожидаем формат файла: 2025-02-20_18-55.mp3
                date_part = file.rsplit('.', 1)[0]
                dt = datetime.strptime(date_part, "%Y-%m-%d_%H-%M")
            except ValueError:
                continue

            file_path = os.path.join(root, file)
            case_number = os.path.basename(os.path.dirname(file_path))

            # Проверка, есть ли уже такая запись
            exists = session.query(AudioRecord).filter_by(file_path=file_path).first()
            if exists:
                continue

            record = AudioRecord(
                user_folder=user_folder,
                case_number=case_number,
                audio_date=dt,
                recognize_text=True,
                file_path=file_path,
                comment='(добавлено через сканирование)',
                courtroom=None
            )
            session.add(record)
            new_records += 1

    session.commit()
    session.close()
    return new_records
