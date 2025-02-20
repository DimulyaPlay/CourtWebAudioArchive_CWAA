from PySide2.QtCore import Signal, Slot
from PySide2.QtGui import QIcon
import time
from backend import create_app, config
from backend.utils import save_config, get_all_public_ips, compare_files
from waitress import serve
import subprocess
from string import Template
import os
import threading
import logging
import psutil
from PySide2.QtWidgets import (
QMainWindow, QApplication, QPushButton, QLabel, QVBoxLayout, QWidget, QLineEdit, QFormLayout, QMessageBox, QComboBox, QSystemTrayIcon, QMenu, QAction)
import sys

# .venv/Scripts/pyinstaller.exe --windowed --noconfirm --contents-directory "." --icon "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\cwaa-icon.ico" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\cwaa-icon.ico;." --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\assets;assets" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\frontend;frontend" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\nginx-1.27.1;nginx-1.27.1" "CWAA Server.py"

# Initialize logging and directories
logging.basicConfig(filename='app.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
if not os.path.exists('logs'):
    os.mkdir('logs')
if not os.path.exists('temp'):
    os.mkdir('temp')
if not os.path.exists('backend'):
    os.mkdir('backend')

# Nginx configuration template
nginx_config_template = """
events {
    worker_connections 1024;
}

http {
    server {
        listen ${app_port};
        server_name ${server_ip}${app_port};
        client_max_body_size 200M;

        location / {
            proxy_pass http://127.0.0.1:${app_port};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
"""

nginx_process = None
flask_thread = None
flask_app = None

def generate_nginx_config(config):
    try:
        nginx_config = Template(nginx_config_template).safe_substitute(
            server_ip=config['server_ip'],
            app_port=config['server_port']
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
    global nginx_process
    try:
        nginx_process = subprocess.Popen(
            ['nginx-1.27.1/nginx.exe', '-c', os.path.abspath('nginx_dynamic.conf')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(2)  # Даем немного времени процессу запуститься

        # Проверяем, запущен ли процесс Nginx
        if nginx_process.poll() is not None:
            stdout, stderr = nginx_process.communicate()
            return 1, f"start_nginx: {stderr.decode()}"
        return 0, ""
    except Exception as e:
        return 1, f"start_nginx: {str(e)}"

def stop_nginx():
    global nginx_process
    if nginx_process:
        try:
            nginx_proc = psutil.Process(nginx_process.pid)
        except:
            print('Процесс не найден', nginx_process.pid)
            return
        children = nginx_proc.children(recursive=True)
        for child in children:
            try:
                child.terminate()
                child.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass
        nginx_process.terminate()
        nginx_process.wait()
        nginx_process = None

def start_flask():
    global flask_thread, flask_app
    flask_app, message = create_app()  # Создаем Flask-приложение
    print(message)
    if flask_app:
        try:
            print("Flask App создан:", flask_app)
            serve(flask_app, host='127.0.0.1', port=config['server_port'], threads=12)
        except Exception as e:
            flask_thread.exit_reason = f"serve: {e}"
    else:
        print(message)
        window.signal_error(message)


def start_service():
    global flask_thread, flask_app
    res, msg = generate_nginx_config(config)
    if res:
        window.signal_error(msg)
        return
    res, msg = start_nginx()
    if res:
        window.signal_error(msg)
        return
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.exit_reason = 0
    flask_thread.start()
    # Дожидаемся инициализации `flask_app`
    time.sleep(2)
    # Проверяем, что Flask-приложение успешно создано
    if flask_app is None:
        print("Ошибка: Flask App не создан!")
        return

def stop_service():
    global flask_thread
    stop_nginx()

class MainWindow(QMainWindow):
    nginx_error_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.monitor_thread = None
        self.stop_threads_event = threading.Event()
        self.setWindowTitle("Сервер CWAA")
        self.setFixedSize(420, 280)
        self.setWindowIcon(QIcon('cwaa-icon.ico'))

        # Статус сервера и кнопки
        self.status_label = QLabel("Статус: Остановлен", self)
        self.start_button = QPushButton("Запустить сервер", self)
        self.stop_button = QPushButton("Остановить сервер", self)
        self.stop_button.setEnabled(False)
        self.app_link = QLabel()
        self.update_app_link()
        self.app_link.setOpenExternalLinks(True)
        self.nginx_error_signal.connect(self.signal_error)

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
        form_layout.addRow("Выберите IP для размещения сервера:", self.server_ip_combo)
        form_layout.addRow("Введите номер порта для размещения сервера:", self.server_port_input)
        form_layout.addRow("Путь хранения открытых аудиопротоколов:", self.public_audio_path_input)
        form_layout.addRow("Путь хранения закрытых аудиопротоколов:", self.closed_audio_path_input)

        # Кнопка сохранения настроек
        self.save_button = QPushButton("Сохранить настройки")
        self.save_button.clicked.connect(self.save_config)

        self.firewall_button = QPushButton("Создать правило в брандмауэре для указанного порта\nТребуется запуск от имени администратора")
        self.firewall_button.clicked.connect(self.create_firewall_rule)

        # Основной layout
        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.app_link)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        layout.addLayout(form_layout)
        layout.addWidget(self.firewall_button)
        layout.addWidget(self.save_button)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Подключение событий кнопок
        self.start_button.clicked.connect(self.start_server)
        self.stop_button.clicked.connect(self.stop_server)

        # Настройка системного трея
        self.tray_icon = QSystemTrayIcon(self)
        self.update_tray_icon("yellow")  # Изначально остановлен
        self.tray_icon.setVisible(True)

        # Контекстное меню для иконки в трее
        tray_menu = QMenu()
        self.start_action = QAction("Запустить сервер", self)
        self.start_action.triggered.connect(self.start_server)
        tray_menu.addAction(self.start_action)

        self.stop_action = QAction("Остановить сервер", self)
        self.stop_action.setEnabled(False)
        self.stop_action.triggered.connect(self.stop_server)
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


    def start_server(self):
        try:
            start_service()  # Запуск сервиса, включая Flask
            self.monitor_thread = threading.Thread(target=self.monitor_services, args=(self.nginx_error_signal, self.stop_threads_event),
                                                   daemon=True)
            self.monitor_thread.start()
            # Ждем, пока flask_app инициализируется
            retries = 5
            while flask_app is None and retries > 0:
                print("Ожидание инициализации Flask-приложения...")
                time.sleep(2)
                retries -= 1
            if flask_app is None:
                print("Flask-приложение так и не было инициализировано. Мониторинг не запущен.")
                return
            self.update_status("Запущен")
            self.update_tray_icon("green")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.server_ip_combo.setEnabled(False)
            self.server_port_input.setEnabled(False)
            self.public_audio_path_input.setEnabled(False)
            self.closed_audio_path_input.setEnabled(False)
            self.save_button.setEnabled(False)
            self.start_action.setEnabled(False)
            self.stop_action.setEnabled(True)
        except RuntimeError as e:
            self.update_tray_icon("red")
            self.show_error_message(str(e))

    def stop_server(self):
        global flask_thread
        stop_service()
        # Устанавливаем флаг остановки и ждем завершения потока
        self.stop_threads_event.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join()
        self.update_status("Остановлен")
        self.update_tray_icon("yellow")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.server_ip_combo.setEnabled(True)
        self.server_port_input.setEnabled(True)
        self.public_audio_path_input.setEnabled(True)
        self.closed_audio_path_input.setEnabled(True)
        self.start_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.save_button.setEnabled(True)

    def monitor_services(self, signal_error, stop_event):
        """Функция для отслеживания процессов nginx и flask"""
        global nginx_process, flask_thread
        while not stop_event.is_set():
            if nginx_process:
                try:
                    if not psutil.pid_exists(nginx_process.pid):
                        raise psutil.NoSuchProcess(nginx_process.pid)
                    nginx_proc = psutil.Process(nginx_process.pid)
                    for child in nginx_proc.children(recursive=True):
                        if not child.is_running():
                            raise psutil.NoSuchProcess(child.pid)
                except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                    signal_error.emit("Nginx умер")  # Ошибка Nginx
                    break
            # Добавляем проверку работы Flask
            if flask_thread and not flask_thread.is_alive():
                signal_error.emit(f'Flask умер: {flask_thread.exit_reason}')  # Ошибка Flask
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
        self.update_status(message)
        self.update_tray_icon("red")

    def save_config(self):
        global config
        config['server_ip'] = self.server_ip_combo.currentText()
        config['server_port'] = int(self.server_port_input.text())
        config['public_audio_path'] = self.public_audio_path_input.text().replace('"', '')
        config['closed_audio_path'] = self.closed_audio_path_input.text().replace('"', '')
        save_config(config)
        self.update_app_link()

    def show_error_message(self, message):
        error_dialog = QMessageBox(self)
        error_dialog.setWindowTitle("Ошибка")
        error_dialog.setText("Произошла ошибка при запуске сервера.")
        error_dialog.setDetailedText(message)
        error_dialog.setIcon(QMessageBox.Critical)
        error_dialog.exec_()

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
        stop_service()
        sys.exit(0)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    sys.exit(app.exec_())
