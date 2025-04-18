from PySide2.QtCore import Signal
from PySide2.QtGui import QIcon
import time
from backend import create_app, config
from backend.utils import save_config, get_all_public_ips
from backend.recognition_orchestrator import run_orchestrator_loop
from waitress import serve
import subprocess
from string import Template
import os
import threading
import psutil
from PySide2.QtWidgets import (
    QMainWindow, QApplication, QPushButton, QLabel, QVBoxLayout, QWidget, QLineEdit, QFormLayout, QMessageBox,
    QComboBox, QSystemTrayIcon, QMenu, QAction, QCheckBox, QListWidget, QHBoxLayout, QTableWidget, QTableWidgetItem, QFileDialog)
import sys
from backend.backup_service import BackupSettingsWindow


# .venv/Scripts/pyinstaller.exe --windowed --noconfirm --contents-directory "." --icon "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\cwaa-icon.ico" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\cwaa-icon.ico;." --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\assets;assets" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\frontend;frontend" --add-data "C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)\nginx-1.27.1;nginx-1.27.1" "CWAA Server.py"

# if getattr(sys, 'frozen', False):
#     sys.stdout = open('console_output.log', 'a', buffering=1)
#     sys.stderr = open('console_errors.log', 'a', buffering=1)

os.makedirs('logs', exist_ok=True)
os.makedirs('temp', exist_ok=True)
os.makedirs('backend', exist_ok=True)

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
        time.sleep(2)
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
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.exit_reason = 0
    flask_thread.start()
    time.sleep(2)
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
        self.backup_window = None
        self.monitor_thread = None
        self.stop_threads_event = threading.Event()
        self.setWindowTitle("Сервер CWAA")
        self.setFixedSize(420, 370)
        self.setWindowIcon(QIcon('cwaa-icon.ico'))

        # Статус сервера и кнопки
        self.status_label = QLabel("Статус: Остановлен", self)
        self.start_button = QPushButton("🚀 Запустить сервер", self)
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
        self.recognize_text_from_audio_path_input = QLineEdit(config['recognize_text_from_audio_path'])
        self.recognize_text_from_audio_path_input.setPlaceholderText('C:\\папка\\еще папка')
        self.create_year_subfolders = QCheckBox()
        self.create_year_subfolders.setChecked(config['create_year_subfolders']=='true')
        form_layout.addRow("Выберите IP для размещения сервера:", self.server_ip_combo)
        form_layout.addRow("Введите номер порта для размещения сервера:", self.server_port_input)
        form_layout.addRow('Создавать подпапки по годам в папках судей', self.create_year_subfolders)
        form_layout.addRow("Путь хранения открытых аудиопротоколов:", self.public_audio_path_input)
        form_layout.addRow("Путь хранения закрытых аудиопротоколов:", self.closed_audio_path_input)
        form_layout.addRow('Путь для распознавания аудиопротоколов:', self.recognize_text_from_audio_path_input)
        # Кнопка сохранения настроек
        self.save_button = QPushButton("💾 Сохранить настройки")
        self.save_button.clicked.connect(self.save_config)

        self.firewall_button = QPushButton(
            "🛡 Создать правило в брандмауэре для указанного порта\nТребуется запуск от имени администратора")
        self.firewall_button.clicked.connect(self.create_firewall_rule)
        self.scan_button = QPushButton("📂 Сканировать папки и импортировать записи")
        self.scan_button.clicked.connect(self.scan_archives)

        self.backup_button = QPushButton("🛠 Параметры резервного копирования")
        self.backup_button.clicked.connect(self.open_backup_settings)

        self.courtroom_button = QPushButton("🏛 Управление залами")
        self.courtroom_button.clicked.connect(self.open_courtroom_manager)

        # Основной layout
        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.app_link)
        layout.addWidget(self.start_button)
        layout.addLayout(form_layout)
        layout.addWidget(self.firewall_button)
        layout.addWidget(self.scan_button)
        layout.addWidget(self.backup_button)
        layout.addWidget(self.courtroom_button)
        layout.addWidget(self.save_button)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.start_button.clicked.connect(self.start_server)

        self.tray_icon = QSystemTrayIcon(self)
        self.update_tray_icon("yellow")  # Изначально остановлен
        self.tray_icon.setVisible(True)

        tray_menu = QMenu()
        self.start_action = QAction("Запустить сервер", self)
        self.start_action.triggered.connect(self.start_server)
        tray_menu.addAction(self.start_action)

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

    def start_server(self):
        try:
            if not self.backup_window:
                self.backup_window = BackupSettingsWindow()
            start_service()  # Запуск сервиса, включая Flask
            threading.Thread(target=run_orchestrator_loop, daemon=True).start()

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
            self.server_ip_combo.setEnabled(False)
            self.server_port_input.setEnabled(False)
            self.public_audio_path_input.setEnabled(False)
            self.closed_audio_path_input.setEnabled(False)
            self.save_button.setEnabled(False)
            self.start_action.setEnabled(False)
        except RuntimeError as e:
            self.update_tray_icon("red")
            self.show_error_message(str(e))

    def stop_server(self):
        global flask_thread
        stop_service()
        self.stop_threads_event.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join()

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
        config['recognize_text_from_audio_path'] = self.recognize_text_from_audio_path_input.text().replace('"', '')
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
        self.stop_server()
        sys.exit(0)


class CourtroomManagerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Управление залами")
        self.setFixedSize(400, 500)

        layout = QVBoxLayout()
        import_label = QLabel("🔁 Залы для сохранения аудиозаписей:")
        layout.addWidget(import_label)
        self.list_widget = QListWidget()
        self.load_courtrooms()
        self.load_import_sources()
        layout.addWidget(self.list_widget)

        cr_btns = QHBoxLayout()
        self.input_field = QLineEdit()
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
        self.import_path = QLineEdit()
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
    app = QApplication(sys.argv)
    window = MainWindow()
    sys.exit(app.exec_())
