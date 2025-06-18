import os
import time
import shutil
import traceback
import re
from backend import config
from backend.db import Session
from backend.models import AudioRecord
from backend.utils import is_file_fully_copied, index_record_text

RECOGNIZE_FOLDER = config['recognize_text_from_audio_path']
MAX_PENDING_FILES = 20
SLEEP_SECONDS = 20


def load_phrase_replacement_rules():
    rules = []
    with open(os.path.join('assets', 'phraseReplacement.txt'), 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line or '>' not in line:
                continue
            # удаляем лишние пробелы и точку с запятой
            parts = line.split('>')
            if parts[-1].endswith(';'):
                parts[-1] = parts[-1][:-1].strip()
            if len(parts) < 2:
                continue
            *wrong_forms, correct = parts
            for wrong in wrong_forms:
                rules.append((wrong.strip(), correct.strip(), line_num))
    return rules


def apply_replacement_with_tags(text, rules):
    def is_inside_tag(pos, tag_spans):
        return any(start <= pos < end for start, end in tag_spans)
    tag_spans = [(m.start(), m.end()) for m in re.finditer(r'<replace>.*?</replace>', text, flags=re.DOTALL)]
    for wrong, correct, rule_num in rules:
        if ' ' in wrong:
            pattern = r'(?<!\S)' + re.escape(wrong) + r'(?!\S)'
        else:
            pattern = r'\b' + re.escape(wrong) + r'\b'
        def safe_replacer(match):
            start = match.start()
            if is_inside_tag(start, tag_spans):
                return match.group(0)  # уже внутри <replace> — не трогаем
            return f"<replace><old>{match.group(0)}</old><new>{correct}</new><rule>{rule_num}</rule></replace>"
        text = re.sub(pattern, safe_replacer, text)
        tag_spans = [(m.start(), m.end()) for m in re.finditer(r'<replace>.*?</replace>', text, flags=re.DOTALL)]
    return text


def strip_replacement_tags(tagged_text):
    return re.sub(r'<replace><old>.*?</old><new>(.*?)</new><rule>\d+</rule></replace>', r'\1', tagged_text)


def run_orchestrator_loop():
    if not RECOGNIZE_FOLDER or not os.path.exists(RECOGNIZE_FOLDER):
        return
    while True:
        try:
            session = Session()
            pending_mp3 = [f for f in os.listdir(RECOGNIZE_FOLDER) if f.endswith('.mp3')]
            if len(pending_mp3) < MAX_PENDING_FILES:
                needed = MAX_PENDING_FILES - len(pending_mp3)
                # Получаем нужные записи для распознавания
                records = session.query(AudioRecord).filter(
                    AudioRecord.recognize_text == True,
                    AudioRecord.recognized_text_path == None
                ).all()
                for record in records:
                    if needed:
                        source_path = record.file_path
                        base_filename = os.path.basename(source_path)
                        target_filename = f"{record.id}___{base_filename}"
                        target_path = os.path.join(RECOGNIZE_FOLDER, target_filename)
                        if not os.path.exists(target_path):
                            shutil.copy2(source_path, target_path)
                            print(f"Копируем {source_path} → {target_path}")
                            needed-=1
                        else:
                            continue
            pending_txt = [f for f in os.listdir(RECOGNIZE_FOLDER) if f.endswith('.txt') and '___' in f]
            for txt_file in pending_txt:
                id_part, original_name = txt_file.split('___', 1)
                try:
                    record_id = int(id_part)
                except ValueError:
                    continue
                record = session.query(AudioRecord).get(record_id)
                if not record:
                    continue
                source_txt_path = os.path.join(RECOGNIZE_FOLDER, txt_file)
                if not is_file_fully_copied(source_txt_path):
                    print(f"Файл {source_txt_path} ещё копируется, пропускаем...")
                    continue
                archive_folder = os.path.dirname(record.file_path)
                final_txt_path = os.path.join(archive_folder, original_name)
                try:
                    with open(source_txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        rules = load_phrase_replacement_rules()
                        tagged_text = apply_replacement_with_tags(content, rules)
                    with open(source_txt_path, 'w', encoding='utf-8') as f:
                        f.write(tagged_text)
                    index_record_text(record.id, strip_replacement_tags(tagged_text))
                    print(f"Индексировали текст ID={record.id}")
                    shutil.move(source_txt_path, final_txt_path)
                    record.recognized_text_path = final_txt_path
                    session.commit()
                    print(f"Переместили распознанный текст {final_txt_path}")
                    try:
                        recognize_mp3_name = f"{record.id}___{os.path.basename(record.file_path)}"
                        recognize_mp3_path = os.path.join(RECOGNIZE_FOLDER, recognize_mp3_name)
                        recognize_docx_path = recognize_mp3_path.replace('.mp3', '.docx')
                        if os.path.exists(recognize_docx_path):
                            os.remove(recognize_docx_path)
                            print(f"Удалили побочный {recognize_docx_path}")
                        if os.path.exists(recognize_mp3_path):
                            os.remove(recognize_mp3_path)
                            print(f"Удалили из очереди {recognize_mp3_path}")
                    except Exception as e:
                        print(f"Не удалось удалить {recognize_mp3_path}: {e}")
                except Exception as e:
                    traceback.print_exc()
                    print(f"Ошибка при индексировании текста для ID={record.id}: {e}")
        except Exception as e:
            traceback.print_exc()
            print("Ошибка в оркестраторе:", e)
        finally:
            session.close()

        time.sleep(SLEEP_SECONDS)
