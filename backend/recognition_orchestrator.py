import os
import time
import shutil
import traceback

from backend import config
from backend.db import Session
from backend.models import AudioRecord
from backend.utils import is_file_fully_copied, index_record_text

RECOGNIZE_FOLDER = config['recognize_text_from_audio_path']
MAX_PENDING_FILES = 20
SLEEP_SECONDS = 20


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

            # Ищем готовые .txt
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
                    # 1. читаем
                    with open(source_txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    # 2. индексируем
                    index_record_text(record.id, content)
                    print(f"Индексировали текст ID={record.id}")
                    # 3. только если всё прошло успешно — перемещаем
                    shutil.move(source_txt_path, final_txt_path)
                    record.recognized_text_path = final_txt_path
                    session.commit()
                    print(f"Переместили распознанный текст → {final_txt_path}")
                    # 4. удаляем .mp3 из RECOGNIZE_FOLDER
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
                        print(f"⚠ Не удалось удалить {recognize_mp3_path}: {e}")
                except Exception as e:
                    traceback.print_exc()
                    print(f"❌ Ошибка при индексировании текста для ID={record.id}: {e}")
                    # файл останется в папке — будет повторно обработан позже

        except Exception as e:
            traceback.print_exc()
            print("Ошибка в оркестраторе:", e)
        finally:
            session.close()

        time.sleep(SLEEP_SECONDS)
