from flask import request, jsonify, Blueprint, send_file
import os
from backend.utils import (
    parse_transcript_file,
    TEMP_MP3_FOLDER
)
from backend.recognition_orchestrator import (
    load_phrase_replacement_rules,
    apply_replacement_with_tags,
    strip_replacement_tags
)
from . import config
from backend.db import Session
from backend.models import AudioRecord, DownloadLog
from sqlalchemy import desc
import zipfile
import io
from pathlib import Path
from datetime import timedelta, datetime
import subprocess
import re
from threading import Semaphore
import uuid

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

    for rec in records:
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
    if records:
        client_ip = request.headers.get('X-Real-IP')
        now = datetime.utcnow()
        for rec in records:
            recent = session.query(DownloadLog).filter(
                DownloadLog.record_id == rec.id,
                DownloadLog.ip == client_ip,
                DownloadLog.timestamp > now - timedelta(minutes=10)
            ).first()
            if not recent:
                log = DownloadLog(record_id=rec.id, ip=client_ip, timestamp=now)
                session.add(log)
        session.commit()
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
            '-filter_complex', f'amix=inputs={len(intermediate_files)}:duration=longest:dropout_transition=2,volume=20dB',
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


@api.route('/export_text/<int:record_id>')
def export_text(record_id):
    def strip_replace_tags(text):
        """Заменяет <replace>...</replace> на содержимое <new>...</new>"""
        return re.sub(
            r'<replace><old>.*?</old><new>(.*?)</new><rule>\d+</rule></replace>',
            r'\1',
            text,
            flags=re.DOTALL
        )
    def clean_to_cp1251(text):
        """Удаляет все символы, не входящие в CP1251"""
        return ''.join(ch for ch in text if ch.encode('cp1251', errors='ignore'))

    def phrases_to_rtf(phrases):
        lines = [
            clean_to_cp1251(
                strip_replace_tags(p['text'])
            ).replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}')
            for p in phrases
        ]
        rtf_body = '\\par\n'.join(lines)
        return (
            r'{\rtf1\ansi\ansicpg1251\deff0'
            r'{\fonttbl{\f0 Times New Roman;}}'
            r'\deflang1049'
            r'{\f0\fs24\n' + rtf_body + '\n}}'
        )
    session = Session()
    record = session.query(AudioRecord).get(record_id)
    session.close()
    if not record or not record.recognized_text_path or not os.path.exists(record.recognized_text_path):
        return "Протокол не найден или отсутствует текст", 404
    phrases = parse_transcript_file(record.recognized_text_path)
    rtf_text = phrases_to_rtf(phrases)
    filename = f"transcript_{uuid.uuid4().hex}.rtf"
    temp_path = os.path.join(TEMP_MP3_FOLDER, filename)
    with open(temp_path, 'w', encoding='cp1251') as f:
        f.write(rtf_text)
    return send_file(
        temp_path,
        as_attachment=True,
        download_name=f"{record.case_number}_{record.audio_date.strftime('%Y-%m-%d_%H-%M')}.rtf"
    )


@api.route('/add_replacement_rule', methods=['POST'])
def add_replacement_rule():
    from_text = request.json.get('from')
    to_text = request.json.get('to')
    record_id = request.json.get('record_id')
    if not from_text or not to_text or not record_id:
        return jsonify({'error': 'Неверные данные'}), 400
    path = os.path.join('assets', 'phraseReplacement.txt')
    rule_index = None
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    for idx, line in enumerate(lines):
        if '>' in line and line.strip().endswith(';'):
            parts = line.strip()[:-1].split('>')
            *wrongs, correct = [p.strip() for p in parts]
            if correct == to_text:
                if from_text not in wrongs:
                    wrongs.append(from_text)
                    lines[idx] = '>'.join(wrongs + [correct]) + ';\n'
                    with open(path, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
                rule_index = idx + 1
                break
    if rule_index is None:
        lines.append(f"{from_text}>{to_text};\n")
        rule_index = len(lines)
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
    session = Session()
    record = session.query(AudioRecord).get(record_id)
    if not record or not record.recognized_text_path or not os.path.exists(record.recognized_text_path):
        session.close()
        return jsonify({'error': 'Запись не найдена'}), 404
    with open(record.recognized_text_path, 'r', encoding='utf-8') as f:
        old_text = f.read()
    rules = load_phrase_replacement_rules(force_reload=True)
    new_tagged = apply_replacement_with_tags(strip_replacement_tags(old_text), rules)
    with open(record.recognized_text_path, 'w', encoding='utf-8') as f:
        f.write(new_tagged)
    session.close()
    return jsonify({'rule_index': rule_index})



@api.route('/undo_replacement', methods=['POST'])
def undo_replacement():
    record_id = request.json.get('record_id')
    old_text = request.json.get('original')
    rule_num = request.json.get('rule')
    if not record_id or not old_text or not rule_num:
        return jsonify({'error': 'Недостаточно данных'}), 400
    session = Session()
    record = session.query(AudioRecord).get(record_id)
    if not record or not record.recognized_text_path or not os.path.exists(record.recognized_text_path):
        session.close()
        return jsonify({'error': 'Файл не найден'}), 404
    path = record.recognized_text_path
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    pattern = (
        r'<replace><old>' + re.escape(old_text) +
        r'</old><new>.*?</new><rule>' + re.escape(str(rule_num)) + r'</rule></replace>'
    )
    new_content, count = re.subn(pattern, old_text, content)
    if count == 0:
        session.close()
        return jsonify({'error': 'Совпадение не найдено'}), 404
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    session.close()
    return jsonify({'status': 'ok'})


@api.route('/reapply_rules', methods=['POST'])
def reapply_rules():
    data = request.get_json()
    record_id = data.get('record_id')
    session = Session()
    record = session.query(AudioRecord).get(record_id)
    if not record or not record.recognized_text_path or not os.path.exists(record.recognized_text_path):
        session.close()
        return jsonify({'error': 'Запись не найдена'}), 404
    with open(record.recognized_text_path, 'r', encoding='utf-8') as f:
        content = f.read()
    base_text = strip_replacement_tags(content)
    rules = load_phrase_replacement_rules(force_reload=True)
    new_text = apply_replacement_with_tags(base_text, rules)
    with open(record.recognized_text_path, 'w', encoding='utf-8') as f:
        f.write(new_text)
    session.close()
    return jsonify({'status': 'ok'})
