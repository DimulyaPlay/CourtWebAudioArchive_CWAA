import os
import shutil
import time
import hashlib
import socket
from traceback import print_exc
import subprocess

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
        "closed_audio_path": ""
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


