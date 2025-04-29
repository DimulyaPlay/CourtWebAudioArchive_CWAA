from flask import request, jsonify, Blueprint, send_from_directory, send_file
import os
from backend.utils import get_file_hash, compare_files, parse_transcript_file
from . import config
from backend.db import Session, engine
from backend.models import AudioRecord
from sqlalchemy import text, desc
import zipfile
import io
from pathlib import Path
from datetime import timedelta, datetime


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
