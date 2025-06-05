from flask import request, jsonify, Blueprint, send_from_directory, send_file
import os
from backend.utils import get_file_hash, compare_files, parse_transcript_file, TEMP_MP3_FOLDER
from . import config
from backend.db import Session, engine
from backend.models import AudioRecord
from sqlalchemy import text, desc
import zipfile
import io
from pathlib import Path
from datetime import timedelta, datetime
import tempfile
import subprocess
import re
from threading import Semaphore

FFMPEG_SEMAPHORE = Semaphore(2)  # максимум 2 задачи конвертации одновременно


api = Blueprint('api', __name__)


@api.route('/search')
def search_records():
    session = Session()
    query = session.query(AudioRecord)
    case_number = request.args.get('case_number')
    courtroom = request.args.get('courtroom')
    comment = request.args.get('comment')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    use_fts = request.args.get('use_fts')
    text_query = request.args.get('text_query')
    user_folder = request.args.get('user_folder')
    if case_number:
        query = query.filter(AudioRecord.case_number.like(f"%{case_number}%"))
    if courtroom:
        query = query.filter(AudioRecord.courtroom.like(f"%{courtroom}%"))
    if comment:
        query = query.filter(AudioRecord.comment.like(f"%{comment}%"))
    if date_from and date_to and date_from == date_to:
        try:
            start_dt = datetime.strptime(date_from, "%Y-%m-%d")
            end_dt = start_dt + timedelta(days=1)
            query = query.filter(AudioRecord.audio_date >= start_dt, AudioRecord.audio_date < end_dt)
        except ValueError:
            pass
    else:
        if date_from:
            query = query.filter(AudioRecord.audio_date >= date_from)
        if date_to:
            try:
                # Добавляем +1 день к date_to, чтобы включить его полностью
                end_dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(AudioRecord.audio_date < end_dt)
            except ValueError:
                pass
    if user_folder:
        query = query.filter(AudioRecord.user_folder.like(f"%{user_folder}%"))
    records = query.order_by(desc(AudioRecord.audio_date)).limit(50).all()
    results = []
    fts_matches = set()
    if use_fts and text_query:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT audio_id FROM record_texts WHERE content MATCH :q"),
                {'q': text_query}
            )
            fts_matches = {row[0] for row in result.fetchall()}

    for rec in records:
        if use_fts and rec.id not in fts_matches:
            continue
        results.append({
            'id': rec.id,
            'case_number': rec.case_number,
            'user_folder': rec.user_folder,
            'date': rec.audio_date.isoformat(),
            'courtroom': rec.courtroom,
            'comment': rec.comment,
            'file_path': rec.file_path,
            'recognized_text_path': rec.recognized_text_path,
        })

    session.close()
    return jsonify(results)


@api.route('/audio/<path:filename>')
def serve_audio(filename):
    full_path = os.path.join(config['public_audio_path'], filename)
    if not os.path.exists(full_path):
        full_path = os.path.join(config['closed_audio_path'], filename)
        if not os.path.exists(full_path):
            return "Файл не найден", 404

    return send_file(full_path, mimetype='audio/mpeg')


@api.route('/download')
def download_files():
    ids = request.args.getlist('id', type=int)
    if not ids:
        return "Не переданы ID файлов.", 400

    session = Session()
    records = session.query(AudioRecord).filter(AudioRecord.id.in_(ids)).all()
    session.close()

    if len(records) == 1:
        file_path = records[0].file_path
        if not os.path.exists(file_path):
            return "Файл не найден.", 404
        return send_file(file_path, as_attachment=True)

    # Несколько файлов: архивируем
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        case_list = set()
        for rec in records:
            print("текущий сет:", case_list)
            if os.path.exists(rec.file_path):
                print("добавляем в сет:", rec.case_number)
                case_list.add(rec.case_number)
                arcname = os.path.basename(rec.file_path)
                zf.write(rec.file_path, arcname)
    print("финальный сет:", case_list)
    download_name = '; '.join(case_list)+'.zip'
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', download_name=download_name, as_attachment=True)


@api.route('/record/<int:record_id>')
def get_record_data(record_id):
    session = Session()
    record = session.query(AudioRecord).get(record_id)
    session.close()
    if not record:
        return jsonify({"error": "Протокол не найден"}), 404
    # Определяем путь до аудио
    rel_path = Path(record.file_path).relative_to(config['public_audio_path'])
    audio_url = f"/api/audio/{rel_path.as_posix()}"
    # Парсим текст, если он есть
    phrases = []
    if record.recognized_text_path and os.path.exists(record.recognized_text_path):
        phrases = parse_transcript_file(record.recognized_text_path)

    return jsonify({
        'title': f"{record.case_number} — {record.audio_date.strftime('%d.%m.%Y %H:%M')}",
        'audio_url': audio_url,
        'phrases': phrases
    })


@api.route('/reset_transcription/<int:record_id>', methods=['POST'])
def reset_transcription(record_id):
    session = Session()
    record = session.query(AudioRecord).get(record_id)
    if not record:
        session.close()
        return jsonify({"error": "Протокол не найден"}), 404

    if record.recognized_text_path and os.path.exists(record.recognized_text_path):
        try:
            os.remove(record.recognized_text_path)
        except Exception as e:
            print(f"Не удалось удалить файл: {e}")

    record.recognized_text_path = None
    record.recognize_text = True
    session.commit()
    session.close()
    return jsonify({"status": "ok"})


@api.route('/get_vr_queue_len')
def get_vr_queue_len():
    session = Session()
    records = session.query(AudioRecord).filter(
        AudioRecord.recognize_text == True,
        AudioRecord.recognized_text_path == None
    ).all()
    return jsonify({'records_to_vr':len(records)})


@api.route('/import_sources')
def list_import_sources():
    result = []
    if os.path.exists('import_sources.txt'):
        with open('import_sources.txt', 'r', encoding='utf-8') as f:
            for line in f:
                if '|' in line:
                    name, path = line.strip().split('|', 1)
                    if os.path.isdir(path):
                        result.append({'name': name, 'path': path})
    return jsonify(result)


@api.route('/import_cases')
def list_cases_in_folder():
    base = request.args.get('path')
    if not base or not os.path.exists(base):
        return jsonify([])
    entries = []
    for name in os.listdir(base):
        full_path = os.path.join(base, name)
        if os.path.isdir(full_path):
            entries.append((name, os.path.getmtime(full_path)))
    entries.sort(key=lambda x: -x[1])  # от новых к старым
    return jsonify([e[0] for e in entries])


@api.route('/convert_case', methods=['POST'])
def convert_case():
    with FFMPEG_SEMAPHORE:
        case_path = request.form.get('path')
        if not case_path or not os.path.isdir(case_path):
            return jsonify({'error': 'invalid path'}), 400
        # Группировка по каналам
        channel_groups = {}
        for root, _, files in os.walk(case_path):
            for f in files:
                if not f.lower().endswith('.wav'):
                    continue
                match = re.match(r'^(\d\s\d\d)', f)
                if match:
                    key = match.group(1)
                    channel_groups.setdefault(key, []).append(os.path.join(root, f))

        groups = [sorted(lst) for lst in sorted(channel_groups.values()) if lst]
        if not groups:
            return jsonify({'error': 'no valid .wav files'}), 400
        # Конкатенация каналов
        intermediate_files = []
        for idx, group in enumerate(groups):
            cmd = ['ffmpeg', '-y']
            for f in group:
                cmd += ['-i', f]
            concat_filter = ''.join([f'[{i}:0]' for i in range(len(group))])
            concat_filter += f'concat=n={len(group)}:v=0:a=1[out]'
            out_tmp = os.path.join(TEMP_MP3_FOLDER, f"intermediate_{idx}.wav")
            cmd += ['-filter_complex', concat_filter, '-map', '[out]', '-acodec', 'adpcm_ima_wav', out_tmp]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                return jsonify({'error': f'ffmpeg concat error: {result.stderr.decode()[:200]}'}), 500
            intermediate_files.append(out_tmp)
        # Смешивание всех каналов
        mix_cmd = ['ffmpeg', '-y']
        for f in intermediate_files:
            mix_cmd += ['-i', f]
        final_name = f"femida_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
        final_tmp = os.path.join(TEMP_MP3_FOLDER, final_name)
        mix_cmd += [
            '-filter_complex', f'amix=inputs={len(intermediate_files)}:duration=longest:dropout_transition=2',
            '-ac', '1', '-ar', '16000', '-q:a', '2', final_tmp
        ]
        result = subprocess.run(mix_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            return jsonify({'error': f'ffmpeg mix error: {result.stderr.decode()[:200]}'}), 500
        # Извлекаем дату из названия кейса (например: from 18-04-2025)
        base_name = os.path.basename(case_path)
        dt_match = re.search(r'from\s+(\d{2})-(\d{2})-(\d{4})', base_name)
        if dt_match:
            dt = datetime.strptime('-'.join(dt_match.groups()), "%d-%m-%Y")
        else:
            dt = datetime.now()

        return jsonify({
            'temp_id': final_tmp,
            'date': dt.strftime("%Y-%m-%d")
        })


# Используем отдельный эндпоинт для временных файлов, чтобы не конфликтовать
# с основным обработчиком /audio/<path:filename>
@api.route('/temp_audio/<filename>')
def serve_temp_audio(filename):
    path = os.path.join(TEMP_MP3_FOLDER, filename)
    if os.path.exists(path):
        return send_file(path, mimetype='audio/mpeg')
    return "Файл не найден", 404
