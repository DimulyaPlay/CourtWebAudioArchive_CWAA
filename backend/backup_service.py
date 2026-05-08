from PySide2.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QCheckBox, QTimeEdit, QSpinBox,
    QPushButton, QFileDialog, QGroupBox, QLineEdit, QProgressBar, QHBoxLayout, QMessageBox
)
from PySide2.QtCore import Qt, QTime
from . import config
from .db import checkpoint_wal, sqlite_backup_snapshot
from contextlib import ExitStack
import os
import zipfile
import datetime
import threading
import schedule
import time as time_module

BACKUP_CONFIG_PATH = './backup_config.txt'

class BackupSettingsWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Настройки резервного копирования")
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        # Блок "Основные настройки"
        main_group = QGroupBox("Основные параметры")
        main_layout = QVBoxLayout()

        self.enable_backup = QCheckBox("Резервное копирование по расписанию")
        main_layout.addWidget(self.enable_backup)

        days_layout = QHBoxLayout()
        self.day_checkboxes = []
        self.day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        days_rus = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        for i, day in enumerate(days_rus):
            cb = QCheckBox(day)
            self.day_checkboxes.append(cb)
            days_layout.addWidget(cb)
        main_layout.addLayout(days_layout)

        time_layout = QHBoxLayout()
        time_layout.addWidget(QLabel("🕒 Время запуска:"))
        self.time_edit = QTimeEdit(QTime(3, 0))
        self.time_edit.setDisplayFormat("HH:mm")
        time_layout.addWidget(self.time_edit)
        main_layout.addLayout(time_layout)

        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("📁 Папка для хранения:"))
        self.path_edit = QLineEdit("C:\\Backups")
        path_btn = QPushButton("🔍")
        path_btn.setMaximumWidth(38)
        path_btn.clicked.connect(self.select_folder)
        path_layout.addWidget(self.path_edit)
        path_layout.addWidget(path_btn)
        main_layout.addLayout(path_layout)

        keep_layout = QHBoxLayout()
        keep_layout.addWidget(QLabel("🗂 Хранить копий:"))
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(1, 100)
        self.keep_spin.setValue(5)
        keep_layout.addWidget(self.keep_spin)
        main_layout.addLayout(keep_layout)

        main_group.setLayout(main_layout)
        layout.addWidget(main_group)

        # Блок "Что включать в архив"
        include_group = QGroupBox("Содержимое архива")
        include_layout = QVBoxLayout()
        self.include_db = QCheckBox("🧠 База данных (audio_archive.db)")
        self.include_public = QCheckBox("🌐 Открытые аудиопротоколы")
        self.include_closed = QCheckBox("🔒 Закрытые аудиопротоколы")
        self.include_db.setChecked(True)
        self.include_public.setChecked(True)
        self.include_closed.setChecked(True)
        include_layout.addWidget(self.include_db)
        include_layout.addWidget(self.include_public)
        include_layout.addWidget(self.include_closed)
        include_group.setLayout(include_layout)
        layout.addWidget(include_group)

        # Кнопки управления
        self.save_btn = QPushButton("💾 Сохранить настройки")
        self.run_backup_btn = QPushButton("🛠 Создать бэкап сейчас")
        layout.addWidget(self.save_btn)
        layout.addWidget(self.run_backup_btn)

        # Прогресс и статус
        self.status_label = QLabel("⏳ Ожидание...")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

        self.setLayout(layout)

        self.save_btn.clicked.connect(self.save_config)
        self.run_backup_btn.clicked.connect(self.confirm_and_run_backup)

        self.load_config()
        self.schedule_backup_thread()
        self.update_next_backup_status()

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку для бэкапов")
        if folder:
            self.path_edit.setText(folder)

    def confirm_and_run_backup(self):
        reply = QMessageBox.question(self, "Подтвердите запуск",
                                     "Вы действительно хотите запустить бэкап прямо сейчас? Это может занять несколько минут.",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            threading.Thread(target=self.run_backup_now, daemon=True).start()

    def update_next_backup_status(self):
        if not self.enable_backup.isChecked():
            self.status_label.setText("⛔ Резервное копирование по расписанию отключено.")
            return

        selected_days = [i for i, cb in enumerate(self.day_checkboxes) if cb.isChecked()]
        if not selected_days:
            self.status_label.setText("⚠ Расписание включено, но дни не выбраны")
            return

        now = datetime.datetime.now()
        target_time = self.time_edit.time()
        target_dt = now.replace(hour=target_time.hour(), minute=target_time.minute(), second=0, microsecond=0)

        days_ahead = [(d - now.weekday()) % 7 for d in selected_days]
        deltas = []
        for delta_day in days_ahead:
            dt_candidate = target_dt + datetime.timedelta(days=delta_day)
            if dt_candidate < now:
                dt_candidate += datetime.timedelta(days=7)
            deltas.append(dt_candidate - now)

        if deltas:
            nearest = min(deltas)
            hours, remainder = divmod(nearest.total_seconds(), 3600)
            minutes = remainder // 60
            self.status_label.setText(f"📅 Следующий запуск через {int(hours)}ч {int(minutes)}м")
        else:
            self.status_label.setText("⏳ Ожидание...")

    def load_config(self):
        if not os.path.exists(BACKUP_CONFIG_PATH):
            return
        with open(BACKUP_CONFIG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        config_data = dict(line.strip().split('=', 1) for line in lines if '=' in line)

        self.enable_backup.setChecked(config_data.get('backup_enabled', 'false') == 'true')
        days = config_data.get('backup_days', '').split(',')
        for i, day in enumerate(self.day_names):
            self.day_checkboxes[i].setChecked(day.lower() in [d.lower() for d in days])

        time_parts = config_data.get('backup_time', '03:00').split(':')
        if len(time_parts) == 2:
            self.time_edit.setTime(QTime(int(time_parts[0]), int(time_parts[1])))

        self.path_edit.setText(config_data.get('backup_path', 'C:\\CWAA_Backups'))
        self.keep_spin.setValue(int(config_data.get('backup_keep', 5)))
        self.include_db.setChecked(config_data.get('backup_include_db', 'true') == 'true')
        self.include_public.setChecked(config_data.get('backup_include_public', 'true') == 'true')
        self.include_closed.setChecked(config_data.get('backup_include_closed', 'true') == 'true')

    def save_config(self):
        config_lines = [
            f"backup_enabled={'true' if self.enable_backup.isChecked() else 'false'}",
            f"backup_days={','.join([day for day, cb in zip(self.day_names, self.day_checkboxes) if cb.isChecked()])}",
            f"backup_time={self.time_edit.time().toString('HH:mm')}",
            f"backup_path={self.path_edit.text()}",
            f"backup_keep={self.keep_spin.value()}",
            f"backup_include_db={'true' if self.include_db.isChecked() else 'false'}",
            f"backup_include_public={'true' if self.include_public.isChecked() else 'false'}",
            f"backup_include_closed={'true' if self.include_closed.isChecked() else 'false'}",
        ]
        with open(BACKUP_CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write('\n'.join(config_lines))
        self.status_label.setText("✅ Настройки сохранены.")
        self.update_next_backup_status()

    def schedule_backup_thread(self):
        def run_scheduler():
            while True:
                schedule.run_pending()
                time_module.sleep(30)

        def load_and_schedule():
            if not self.enable_backup.isChecked():
                return
            selected_days = [day for day, cb in zip(self.day_names, self.day_checkboxes) if cb.isChecked()]
            time_str = self.time_edit.time().toString("HH:mm")
            for day in selected_days:
                schedule.every().__getattribute__(day).at(time_str).do(lambda: self.run_backup_now())
            print("Бэкапы по расписанию запущены")

        load_and_schedule()
        thread = threading.Thread(target=run_scheduler, daemon=True)
        thread.start()

    def run_backup_now(self):
        self.status_label.setText("⏳ Создание бэкапа...")
        self.progress_bar.setValue(0)

        backup_folder = self.path_edit.text()
        os.makedirs(backup_folder, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        archive_path = os.path.join(backup_folder, f"backup_{timestamp}.zip")

        include_paths = []
        if self.include_public.isChecked():
            include_paths.append((config['public_audio_path'], "public_audio"))

        if self.include_closed.isChecked():
            include_paths.append((config['closed_audio_path'], "closed_audio"))

        try:
            with ExitStack() as stack:
                if self.include_db.isChecked():
                    db_path = os.path.abspath("audio_archive.db")
                    if os.path.exists(db_path):
                        snapshot_path = os.path.join(backup_folder, f"_audio_archive_snapshot_{timestamp}.db")
                        db_snapshot = stack.enter_context(sqlite_backup_snapshot(snapshot_path))
                        include_paths.insert(0, (db_snapshot, "audio_archive.db"))

                all_files = []
                for src, arcname in include_paths:
                    if os.path.isdir(src):
                        for root, _, files in os.walk(src):
                            for file in files:
                                full_path = os.path.join(root, file)
                                rel_path = os.path.relpath(full_path, src)
                                all_files.append((full_path, os.path.join(arcname, rel_path)))
                    elif os.path.isfile(src):
                        all_files.append((src, arcname))

                total_files = len(all_files)
                progress = 0

                with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for full_path, arcname in all_files:
                        zipf.write(full_path, arcname)
                        progress += 1
                        if total_files:
                            self.progress_bar.setValue(int(progress / total_files * 100))

            if self.include_db.isChecked():
                checkpoint_wal('TRUNCATE')

            self.status_label.setText("✅ Бэкап завершён.")
        except Exception as e:
            self.status_label.setText(f"❌ Ошибка: {str(e)}")
            return

        # Удаляем старые архивы
        try:
            files = sorted(
                [f for f in os.listdir(backup_folder) if f.startswith("backup_") and f.endswith(".zip")],
                reverse=True
            )
            for old in files[self.keep_spin.value():]:
                os.remove(os.path.join(backup_folder, old))
        except Exception as e:
            print("Ошибка при удалении старых копий:", e)
