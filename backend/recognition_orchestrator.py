import os
import time
import shutil
import traceback
import re
from sqlalchemy import or_
from backend import config
from backend.db import Session
from backend.models import AudioRecord
from backend.path_resolver import absolute_to_relative_path, resolve_record_audio_path
from backend.utils import is_file_fully_copied
import subprocess
import sys

MAX_PENDING_FILES = 20
SLEEP_SECONDS = 20

# Кэш для правил замены и время их последнего обновления
_PHRASE_RULES = None
_PHRASE_RULES_MTIME = None


def get_asr_executable_path():
    return os.path.abspath(os.path.join(os.getcwd(), "GigaAM_ASR", "GigaAM_ASR.exe"))


def has_asr_executable():
    return os.path.exists(get_asr_executable_path())


def load_phrase_replacement_rules(force_reload: bool = False):
    """Загружает правила из файла, используя кэш.

    Правила перечитываются только если файл был изменён либо при
    принудительном обновлении через ``force_reload``.
    """
    global _PHRASE_RULES, _PHRASE_RULES_MTIME

    file_path = os.path.join('assets', 'phraseReplacement.txt')
    try:
        mtime = os.path.getmtime(file_path)
    except FileNotFoundError:
        mtime = None

    if force_reload or _PHRASE_RULES is None or mtime != _PHRASE_RULES_MTIME:
        rules = []
        if mtime is not None:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line or '>' not in line:
                        continue
                    parts = line.split('>')
                    if parts[-1].endswith(';'):
                        parts[-1] = parts[-1][:-1].strip()
                    if len(parts) < 2:
                        continue
                    *wrong_forms, correct = parts
                    for wrong in wrong_forms:
                        rules.append((wrong.strip(), correct.strip(), line_num))
        _PHRASE_RULES = rules
        _PHRASE_RULES_MTIME = mtime

    return _PHRASE_RULES


def get_phrase_replacement_rules():
    """Возвращает загруженные правила замены."""
    return _PHRASE_RULES if _PHRASE_RULES is not None else load_phrase_replacement_rules()


# Загружаем правила при инициализации модуля
load_phrase_replacement_rules()


def apply_replacement_with_tags(text, rules):
    # Прячем уже существующие <replace> теги
    placeholders = []
    def hide_existing(match):
        placeholders.append(match.group(0))
        return f"<<PH_{len(placeholders)}>>"
    text = re.sub(r'<replace>.*?</replace>', hide_existing, text, flags=re.DOTALL)

    for wrong, correct, rule_num in rules:
        # Поддержка границ: не внутри слов
        pattern = re.compile(r'(?<![\wа-яА-Я])(' + re.escape(wrong) + r')(?![\wа-яА-Я])', flags=re.IGNORECASE)

        def replacer(match):
            return f"<replace><old>{match.group(1)}</old><new>{correct}</new><rule>{rule_num}</rule></replace>"

        text = pattern.sub(replacer, text)

    # Восстанавливаем скрытые теги
    for i, ph in enumerate(placeholders):
        text = text.replace(f"<<PH_{i}>>", ph)

    return text



def strip_replacement_tags(tagged_text):
    return re.sub(r'<replace><old>.*?</old><new>(.*?)</new><rule>\d+</rule></replace>', r'\1', tagged_text)


def run_orchestrator_loop(stop_event=None):
    recognize_folder = config['recognize_text_from_audio_path']
    if not recognize_folder or not os.path.exists(recognize_folder):
        return
    ASR_EXE = get_asr_executable_path()
    if not os.path.exists(ASR_EXE):
        print(f"[ASR] Не найден исполняемый файл: {ASR_EXE}. Оркестратор распознавания не запущен.")
        return
    CHECK_INTERVAL_SECONDS = 60
    while not (stop_event and stop_event.is_set()):
        session = None
        try:
            session = Session()
            # 1) Берём записи из БД, которые надо распознать (ограничиваем пачку)
            records = session.query(AudioRecord).filter(
                AudioRecord.recognize_text == True,
                or_(
                    AudioRecord.recognized_text_path == None,
                    AudioRecord.recognized_text_path == ''
                )
            ).limit(MAX_PENDING_FILES).all()
            if not records:
                if stop_event:
                    stop_event.wait(CHECK_INTERVAL_SECONDS)
                else:
                    time.sleep(CHECK_INTERVAL_SECONDS)
                continue
            # 2) Убеждаемся, что mp3 лежат в папке распознавания (копируем недостающие)
            mp3_to_recognize = []
            for record in records:
                source_path = resolve_record_audio_path(record)[1]
                if not source_path or not os.path.exists(source_path):
                    print(f"[ASR] Аудиофайл не найден для ID={record.id}: {record.file_path}")
                    continue
                base_filename = os.path.basename(source_path)
                target_filename = f"{record.id}___{base_filename}"
                target_path = os.path.join(recognize_folder, target_filename)
                if not os.path.exists(target_path):
                    shutil.copy2(source_path, target_path)
                    print(f"[ASR] Копируем {source_path} -> {target_path}")
                mp3_to_recognize.append(target_path)
            if not mp3_to_recognize:
                if stop_event:
                    stop_event.wait(CHECK_INTERVAL_SECONDS)
                else:
                    time.sleep(CHECK_INTERVAL_SECONDS)
                continue
            # 3) Запускаем локальный ASR на всей пачке и ждём завершения
            cmd = [ASR_EXE, *mp3_to_recognize]
            print(f"[ASR] Запуск: {' '.join(cmd[:3])}{' ...' if len(cmd) > 3 else ''}")
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=os.getcwd()
            )
            if proc.returncode != 0:
                print(f"[ASR] Ошибка распознавания. code={proc.returncode}")
                if proc.stderr:
                    print(proc.stderr[-1500:])
                if stop_event:
                    stop_event.wait(CHECK_INTERVAL_SECONDS)
                else:
                    time.sleep(CHECK_INTERVAL_SECONDS)
                continue
            rules = get_phrase_replacement_rules()
            for record in records:
                source_path = resolve_record_audio_path(record)[1]
                if not source_path:
                    continue
                mp3_name = f"{record.id}___{os.path.basename(source_path)}"
                recognize_mp3_path = os.path.join(recognize_folder, mp3_name)
                recognize_txt_path = os.path.splitext(recognize_mp3_path)[0] + ".txt"
                if not os.path.exists(recognize_txt_path):
                    print(f"[ASR] TXT не найден для {recognize_mp3_path}: ожидали {recognize_txt_path}")
                    continue
                if not is_file_fully_copied(recognize_txt_path):
                    print(f"[ASR] TXT ещё пишется, пропускаем: {recognize_txt_path}")
                    continue
                archive_folder = os.path.dirname(source_path)
                final_txt_path = os.path.join(archive_folder, os.path.basename(recognize_txt_path).split('___', 1)[1])
                try:
                    with open(recognize_txt_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    tagged_text = apply_replacement_with_tags(content, rules)
                    with open(final_txt_path, "w", encoding="utf-8") as f:
                        f.write(tagged_text)
                    record.recognized_text_path = absolute_to_relative_path(final_txt_path) or final_txt_path
                    session.commit()
                    print(f"[ASR] Сохранён протокол: {final_txt_path}")
                except Exception as e:
                    traceback.print_exc()
                    print(f"[ASR] Ошибка индексирования ID={record.id}: {e}")
                    continue
                try:
                    recognize_docx_path = os.path.splitext(recognize_mp3_path)[0] + ".docx"
                    if os.path.exists(recognize_docx_path):
                        os.remove(recognize_docx_path)
                        print(f"[ASR] Удалили побочный {recognize_docx_path}")
                    if os.path.exists(recognize_txt_path):
                        os.remove(recognize_txt_path)
                    if os.path.exists(recognize_mp3_path):
                        os.remove(recognize_mp3_path)
                except Exception as e:
                    print(f"[ASR] Не удалось подчистить хвосты: {e}")
            continue
        except Exception as e:
            traceback.print_exc()
            print("[ASR] Ошибка в оркестраторе:", e)
            if stop_event:
                stop_event.wait(CHECK_INTERVAL_SECONDS)
            else:
                time.sleep(CHECK_INTERVAL_SECONDS)
        finally:
            if session:
                session.close()
