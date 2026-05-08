import traceback

from PySide2.QtCore import Signal, Qt, QTranslator, QLocale, QLibraryInfo, QCoreApplication
from PySide2.QtGui import QIcon
import time
from backend import create_app, config
from backend.utils import save_config, get_all_public_ips, cleanup_old_mp3_files, version, TEMP_MP3_FOLDER
from backend.recognition_orchestrator import get_asr_executable_path, has_asr_executable, run_orchestrator_loop
from waitress import create_server
import subprocess
from string import Template
import os
import threading
import psutil
from PySide2.QtWidgets import (QStyleFactory,
    QMainWindow, QApplication, QPushButton, QLabel, QVBoxLayout, QWidget, QLineEdit, QFormLayout, QMessageBox,
    QComboBox, QSystemTrayIcon, QMenu, QAction, QCheckBox, QListWidget, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QFileDialog, QTextEdit, QAbstractItemView)
import sys
import socket
from backend.backup_service import BackupSettingsWindow


# .venv/Scripts/pyinstaller.exe --windowed --noupx --noconfirm --contents-directory "." --icon "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\cwaa-icon.ico" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\cwaa-icon.ico;." --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\assets;assets" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\frontend;frontend" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\nginx;nginx" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\ffmpeg.exe;." --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\ffprobe.exe;." "CWAA Server.py"
if getattr(sys, 'frozen', False):
    sys.stdout = open('console_output.log', 'a', buffering=1)
    sys.stderr = open('console_errors.log', 'a', buffering=1)

os.makedirs('logs', exist_ok=True)
os.makedirs('temp', exist_ok=True)
os.makedirs('backend', exist_ok=True)

# Nginx configuration template
nginx_config_template = """
pid "${nginx_pid}";
error_log "${nginx_error_log}";

events {
    worker_connections 1024;
}

http {
    access_log "${nginx_access_log}";
    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;

    server {
        listen ${server_ip}:${external_port};
        server_name _;
        client_max_body_size 500M;

        location /static/ {
            alias "${static_root}/";
            expires 1d;
            add_header Cache-Control "public";
        }

        location /protected_public_audio/ {
            internal;
            alias "${public_audio_root}/";
            types { audio/mpeg mp3; }
            add_header Accept-Ranges bytes;
        }

        location /protected_closed_audio/ {
            internal;
            alias "${closed_audio_root}/";
            types { audio/mpeg mp3; }
            add_header Accept-Ranges bytes;
        }

        location /protected_temp_mp3/ {
            internal;
            alias "${temp_mp3_root}/";
            types { audio/mpeg mp3; }
            add_header Accept-Ranges bytes;
        }

        location / {
            proxy_pass http://127.0.0.1:${internal_port};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_buffering off;
            proxy_read_timeout 600s;
            proxy_connect_timeout 600s;
            proxy_send_timeout 600s;
        }
    }
}
"""


def _nginx_path(path):
    return os.path.abspath(path).replace('\\', '/')


def _is_port_free(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return False
    except OSError:
        return True


def _find_internal_port(external_port):
    try:
        preferred = int(external_port) + 10000
    except Exception:
        preferred = 14446
    candidates = []
    if 1 <= preferred <= 65535:
        candidates.append(preferred)
    candidates.extend(range(14000, 15000))
    for port in candidates:
        if _is_port_free('127.0.0.1', port):
            return port
    raise RuntimeError("Не найден свободный внутренний порт для Waitress")


def _http_health_check(host, port, path="/healthz", request_timeout=0.25):
    with socket.create_connection((host, int(port)), timeout=request_timeout) as sock:
        sock.settimeout(request_timeout)
        request = (
            f"GET {path} HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = sock.recv(128)
    return response.startswith(b"HTTP/1.") and b" 200 " in response[:32]


def _wait_for_http(host, port, timeout=8, request_timeout=0.25, interval=0.1, path="/healthz"):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            if _http_health_check(host, port, path=path, request_timeout=request_timeout):
                return True, ""
        except Exception as e:
            last_error = e
        time.sleep(interval)
    return False, str(last_error or "нет ответа")


def generate_nginx_config(server_ip, external_port, internal_port):
    try:
        public_audio_root = config.get('public_audio_path') or os.getcwd()
        closed_audio_root = config.get('closed_audio_path') or os.getcwd()
        nginx_config = Template(nginx_config_template).safe_substitute(
            server_ip=server_ip,
            external_port=external_port,
            internal_port=internal_port,
            static_root=_nginx_path(os.path.join(os.getcwd(), 'frontend', 'assets')),
            public_audio_root=_nginx_path(public_audio_root),
            closed_audio_root=_nginx_path(closed_audio_root),
            temp_mp3_root=_nginx_path(TEMP_MP3_FOLDER),
            nginx_pid=_nginx_path(os.path.join(os.getcwd(), 'logs', 'nginx.pid')),
            nginx_error_log=_nginx_path(os.path.join(os.getcwd(), 'logs', 'nginx_error.log')),
            nginx_access_log=_nginx_path(os.path.join(os.getcwd(), 'logs', 'nginx_access.log')),
        )
        config_path = 'nginx_dynamic.conf'
        if os.path.exists(config_path):
            os.remove(config_path)
        with open(config_path, 'w') as f:
            f.write(nginx_config)
        return 0, ''
    except Exception as e:
        return 1, 'generate_nginx_config:'+str(e)


def start_nginx():
    try:
        nginx_exe = os.path.join('nginx', 'nginx.exe')
        if not os.path.exists(nginx_exe):
            return 1, f"start_nginx: не найден {nginx_exe}"
        stage_time = time.perf_counter()
        test = subprocess.run(
            [nginx_exe, '-t', '-c', os.path.abspath('nginx_dynamic.conf')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        print(f"[startup] nginx -t: {time.perf_counter() - stage_time:.2f}s")
        if test.returncode != 0:
            print(f"start_nginx -t: {test.stderr or test.stdout}")
            return 1, "start_nginx: nginx_dynamic.conf не прошел проверку"
        stdout = open(os.path.join('logs', 'nginx_stdout.log'), 'a', buffering=1)
        stderr = open(os.path.join('logs', 'nginx_stderr.log'), 'a', buffering=1)
        stage_time = time.perf_counter()
        proc = subprocess.Popen(
            [nginx_exe, '-c', os.path.abspath('nginx_dynamic.conf')],
            stdout=stdout, stderr=stderr
        )
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        print(f"[startup] nginx process start: {time.perf_counter() - stage_time:.2f}s")
        if proc.poll() is not None:
            print("start_nginx: процесс nginx завершился сразу после запуска")
            return 1, "start_nginx: см. logs/nginx_stderr.log"
        return 0, "", proc
    except Exception as e:
        return 1, f"start_nginx: {str(e)}"


def stop_nginx(proc):
    if not proc:
        return
    try:
        nginx_proc = psutil.Process(proc.pid)
    except Exception:
        print('Процесс nginx не найден', getattr(proc, 'pid', None))
        return
    for child in nginx_proc.children(recursive=True):
        try:
            child.terminate()
            child.wait(timeout=3)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            child.kill()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


class ServerManager:
    def __init__(self):
        self.threads = {}
        self.stop_event = threading.Event()
        self.nginx_process = None
        self.waitress_server = None
        self.flask_app = None
        self.flask_thread = None
        self.service_status = "stopped"
        self.last_exit_reason = None
        self.internal_port = None

    def _bootstrap_app(self):
        app, message = create_app()
        if not app:
            raise RuntimeError(message)
        return app

    def _start_waitress(self, app, internal_port):
        server = create_server(
            app,
            host='127.0.0.1',
            port=internal_port,
            threads=12,
            connection_limit=300,
            channel_timeout=600,
        )
        self.waitress_server = server

        def run_server():
            try:
                server.run()
            except Exception as e:
                if not self.stop_event.is_set():
                    traceback.print_exc()
                    self.last_exit_reason = f"waitress: {e}"

        th = threading.Thread(target=run_server, daemon=True, name="waitress")
        th.start()
        self.flask_thread = th
        self.threads['waitress'] = th

    def _start_thread(self, key, target, args=()):
        th = threading.Thread(target=target, args=args, daemon=True, name=key)
        th.start()
        self.threads[key] = th
        return th

    def start(self, server_ip, external_port):
        start_time = time.perf_counter()
        self.service_status = "starting"
        self.last_exit_reason = None
        self.stop_event = threading.Event()
        self.threads = {}
        external_port = int(external_port)
        internal_port = _find_internal_port(external_port)
        self.internal_port = internal_port
        try:
            stage_time = time.perf_counter()
            app = self._bootstrap_app()
            print(f"[startup] Flask bootstrap: {time.perf_counter() - stage_time:.2f}s")
            self.flask_app = app
            print("Flask App создан:", app)
            stage_time = time.perf_counter()
            self._start_waitress(app, internal_port)
            ok, detail = _wait_for_http("127.0.0.1", internal_port, timeout=8)
            if not ok:
                raise RuntimeError(f"Waitress не отвечает на health-check: {detail}")
            print(f"[startup] Waitress health-check: {time.perf_counter() - stage_time:.2f}s")

            stage_time = time.perf_counter()
            res, msg = generate_nginx_config(server_ip, external_port, internal_port)
            if res:
                raise RuntimeError(msg)
            result = start_nginx()
            if result[0]:
                raise RuntimeError(result[1])
            self.nginx_process = result[2]
            nginx_health_time = time.perf_counter()
            ok, detail = _wait_for_http(server_ip, external_port, timeout=8)
            if not ok:
                raise RuntimeError(f"Nginx не отвечает на health-check: {detail}")
            print(f"[startup] Nginx health-check: {time.perf_counter() - nginx_health_time:.2f}s")
            print(f"[startup] Nginx config/start/health-check: {time.perf_counter() - stage_time:.2f}s")

            stage_time = time.perf_counter()
            recognize_path = config.get('recognize_text_from_audio_path')
            recognition_enabled = config.get('recognize_text_enabled', 'true') == 'true'
            if recognition_enabled and recognize_path and os.path.exists(recognize_path) and has_asr_executable():
                self._start_thread('orchestrator', run_orchestrator_loop, (self.stop_event,))
            elif recognition_enabled and not has_asr_executable():
                print(f"[ASR] Не найден исполняемый файл: {get_asr_executable_path()}. Поток распознавания не запущен.")
            else:
                print('Папка распознавания не настроена — оркестратор распознавания отключен.')
            self._start_thread('cleanup_mp3', cleanup_old_mp3_files, (self.stop_event,))
            print(f"[startup] Background services: {time.perf_counter() - stage_time:.2f}s")
            self.service_status = "running"
            print(f"[startup] Total: {time.perf_counter() - start_time:.2f}s")
            return True, ""
        except Exception as e:
            traceback.print_exc()
            self.last_exit_reason = str(e)
            self.stop()
            self.service_status = "failed"
            return False, str(e)

    def stop(self):
        if self.service_status == "stopped":
            return
        self.service_status = "stopping"
        self.stop_event.set()
        stop_nginx(self.nginx_process)
        self.nginx_process = None
        if self.waitress_server:
            try:
                self.waitress_server.close()
            except Exception as e:
                print(f"Ошибка при остановке Waitress: {e}")
        for key, thread in list(self.threads.items()):
            if thread and thread.is_alive():
                try:
                    thread.join(timeout=5)
                except Exception as e:
                    print(f"Ошибка при завершении потока {key}: {e}")
        self.waitress_server = None
        self.flask_thread = None
        self.flask_app = None
        self.internal_port = None
        self.service_status = "stopped"

    def thread(self, key):
        return self.threads.get(key)


class MainWindow(QMainWindow):
    nginx_error_signal = Signal(str)
    service_start_finished = Signal(bool, str)
    service_stop_finished = Signal()

    def __init__(self):
        super().__init__()
        self.manager = ServerManager()
        self.backup_window = None
        self.path_migration_window = None
        self.duplicate_resolver_window = None
        self.monitor_thread = None
        self.orchestrator_thread = None
        self.cleanup_thread = None
        self.stop_threads_event = threading.Event()
        self.setWindowTitle(f"Сервер CWAA, версия {version}")
        self.setMinimumSize(500, 400)
        self.setWindowIcon(QIcon('cwaa-icon.ico'))

        # Статус сервера и кнопки
        self.status_label = QLabel("Статус: Остановлен", self)
        self.start_button = QPushButton("🚀 Запустить сервер", self)
        self.stop_button = QPushButton("Остановить сервер", self)
        self.stop_button.setEnabled(False)
        self.app_link = QLabel()
        self.update_app_link()
        self.app_link.setOpenExternalLinks(True)
        self.nginx_error_signal.connect(self.signal_error)
        self.service_start_finished.connect(self.on_service_start_finished)
        self.service_stop_finished.connect(self.on_service_stop_finished)

        # Настройки формы
        form_layout = QFormLayout()
        self.server_ip_combo = QComboBox()
        available_ips = get_all_public_ips()
        self.server_ip_combo.addItems(available_ips)
        if config['server_ip'] in available_ips:
            self.server_ip_combo.setCurrentText(config['server_ip'])
        self.server_port_input = QLineEdit(str(config['server_port']))
        self.public_audio_path_input = QLineEdit(config['public_audio_path'])
        self.public_audio_path_input.setPlaceholderText('C:\\папка\\еще папка')
        self.closed_audio_path_input = QLineEdit(config['closed_audio_path'])
        self.closed_audio_path_input.setPlaceholderText('C:\\папка\\еще папка')
        self.recognize_text_from_audio_path_input = QLineEdit(config['recognize_text_from_audio_path'])
        self.recognize_text_from_audio_path_input.setPlaceholderText('C:\\папка\\еще папка')
        self.recognize_text_enabled = QCheckBox()
        self.recognize_text_enabled.setChecked(config.get('recognize_text_enabled', 'true') == 'true')
        self.recognize_text_enabled.setToolTip(
            'Показывает чекбокс распознавания в форме архивации и включает очередь распознавания в архиве.'
        )
        self.recognize_text_default = QCheckBox()
        self.recognize_text_default.setChecked(config.get('recognize_text_default', 'false') == 'true')
        self.recognize_text_default.setToolTip(
            'Если распознавание включено, чекбокс "Перевести аудио в текст" на странице архивации будет отмечен заранее.'
        )
        self.create_year_subfolders = QCheckBox()
        self.create_year_subfolders.setChecked(config['create_year_subfolders']=='true')
        form_layout.addRow("Выберите IP для размещения сервера:", self.server_ip_combo)
        form_layout.addRow("Введите номер порта для размещения сервера:", self.server_port_input)
        form_layout.addRow('Создавать подпапки по годам в папках судей', self.create_year_subfolders)
        form_layout.addRow('Распознавать записи в текст', self.recognize_text_enabled)
        form_layout.addRow('Распознавать записи по умолчанию', self.recognize_text_default)
        form_layout.addRow("Путь хранения открытых аудиопротоколов:", self.public_audio_path_input)
        form_layout.addRow("Путь хранения закрытых аудиопротоколов:", self.closed_audio_path_input)
        form_layout.addRow('Путь для распознавания аудиопротоколов:', self.recognize_text_from_audio_path_input)
        # Кнопка сохранения настроек
        self.save_button = QPushButton("💾 Сохранить настройки")
        self.save_button.clicked.connect(self.save_config)

        self.firewall_button = QPushButton(
            "🛡 Создать правило в брандмауэре для указанного порта\nТребуется запуск от имени администратора")
        self.firewall_button.clicked.connect(self.create_firewall_rule)
        self.scan_button = QPushButton("📂 Восстановить файл базы из директории")
        self.scan_button.clicked.connect(self.scan_archives)
        if "-restore_base_from_dir" not in sys.argv:
            self.scan_button.setDisabled(True)
            self.scan_button.setToolTip('Запустите с параметром -restore_base_from_dir чтобы создать новую базу из имеющегося аудиоархива')

        self.backup_button = QPushButton("🛠 Параметры резервного копирования")
        self.backup_button.clicked.connect(self.open_backup_settings)

        self.courtroom_button = QPushButton("🏛 Управление залами")
        self.courtroom_button.clicked.connect(self.open_courtroom_manager)

        self.path_migration_button = QPushButton("🧭 Миграция путей аудиоархива")
        self.path_migration_button.clicked.connect(self.open_path_migration)
        self.duplicate_resolver_button = QPushButton("🧩 Разрешение дублей записей")
        self.duplicate_resolver_button.clicked.connect(self.open_duplicate_resolver)

        # Основной layout
        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.app_link)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        layout.addLayout(form_layout)
        layout.addWidget(self.firewall_button)
        layout.addWidget(self.scan_button)
        layout.addWidget(self.backup_button)
        layout.addWidget(self.courtroom_button)
        layout.addWidget(self.path_migration_button)
        layout.addWidget(self.duplicate_resolver_button)
        layout.addWidget(self.save_button)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.start_button.clicked.connect(self.start_server)
        self.stop_button.clicked.connect(self.stop_server)

        self.tray_icon = QSystemTrayIcon(self)
        self.update_tray_icon("yellow")  # Изначально остановлен
        self.tray_icon.setVisible(True)

        tray_menu = QMenu()
        self.start_action = QAction("Запустить сервер", self)
        self.start_action.triggered.connect(self.start_server)
        tray_menu.addAction(self.start_action)

        self.stop_action = QAction("Остановить сервер", self)
        self.stop_action.triggered.connect(self.stop_server)
        self.stop_action.setEnabled(False)
        tray_menu.addAction(self.stop_action)

        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self.exit_application)
        tray_menu.addAction(exit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_click)

        if '-autostart' in sys.argv:
            self.start_action.trigger()
        else:
            self.show()

    def open_backup_settings(self):
        if not self.backup_window:
            self.backup_window = BackupSettingsWindow()
        self.backup_window.show()

    def open_courtroom_manager(self):
        if not hasattr(self, 'courtroom_window') or self.courtroom_window is None:
            self.courtroom_window = CourtroomManagerWindow()
        self.courtroom_window.show()

    def open_path_migration(self):
        if not self.path_migration_window:
            self.path_migration_window = PathMigrationWindow()
        self.path_migration_window.show()

    def open_duplicate_resolver(self):
        if not self.duplicate_resolver_window:
            self.duplicate_resolver_window = DuplicateResolverWindow()
        self.duplicate_resolver_window.show()

    def start_server(self):
        try:
            if self.manager.service_status in ("starting", "running"):
                return
            self.save_config()
            if not self.backup_window:
                self.backup_window = BackupSettingsWindow()
            server_ip = self.server_ip_combo.currentText()
            server_port = int(self.server_port_input.text())
            self.set_starting_state()

            def run_start():
                ok, message = self.manager.start(server_ip, server_port)
                self.service_start_finished.emit(ok, message)

            threading.Thread(target=run_start, daemon=True, name='service_start').start()
        except Exception as e:
            self.update_tray_icon("red")
            self.show_error_message(str(e))

    def stop_server(self):
        if self.manager.service_status in ("stopped", "stopping"):
            return
        self.set_stopping_state()

        def run_stop():
            self.manager.stop()
            if self.monitor_thread and self.monitor_thread.is_alive():
                try:
                    self.monitor_thread.join(timeout=5)
                except Exception as e:
                    print(f"Ошибка при завершении потока мониторинга: {e}")
            self.service_stop_finished.emit()

        threading.Thread(target=run_stop, daemon=True, name='service_stop').start()

    def on_service_start_finished(self, ok, message):
        self.stop_threads_event = self.manager.stop_event
        if not ok:
            self.signal_error(message or 'Ошибка запуска')
            self.show_error_message(message or 'Ошибка запуска')
            return
        self.orchestrator_thread = self.manager.thread('orchestrator')
        self.cleanup_thread = self.manager.thread('cleanup_mp3')
        self.monitor_thread = threading.Thread(
            target=self.monitor_services,
            args=(self.nginx_error_signal, self.stop_threads_event),
            daemon=True,
            name='monitor_services'
        )
        self.monitor_thread.start()
        if self.monitor_thread.is_alive():
            print('Мониторинг процессов запущен.')
        self.set_running_state()

    def on_service_stop_finished(self):
        self.orchestrator_thread = None
        self.cleanup_thread = None
        self.monitor_thread = None
        self.set_stopped_state()

    def set_starting_state(self):
        self.update_status("Запускается...")
        self.update_tray_icon("yellow")
        self.start_button.setText("Запуск...")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.server_ip_combo.setEnabled(False)
        self.server_port_input.setEnabled(False)
        self.public_audio_path_input.setEnabled(False)
        self.closed_audio_path_input.setEnabled(False)
        self.save_button.setEnabled(False)
        self.start_action.setEnabled(False)
        self.stop_action.setEnabled(False)
        self.firewall_button.setEnabled(False)
        self.create_year_subfolders.setEnabled(False)
        self.recognize_text_enabled.setEnabled(False)
        self.recognize_text_default.setEnabled(False)
        self.recognize_text_from_audio_path_input.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.backup_button.setEnabled(False)
        self.courtroom_button.setEnabled(False)
        self.path_migration_button.setEnabled(False)
        self.duplicate_resolver_button.setEnabled(False)

    def set_running_state(self):
        self.update_status("Запущен")
        self.update_tray_icon("green")
        self.start_button.setText("🚀 Запустить сервер")
        self.stop_button.setText("Остановить сервер")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.start_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.server_ip_combo.setEnabled(False)
        self.server_port_input.setEnabled(False)
        self.public_audio_path_input.setEnabled(False)
        self.closed_audio_path_input.setEnabled(False)
        self.save_button.setEnabled(False)
        self.firewall_button.setEnabled(False)
        self.create_year_subfolders.setEnabled(False)
        self.recognize_text_enabled.setEnabled(False)
        self.recognize_text_default.setEnabled(False)
        self.recognize_text_from_audio_path_input.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.backup_button.setEnabled(False)
        self.courtroom_button.setEnabled(False)
        self.path_migration_button.setEnabled(False)
        self.duplicate_resolver_button.setEnabled(False)

    def set_stopping_state(self):
        self.update_status("Останавливается...")
        self.update_tray_icon("yellow")
        self.stop_button.setText("Остановка...")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.start_action.setEnabled(False)
        self.stop_action.setEnabled(False)

    def set_stopped_state(self):
        self.update_status("Остановлен")
        self.update_tray_icon("yellow")
        self.start_button.setText("🚀 Запустить сервер")
        self.stop_button.setText("Остановить сервер")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.server_ip_combo.setEnabled(True)
        self.server_port_input.setEnabled(True)
        self.public_audio_path_input.setEnabled(True)
        self.closed_audio_path_input.setEnabled(True)
        self.save_button.setEnabled(True)
        self.start_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.firewall_button.setEnabled(True)
        self.create_year_subfolders.setEnabled(True)
        self.recognize_text_enabled.setEnabled(True)
        self.recognize_text_default.setEnabled(True)
        self.recognize_text_from_audio_path_input.setEnabled(True)
        self.scan_button.setEnabled("-restore_base_from_dir" in sys.argv)
        self.backup_button.setEnabled(True)
        self.courtroom_button.setEnabled(True)
        self.path_migration_button.setEnabled(True)
        self.duplicate_resolver_button.setEnabled(True)

    def monitor_services(self, signal_error, stop_event):
        """Функция для отслеживания процессов nginx и flask"""
        while not stop_event.is_set():
            if self.manager.nginx_process:
                try:
                    if not psutil.pid_exists(self.manager.nginx_process.pid):
                        raise psutil.NoSuchProcess(self.manager.nginx_process.pid)
                    nginx_proc = psutil.Process(self.manager.nginx_process.pid)
                    for child in nginx_proc.children(recursive=True):
                        if not child.is_running():
                            raise psutil.NoSuchProcess(child.pid)
                except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                    signal_error.emit("Nginx умер")  # Ошибка Nginx
                    break
            if self.manager.flask_thread and not self.manager.flask_thread.is_alive():
                reason = self.manager.last_exit_reason or "неизвестно"
                signal_error.emit(f'Flask умер: {reason}')  # Ошибка Flask
                break
            if self.orchestrator_thread and not self.orchestrator_thread.is_alive():
                signal_error.emit('Оркестратор распознавания умер')
                break
            if self.cleanup_thread and not self.cleanup_thread.is_alive():
                signal_error.emit('Очистка временных mp3 умерла')
                break
            for i in range(10):
                time.sleep(1)
                if stop_event.is_set():
                    break

    def update_status(self, status):
        self.status_label.setText(f"Статус: {status}")

    def update_app_link(self):
        url = f"http://{config['server_ip']}:{config['server_port']}"
        self.app_link.setText(f"<a href='{url}'>{url}</a>")

    def signal_error(self, message="Ошибка"):
        """Метод, вызываемый при обнаружении ошибки для обновления статуса."""
        if self.manager.service_status == "running":
            self.update_status("Останавливается после ошибки...")
            self.update_tray_icon("yellow")
            self.manager.stop()
        self.update_status(message)
        self.update_tray_icon("red")
        self.start_button.setText("🚀 Запустить сервер")
        self.stop_button.setText("Остановить сервер")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.server_ip_combo.setEnabled(True)
        self.server_port_input.setEnabled(True)
        self.public_audio_path_input.setEnabled(True)
        self.closed_audio_path_input.setEnabled(True)
        self.save_button.setEnabled(True)
        self.start_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.firewall_button.setEnabled(True)
        self.create_year_subfolders.setEnabled(True)
        self.recognize_text_enabled.setEnabled(True)
        self.recognize_text_default.setEnabled(True)
        self.recognize_text_from_audio_path_input.setEnabled(True)
        self.scan_button.setEnabled("-restore_base_from_dir" in sys.argv)
        self.backup_button.setEnabled(True)
        self.courtroom_button.setEnabled(True)
        self.path_migration_button.setEnabled(True)
        self.duplicate_resolver_button.setEnabled(True)

    def save_config(self):
        global config
        config['server_ip'] = self.server_ip_combo.currentText()
        try:
            server_port = int(self.server_port_input.text())
        except ValueError:
            raise RuntimeError("Порт сервера должен быть числом")
        if not 1 <= server_port <= 65535:
            raise RuntimeError("Порт сервера должен быть в диапазоне 1-65535")
        config['server_port'] = server_port
        public_audio_path = self.public_audio_path_input.text().replace('"', '')
        closed_audio_path = self.closed_audio_path_input.text().replace('"', '')
        try:
            os.makedirs(public_audio_path, exist_ok=True)
            os.makedirs(closed_audio_path, exist_ok=True)
        except Exception as e:
            self.show_error_message(f"Не удалось создать директории: {e}")
        config['public_audio_path'] = public_audio_path
        config['closed_audio_path'] = closed_audio_path
        config['recognize_text_from_audio_path'] = self.recognize_text_from_audio_path_input.text().replace('"', '')
        config['recognize_text_enabled'] = "true" if self.recognize_text_enabled.isChecked() else 'false'
        config['recognize_text_default'] = "true" if self.recognize_text_default.isChecked() else 'false'
        config['create_year_subfolders'] = "true" if self.create_year_subfolders.isChecked() else 'false'
        save_config(config)
        self.update_app_link()

    def show_error_message(self, message):
        error_dialog = QMessageBox(self)
        error_dialog.setWindowTitle("Ошибка")
        error_dialog.setText("Произошла ошибка при запуске сервера.")
        error_dialog.setDetailedText(message)
        error_dialog.setIcon(QMessageBox.Critical)
        error_dialog.exec_()

    def scan_archives(self):
        from backend.utils import scan_and_populate_database
        count = 0
        for folder in os.listdir(config['public_audio_path']):
            user_path = os.path.join(config['public_audio_path'], folder)
            if os.path.isdir(user_path):
                count += scan_and_populate_database(user_path, folder)
        QMessageBox.information(self, "Сканирование завершено", f"Добавлено новых записей: {count}")

    def update_tray_icon(self, status_color):
        icon = QIcon(f"assets/cwaa-icon-{status_color}.png")
        self.tray_icon.setIcon(icon)

    def on_tray_icon_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # ЛКМ по иконке
            self.showNormal()
            self.activateWindow()

    def create_firewall_rule(self):
        port = self.server_port_input.text()
        try:
            subprocess.run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name=CWAA Server Port {port}",
                f"dir=in", "action=allow",
                "protocol=TCP", f"localport={port}"
            ], check=True, shell=True)
            QMessageBox.information(self, "Успех", f"Правило для порта {port} успешно создано в брандмауэре.")
        except subprocess.CalledProcessError as e:
            self.show_error_message(f"Не удалось создать правило брандмауэра для порта {port}. Подробности: {e}")

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def exit_application(self):
        self.manager.stop()
        sys.exit(0)


class PathMigrationWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Миграция путей аудиоархива")
        self.setMinimumSize(620, 420)

        self.summary_label = QLabel(
            "Проверка найдет абсолютные пути в базе, которые находятся внутри текущих "
            "public_audio_path/closed_audio_path, и может заменить их на относительные."
        )
        self.output = QTextEdit()
        self.output.setReadOnly(True)

        self.check_button = QPushButton("Проверить без изменений")
        self.migrate_button = QPushButton("Выполнить миграцию")
        self.close_button = QPushButton("Закрыть")

        buttons = QHBoxLayout()
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.migrate_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout()
        layout.addWidget(self.summary_label)
        layout.addWidget(self.output)
        layout.addLayout(buttons)
        self.setLayout(layout)

        self.check_button.clicked.connect(self.check_paths)
        self.migrate_button.clicked.connect(self.migrate_paths)
        self.close_button.clicked.connect(self.close)

    def _format_result(self, result, dry_run):
        mode = "Проверка" if dry_run else "Миграция"
        lines = [
            f"{mode} завершена.",
            f"Всего записей: {result['total']}",
            f"Записей к изменению: {result['updated']}",
            f"Аудиопутей к переводу: {result['file_path_candidates']}",
            f"Путей текста к переводу: {result['text_path_candidates']}",
            f"Пропущено: {result['skipped']}",
        ]
        if result.get('backup_path'):
            lines.append(f"Бэкап базы перед миграцией: {result['backup_path']}")
        if result['errors']:
            lines.append("")
            lines.append("Ошибки:")
            lines.extend(result['errors'])
        if dry_run and not result['errors']:
            lines.append("")
            lines.append("Изменения не записаны. Для применения нажмите \"Выполнить миграцию\".")
        return "\n".join(lines)

    def _run_migration(self, dry_run):
        from backend.path_resolver import migrate_absolute_paths_to_relative
        self.check_button.setEnabled(False)
        self.migrate_button.setEnabled(False)
        try:
            result = migrate_absolute_paths_to_relative(dry_run=dry_run)
            self.output.setPlainText(self._format_result(result, dry_run))
            if result['errors']:
                QMessageBox.warning(self, "Миграция путей", "Миграция завершилась с ошибками.")
            elif not dry_run:
                QMessageBox.information(self, "Миграция путей", "Пути успешно обновлены.")
        finally:
            self.check_button.setEnabled(True)
            self.migrate_button.setEnabled(True)

    def check_paths(self):
        self._run_migration(dry_run=True)

    def migrate_paths(self):
        answer = QMessageBox.question(
            self,
            "Подтверждение миграции",
            "Заменить подходящие абсолютные пути в базе на относительные?",
            QMessageBox.Yes | QMessageBox.No
        )
        if answer == QMessageBox.Yes:
            self._run_migration(dry_run=False)


class DuplicateResolverWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Разрешение дублей записей")
        self.setMinimumSize(1050, 620)
        self.conflicts = []
        self.current_conflict = None

        layout = QVBoxLayout()
        layout.addWidget(QLabel(
            "Выберите конфликт, затем строку записи, которую нужно оставить. "
            "Перед удалением дублей автоматически создается backup базы."
        ))

        content = QHBoxLayout()
        self.conflict_list = QListWidget()
        self.conflict_list.setMinimumWidth(330)
        content.addWidget(self.conflict_list)

        right = QVBoxLayout()
        self.details_table = QTableWidget(0, 10)
        self.details_table.setHorizontalHeaderLabels([
            "ID", "Судья", "Дело", "Дата", "Зал", "Расп.", "Текст", "Комментарий", "Путь аудио", "IP"
        ])
        self.details_table.horizontalHeader().setStretchLastSection(True)
        self.details_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.details_table.setSelectionMode(QAbstractItemView.SingleSelection)
        right.addWidget(self.details_table)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Обновить список")
        self.keep_button = QPushButton("Оставить выбранную, удалить остальные")
        self.merge_button = QPushButton("Объединить в выбранную и удалить остальные")
        self.skip_button = QPushButton("Пропустить конфликт")
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.keep_button)
        actions.addWidget(self.merge_button)
        actions.addWidget(self.skip_button)
        right.addLayout(actions)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(130)
        right.addWidget(self.output)

        content.addLayout(right)
        layout.addLayout(content)
        self.setLayout(layout)

        self.refresh_button.clicked.connect(self.load_conflicts)
        self.conflict_list.currentRowChanged.connect(self.show_conflict)
        self.keep_button.clicked.connect(lambda: self.resolve_current(merge=False))
        self.merge_button.clicked.connect(lambda: self.resolve_current(merge=True))
        self.skip_button.clicked.connect(self.skip_current)

        self.load_conflicts()

    def load_conflicts(self):
        from backend.duplicate_resolver import find_duplicate_conflicts
        self.conflicts = find_duplicate_conflicts()
        self.conflict_list.clear()
        self.details_table.setRowCount(0)
        self.current_conflict = None
        for conflict in self.conflicts:
            self.conflict_list.addItem(
                f"{conflict['index']}. {conflict['title']} [{', '.join(map(str, conflict['ids']))}]"
            )
        self.output.setPlainText(f"Найдено конфликтов: {len(self.conflicts)}")
        if self.conflicts:
            self.conflict_list.setCurrentRow(0)

    def show_conflict(self, row):
        self.details_table.setRowCount(0)
        if row < 0 or row >= len(self.conflicts):
            self.current_conflict = None
            return
        self.current_conflict = self.conflicts[row]
        records = self.current_conflict['records']
        self.details_table.setRowCount(len(records))
        for table_row, record in enumerate(records):
            values = [
                record['id'],
                record['user_folder'],
                record['case_number'],
                record['audio_date'],
                record['courtroom'],
                "да" if record['recognize_text'] else "нет",
                record['recognized_text_path'],
                record['comment'],
                record['file_path'],
                record['uploaded_ip'],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                self.details_table.setItem(table_row, col, item)
        self.details_table.resizeColumnsToContents()
        if records:
            self.details_table.selectRow(0)

    def _selected_keep_id(self):
        row = self.details_table.currentRow()
        if row < 0:
            return None
        item = self.details_table.item(row, 0)
        return int(item.text()) if item else None

    def resolve_current(self, merge):
        if not self.current_conflict:
            return
        keep_id = self._selected_keep_id()
        if not keep_id:
            QMessageBox.warning(self, "Разрешение дублей", "Выберите запись, которую нужно оставить.")
            return
        action = "объединить выбранную запись с остальными и удалить дубли" if merge else "оставить выбранную запись и удалить остальные"
        answer = QMessageBox.question(
            self,
            "Подтверждение",
            f"ID={keep_id}: {action}?",
            QMessageBox.Yes | QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            return
        from backend.duplicate_resolver import resolve_duplicate_conflict
        try:
            result = resolve_duplicate_conflict(self.current_conflict['ids'], keep_id, merge=merge)
            self.output.setPlainText(
                f"Конфликт разрешен.\n"
                f"Оставлена запись: {result['keep_id']}\n"
                f"Удалены записи: {', '.join(map(str, result['deleted_ids']))}\n"
                f"Backup: {result['backup_path']}"
            )
            self.load_conflicts()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", str(exc))

    def skip_current(self):
        row = self.conflict_list.currentRow()
        if row + 1 < self.conflict_list.count():
            self.conflict_list.setCurrentRow(row + 1)


class CourtroomManagerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Управление залами")
        self.setMinimumSize(500, 500)

        layout = QVBoxLayout()
        import_label = QLabel("🔁 Залы для сохранения аудиозаписей:")
        layout.addWidget(import_label)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        cr_btns = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText('Название зала')
        layout.addWidget(self.input_field)
        self.add_button = QPushButton("➕ Добавить")
        self.add_button.clicked.connect(self.add_courtroom)
        self.delete_button = QPushButton("🗑 Удалить выбранное")
        self.delete_button.clicked.connect(self.delete_selected)
        cr_btns.addWidget(self.add_button)
        cr_btns.addWidget(self.delete_button)
        layout.addLayout(cr_btns)

        import_label = QLabel("🔁 Залы для импорта аудиозаписей:")
        layout.addWidget(import_label)

        self.import_table = QTableWidget(0, 2)
        self.import_table.setHorizontalHeaderLabels(["Зал", "Папка"])
        self.import_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.import_table)

        import_form = QHBoxLayout()
        self.import_name = QLineEdit()
        self.import_name.setPlaceholderText('Название зала')
        self.import_path = QLineEdit()
        self.import_path.setPlaceholderText('Папка с записями фемиды')
        browse_btn = QPushButton("📁")
        browse_btn.clicked.connect(self.browse_folder)
        import_form.addWidget(self.import_name)
        import_form.addWidget(self.import_path)
        import_form.addWidget(browse_btn)
        layout.addLayout(import_form)

        import_btns = QHBoxLayout()
        self.add_import_btn = QPushButton("➕ Добавить")
        self.add_import_btn.clicked.connect(self.add_import_entry)
        self.del_import_btn = QPushButton("🗑 Удалить выбранное")
        self.del_import_btn.clicked.connect(self.delete_import_entry)
        import_btns.addWidget(self.add_import_btn)
        import_btns.addWidget(self.del_import_btn)
        layout.addLayout(import_btns)

        self.save_button = QPushButton("💾 Сохранить")
        self.save_button.clicked.connect(self.save_courtrooms)
        layout.addWidget(self.save_button)
        self.setLayout(layout)
        self.load_courtrooms()
        self.load_import_sources()

    def load_courtrooms(self):
        from backend.utils import get_available_courtrooms
        self.list_widget.clear()
        for room in get_available_courtrooms():
            self.list_widget.addItem(room)

    def add_courtroom(self):
        name = self.input_field.text().strip()
        if name and name not in [self.list_widget.item(i).text() for i in range(self.list_widget.count())]:
            self.list_widget.addItem(name)
            self.input_field.clear()

    def delete_selected(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def save_courtrooms(self):
        COURTROOMS_PATH = "courtrooms.txt"
        IMPORT_PATH = 'import_sources.txt'
        courtrooms = [self.list_widget.item(i).text().strip() for i in range(self.list_widget.count()) if self.list_widget.item(i).text().strip()]
        with open(COURTROOMS_PATH, "w", encoding="utf-8") as f:
            for room in courtrooms:
                f.write(room + "\n")
        with open(IMPORT_PATH, 'w', encoding='utf-8') as f:
            for row in range(self.import_table.rowCount()):
                name = self.import_table.item(row, 0).text().strip()
                path = self.import_table.item(row, 1).text().strip()
                if name and path:
                    f.write(f"{name}|{path}\n")
        QMessageBox.information(self, "Успешно", "Список залов сохранён.")


    def load_import_sources(self):
        IMPORT_PATH = 'import_sources.txt'
        if not os.path.exists(IMPORT_PATH):
            return
        with open(IMPORT_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if '|' in line:
                    name, path = line.strip().split('|', 1)
                    self.add_import_entry(name, path)

    def add_import_entry(self, name=None, path=None):
        name = name or self.import_name.text().strip()
        path = path or self.import_path.text().strip()
        if not name or not path:
            return
        row = self.import_table.rowCount()
        self.import_table.insertRow(row)
        self.import_table.setItem(row, 0, QTableWidgetItem(name))
        self.import_table.setItem(row, 1, QTableWidgetItem(path))
        self.import_name.clear()
        self.import_path.clear()

    def delete_import_entry(self):
        selected = self.import_table.selectionModel().selectedRows()
        for index in sorted(selected, reverse=True):
            self.import_table.removeRow(index.row())

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбор папки")
        if folder:
            self.import_path.setText(folder)


if __name__ == '__main__':
    QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    translator = QTranslator()
    locale = QLocale.system().name()  # Получение системной локали
    path = QLibraryInfo.location(QLibraryInfo.TranslationsPath)  # Путь к переводам Qt
    translator.load("qtbase_" + locale, path)
    app.installTranslator(translator)
    app.setStyle(QStyleFactory.create("Fusion"))
    window = MainWindow()
    sys.exit(app.exec_())
