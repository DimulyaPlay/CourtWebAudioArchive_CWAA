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
import tempfile
import shutil
import math
import json
from array import array

FFMPEG_SEMAPHORE = Semaphore(2)  # максимум 2 задачи конвертации одновременно
WAVEFORM_PEAK_COUNT = 600
WAVEFORM_SAMPLE_RATE = 8000


api = Blueprint('api', __name__)


def _json_error(message, status=400):
    return jsonify({'error': message}), status


def _download_name_from_cases(case_numbers):
    cleaned = []
    for case_number in sorted({str(item).strip() for item in case_numbers if item}):
        safe = re.sub(r'[\\/:*?"<>|;]+', '_', case_number).strip(' ._')
        if safe:
            cleaned.append(safe)

    if not cleaned:
        return 'records.zip'

    name = '_'.join(cleaned[:3])
    if len(cleaned) > 3:
        name += f'_plus_{len(cleaned) - 3}'
    return f'{name[:120]}.zip'


def _resolve_tool(tool_name):
    local_tool = os.path.join(os.getcwd(), f'{tool_name}.exe')
    if os.path.exists(local_tool):
        return local_tool
    return tool_name


def _run_ffmpeg(cmd):
    cmd = [_resolve_tool('ffmpeg')] + cmd[1:]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _run_ffprobe(file_path):
    cmd = [
        _resolve_tool('ffprobe'),
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'ffprobe failed')
    return float(result.stdout.strip())


def _get_waveform_cache_path(temp_path):
    return f'{temp_path}.waveform.json'


def _build_waveform_peaks(temp_path, duration, peak_count=WAVEFORM_PEAK_COUNT):
    if duration <= 0 or peak_count <= 0:
        return []

    total_samples = max(1, int(math.ceil(duration * WAVEFORM_SAMPLE_RATE)))
    samples_per_peak = max(1, int(math.ceil(total_samples / peak_count)))
    peaks = []
    current_peak = 0.0
    samples_in_bucket = 0

    cmd = [
        _resolve_tool('ffmpeg'),
        '-v', 'error',
        '-i', temp_path,
        '-ac', '1',
        '-ar', str(WAVEFORM_SAMPLE_RATE),
        '-f', 's16le',
        '-acodec', 'pcm_s16le',
        '-'
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break

            if len(chunk) % 2:
                chunk = chunk[:-1]
            if not chunk:
                continue

            samples = array('h')
            samples.frombytes(chunk)
            for sample in samples:
                amplitude = abs(sample) / 32768.0
                if amplitude > current_peak:
                    current_peak = amplitude
                samples_in_bucket += 1
                if samples_in_bucket >= samples_per_peak:
                    peaks.append(round(min(current_peak, 1.0), 4))
                    current_peak = 0.0
                    samples_in_bucket = 0

        if samples_in_bucket:
            peaks.append(round(min(current_peak, 1.0), 4))

        stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
        if process.wait() != 0:
            raise RuntimeError(stderr_output.strip() or 'ffmpeg waveform generation failed')
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    if not peaks:
        return []

    if len(peaks) > peak_count:
        return peaks[:peak_count]
    if len(peaks) < peak_count:
        peaks.extend([0.0] * (peak_count - len(peaks)))
    return peaks


def _get_or_build_waveform_peaks(temp_path, duration, file_size, peak_count=WAVEFORM_PEAK_COUNT):
    cache_path = _get_waveform_cache_path(temp_path)
    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as cache_file:
                cached = json.load(cache_file)
            if (
                cached.get('peak_count') == peak_count
                and cached.get('sample_rate') == WAVEFORM_SAMPLE_RATE
                and int(cached.get('file_size') or 0) == int(file_size or 0)
            ):
                peaks = cached.get('peaks') or []
                if isinstance(peaks, list):
                    return [float(value) for value in peaks[:peak_count]]
    except Exception:
        pass

    try:
        peaks = _build_waveform_peaks(temp_path, duration, peak_count=peak_count)
        with open(cache_path, 'w', encoding='utf-8') as cache_file:
            json.dump({
                'peak_count': peak_count,
                'sample_rate': WAVEFORM_SAMPLE_RATE,
                'duration': round(duration, 3),
                'file_size': int(file_size or 0),
                'peaks': peaks
            }, cache_file, ensure_ascii=False)
        return peaks
    except Exception:
        return []


def _build_temp_asset_response(temp_path, source_name=None, date=None):
    filename = os.path.basename(temp_path)
    file_size = os.path.getsize(temp_path)
    duration = round(_run_ffprobe(temp_path), 3)
    return {
        'temp_id': temp_path,
        'temp_url': f"/api/temp_audio/{filename}",
        'name': source_name or filename,
        'file_size': file_size,
        'duration': duration,
        'waveform_peaks': _get_or_build_waveform_peaks(temp_path, duration, file_size),
        'date': date
    }


def _normalize_segments(duration, trim_start, trim_end, cut_start, cut_end, mode):
    trim_start = max(0.0, min(float(trim_start or 0), duration))
    trim_end = max(trim_start, min(float(trim_end or duration), duration))
    if mode == 'cut':
        cut_start = max(trim_start, min(float(cut_start or trim_start), trim_end))
        cut_end = max(cut_start, min(float(cut_end or trim_end), trim_end))
        segments = []
        if cut_start > trim_start:
            segments.append((trim_start, cut_start))
        if cut_end < trim_end:
            segments.append((cut_end, trim_end))
        return segments
    return [(trim_start, trim_end)] if trim_end > trim_start else []


def _is_source_passthrough(source, epsilon=0.05):
    temp_id = source.get('temp_id')
    if not temp_id or not os.path.exists(temp_id):
        return False
    if source.get('mode', 'trim') != 'trim':
        return False

    duration = _run_ffprobe(temp_id)
    segments = _normalize_segments(
        duration,
        source.get('trim_start', 0),
        source.get('trim_end', duration),
        source.get('cut_start'),
        source.get('cut_end'),
        source.get('mode', 'trim')
    )
    if len(segments) != 1:
        return False

    start, end = segments[0]
    return abs(start) <= epsilon and abs(end - duration) <= epsilon


def _single_source_passthrough_temp_id(sources):
    if len(sources) != 1:
        return None
    source = sources[0]
    temp_id = source.get('temp_id')
    if not temp_id or not os.path.exists(temp_id):
        return None
    if _is_source_passthrough(source):
        return temp_id
    return None


def _sanitize_download_stub(value, fallback):
    safe = re.sub(r'[\\/:*?"<>|]+', '_', str(value or '')).strip(' ._')
    return safe or fallback


def _build_render_download_name(case_number=None, audio_date=None, audio_time=None):
    parts = []
    if case_number:
        parts.append(_sanitize_download_stub(case_number, 'audio'))
    if audio_date:
        parts.append(_sanitize_download_stub(audio_date, 'date'))
    if audio_time:
        parts.append(_sanitize_download_stub(str(audio_time).replace(':', '-'), 'time'))
    if not parts:
        parts.append(f'edited_{datetime.now().strftime("%Y-%m-%d_%H-%M")}')
    return f'{"_".join(parts)[:120]}.mp3'


def _render_sources_to_temp_file(sources):
    if not sources:
        raise ValueError('sources required')

    passthrough_temp_id = _single_source_passthrough_temp_id(sources)
    if passthrough_temp_id:
        return passthrough_temp_id

    job_dir = tempfile.mkdtemp(prefix='cwaa_edit_', dir=TEMP_MP3_FOLDER)
    concat_manifest = os.path.join(job_dir, 'concat.txt')
    segment_files = []
    try:
        with FFMPEG_SEMAPHORE:
            for index, source in enumerate(sources):
                temp_id = source.get('temp_id')
                source_name = source.get('name') or f'Источник {index + 1}'
                if not temp_id or not os.path.exists(temp_id):
                    raise ValueError(f'Временный файл не найден: {source_name}')

                duration = _run_ffprobe(temp_id)
                segments = _normalize_segments(
                    duration,
                    source.get('trim_start', 0),
                    source.get('trim_end', duration),
                    source.get('cut_start'),
                    source.get('cut_end'),
                    source.get('mode', 'trim')
                )
                if not segments:
                    continue

                for seg_index, (start, end) in enumerate(segments):
                    segment_path = os.path.join(job_dir, f'segment_{index}_{seg_index}.mp3')
                    cmd = [
                        'ffmpeg', '-y',
                        '-ss', f'{start:.3f}',
                        '-to', f'{end:.3f}',
                        '-i', temp_id,
                        '-ac', '1',
                        '-ar', '16000',
                        '-q:a', '2',
                        segment_path
                    ]
                    result = _run_ffmpeg(cmd)
                    if result.returncode != 0:
                        raise RuntimeError(f'Ошибка ffmpeg при обработке "{source_name}": {result.stderr[:200]}')
                    segment_files.append(segment_path)

            if not segment_files:
                raise ValueError('После обрезки не осталось аудио')

            with open(concat_manifest, 'w', encoding='utf-8') as f:
                for segment_path in segment_files:
                    escaped = segment_path.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            final_path = os.path.join(TEMP_MP3_FOLDER, f'edited_{uuid.uuid4().hex}.mp3')
            concat_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_manifest,
                '-c', 'copy',
                final_path
            ]
            concat_result = _run_ffmpeg(concat_cmd)
            if concat_result.returncode != 0:
                concat_cmd = [
                    'ffmpeg', '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', concat_manifest,
                    '-ac', '1',
                    '-ar', '16000',
                    '-q:a', '2',
                    final_path
                ]
                concat_result = _run_ffmpeg(concat_cmd)
                if concat_result.returncode != 0:
                    raise RuntimeError(f'Ошибка ffmpeg при склейке: {concat_result.stderr[:200]}')

        return final_path
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@api.route('/search')
def search_records():
    with Session() as session:
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

    with Session() as session:
        records = session.query(AudioRecord).filter(AudioRecord.id.in_(ids)).all()
        files = [
            {
                "id": rec.id,
                "file_path": rec.file_path,
                "case_number": rec.case_number
            }
            for rec in records
        ]
        if records:
            client_ip = request.headers.get('X-Real-IP') or request.remote_addr or 'unknown'
            now = datetime.utcnow()
            for rec in records:
                recent = session.query(DownloadLog).filter(
                    DownloadLog.record_id == rec.id,
                    DownloadLog.ip == client_ip,
                    DownloadLog.timestamp > now - timedelta(minutes=10)
                ).first()
                if not recent:
                    session.add(DownloadLog(record_id=rec.id, ip=client_ip, timestamp=now))
            session.commit()

    if len(files) == 1:
        file_path = files[0]["file_path"]
        if not os.path.exists(file_path):
            return "Файл не найден.", 404
        return send_file(file_path, as_attachment=True)

    # Несколько файлов: архивируем
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        case_list = set()
        for f in files:
            if os.path.exists(f["file_path"]):
                case_list.add(f["case_number"])
                zf.write(
                    f["file_path"],
                    os.path.basename(f["file_path"])
                )
    print("финальный сет:", case_list)
    download_name = _download_name_from_cases(case_list)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype='application/zip', download_name=download_name, as_attachment=True)


@api.route('/record/<int:record_id>')
def get_record_data(record_id):
    with Session() as session:
        record = session.get(AudioRecord, record_id)
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
        'phrases': phrases,
        'is_in_recognition_queue': bool(
            record.recognize_text and (
                not record.recognized_text_path or
                not os.path.exists(record.recognized_text_path)
            )
        )
    })


@api.route('/reset_transcription/<int:record_id>', methods=['POST'])
def reset_transcription(record_id):
    session = Session()
    record = session.get(AudioRecord, int(record_id))
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
    with Session() as session:
        count = session.query(AudioRecord).filter(
            AudioRecord.recognize_text.is_(True),
            AudioRecord.recognized_text_path.is_(None)
        ).count()
    return jsonify({'records_to_vr': count})


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
            result = _run_ffmpeg(cmd)
            if result.returncode != 0:
                return jsonify({'error': f'ffmpeg concat error: {result.stderr[180:]}'}), 500
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
        result = _run_ffmpeg(mix_cmd)
        if result.returncode != 0:
            return jsonify({'error': f'ffmpeg mix error: {result.stderr[:200]}'}), 500
        # Извлекаем дату из названия кейса (например: from 18-04-2025)
        base_name = os.path.basename(case_path)
        dt_match = re.search(r'from\s+(\d{2})-(\d{2})-(\d{4})', base_name)
        if dt_match:
            dt = datetime.strptime('-'.join(dt_match.groups()), "%d-%m-%Y")
        else:
            dt = datetime.now()

        return jsonify(_build_temp_asset_response(
            final_tmp,
            source_name=base_name,
            date=dt.strftime("%Y-%m-%d")
        ))


@api.route('/temp_upload_audio', methods=['POST'])
def temp_upload_audio():
    files = request.files.getlist('audio_files')
    if not files:
        return jsonify({'error': 'files required'}), 400

    uploaded = []
    for file in files:
        if not file or not file.filename:
            continue
        if not file.filename.lower().endswith('.mp3'):
            return jsonify({'error': f'Неверный формат файла: {file.filename}'}), 400

        temp_name = f"upload_{uuid.uuid4().hex}.mp3"
        temp_path = os.path.join(TEMP_MP3_FOLDER, temp_name)
        file.save(temp_path)
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 3000:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return jsonify({'error': f'Файл слишком короткий или пустой: {file.filename}'}), 400

        uploaded.append(_build_temp_asset_response(temp_path, source_name=file.filename))

    return jsonify({'items': uploaded})


@api.route('/render_edit', methods=['POST'])
def render_edit():
    payload = request.get_json(silent=True) or {}
    sources = payload.get('sources') or []
    if not sources:
        return jsonify({'error': 'sources required'}), 400

    passthrough_temp_id = _single_source_passthrough_temp_id(sources)
    if passthrough_temp_id:
        return jsonify({'temp_id': passthrough_temp_id})

    job_dir = tempfile.mkdtemp(prefix='cwaa_edit_', dir=TEMP_MP3_FOLDER)
    concat_manifest = os.path.join(job_dir, 'concat.txt')
    segment_files = []
    try:
        with FFMPEG_SEMAPHORE:
            for index, source in enumerate(sources):
                temp_id = source.get('temp_id')
                source_name = source.get('name') or f'Источник {index + 1}'
                if not temp_id or not os.path.exists(temp_id):
                    return jsonify({'error': f'Временный файл не найден: {source_name}'}), 400

                duration = _run_ffprobe(temp_id)
                segments = _normalize_segments(
                    duration,
                    source.get('trim_start', 0),
                    source.get('trim_end', duration),
                    source.get('cut_start'),
                    source.get('cut_end'),
                    source.get('mode', 'trim')
                )
                if not segments:
                    continue

                for seg_index, (start, end) in enumerate(segments):
                    segment_path = os.path.join(job_dir, f'segment_{index}_{seg_index}.mp3')
                    cmd = [
                        'ffmpeg', '-y',
                        '-ss', f'{start:.3f}',
                        '-to', f'{end:.3f}',
                        '-i', temp_id,
                        '-ac', '1',
                        '-ar', '16000',
                        '-q:a', '2',
                        segment_path
                    ]
                    result = _run_ffmpeg(cmd)
                    if result.returncode != 0:
                        return jsonify({'error': f'Ошибка ffmpeg при обработке "{source_name}": {result.stderr[:200]}'}), 500
                    segment_files.append(segment_path)

            if not segment_files:
                return jsonify({'error': 'После обрезки не осталось аудио'}), 400

            with open(concat_manifest, 'w', encoding='utf-8') as f:
                for segment_path in segment_files:
                    escaped = segment_path.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            final_path = os.path.join(TEMP_MP3_FOLDER, f'edited_{uuid.uuid4().hex}.mp3')
            concat_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_manifest,
                '-c', 'copy',
                final_path
            ]
            concat_result = _run_ffmpeg(concat_cmd)
            if concat_result.returncode != 0:
                concat_cmd = [
                    'ffmpeg', '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', concat_manifest,
                    '-ac', '1',
                    '-ar', '16000',
                    '-q:a', '2',
                    final_path
                ]
                concat_result = _run_ffmpeg(concat_cmd)
                if concat_result.returncode != 0:
                    return jsonify({'error': f'Ошибка ffmpeg при склейке: {concat_result.stderr[:200]}'}), 500

        return jsonify(_build_temp_asset_response(final_path, source_name='edited.mp3'))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# Используем отдельный эндпоинт для временных файлов, чтобы не конфликтовать
# с основным обработчиком /audio/<path:filename>
@api.route('/download_rendered_edit', methods=['POST'])
def download_rendered_edit():
    payload = request.get_json(silent=True) or {}
    sources = payload.get('sources') or []
    try:
        final_path = _render_sources_to_temp_file(sources)
        download_name = _build_render_download_name(
            payload.get('case_number'),
            payload.get('audio_date'),
            payload.get('audio_time')
        )
        return send_file(
            final_path,
            mimetype='audio/mpeg',
            as_attachment=True,
            download_name=download_name
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 500


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
    with Session() as session:
        record = session.get(AudioRecord, record_id)
    if not record or not record.recognized_text_path or not os.path.exists(record.recognized_text_path):
        return "Протокол не найден или отсутствует текст", 404
    phrases = parse_transcript_file(record.recognized_text_path)
    rtf_text = phrases_to_rtf(phrases)
    payload = io.BytesIO(rtf_text.encode('cp1251', errors='ignore'))
    payload.seek(0)
    return send_file(
        payload,
        as_attachment=True,
        mimetype='application/rtf',
        download_name=f"{record.case_number}_{record.audio_date.strftime('%Y-%m-%d_%H-%M')}.rtf"
    )


@api.route('/add_replacement_rule', methods=['POST'])
def add_replacement_rule():
    payload = request.get_json(silent=True) or {}
    from_text = payload.get('from')
    to_text = payload.get('to')
    record_id = payload.get('record_id')
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
    record = session.get(AudioRecord, int(record_id))
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
    payload = request.get_json(silent=True) or {}
    record_id = payload.get('record_id')
    old_text = payload.get('original')
    rule_num = payload.get('rule')
    if not record_id or not old_text or not rule_num:
        return jsonify({'error': 'Недостаточно данных'}), 400
    session = Session()
    record = session.get(AudioRecord, int(record_id))
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
    data = request.get_json(silent=True) or {}
    record_id = data.get('record_id')
    session = Session()
    record = session.get(AudioRecord, int(record_id))
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
