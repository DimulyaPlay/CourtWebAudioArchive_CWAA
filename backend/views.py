from flask import render_template, Blueprint, request, jsonify
import os
from datetime import datetime
import tempfile
from werkzeug.utils import secure_filename
import shutil
from pydub import AudioSegment
from backend import config
from backend.db import Session
from backend.models import AudioRecord
from backend.utils import get_available_courtrooms

views = Blueprint('views', __name__)

ALLOWED_EXTENSIONS = {'mp3'}
MIN_SIZE_BYTES = 3000  # 3 секунды в mp3 примерно столько (32000 битрейт ≈ 4кБ/сек)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@views.route('/', methods=['GET', 'POST'])
def home_redirector(ajax=False):
    if request.method == 'GET':
        return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                               courtrooms=get_available_courtrooms(),
                               title='Архивация аудиопротоколов')
    if request.method == 'POST':
        user_folder = request.form.get('user_folder')
        case_number = request.form.get('case_number')
        audio_file = request.files.get('audio_file')
        audio_date = request.form.get('audio_date')
        audio_time = request.form.get('audio_time')
        courtroom = request.form.get('courtroom')
        comment = request.form.get('comment')
        recognize_text = request.form.get('recognize_text')
        closed_session = request.form.get('closed_session')
        imported_temp_id = request.form.get('imported_temp_id')
        temp_path = None
        if not user_folder or not case_number or not audio_date or not audio_time:
            if ajax:
                return jsonify({'error': "Все поля обязательны для заполнения."}), 400

        if not audio_file and not imported_temp_id:
            if ajax:
                return jsonify({'error': "Не выбран файл и не прикреплена запись из Фемиды."}), 400

        try:
            if imported_temp_id:
                if not os.path.exists(imported_temp_id):
                    if ajax:
                        return jsonify({'error': "Временный файл от Фемиды не найден."}), 400
                temp_path = imported_temp_id
            else:
                if not allowed_file(audio_file.filename):
                    if ajax:
                        return jsonify({'error': "Неверный формат файла. Поддерживается только MP3."}), 400

                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                temp_path = tmp.name
                tmp.close()
                audio_file.save(temp_path)
                file_size = os.path.getsize(temp_path)
                if file_size < MIN_SIZE_BYTES:
                    os.remove(temp_path)
                    if ajax:
                        return jsonify({'error': "Файл слишком короткий или пустой."}), 400
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            if ajax:
                return jsonify({'error': "Ошибка при обработке файла"}), 400

        try:
            timestamp = datetime.strptime(f"{audio_date} {audio_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            os.remove(temp_path)
            if ajax:
                return jsonify({'error': "Неверный формат даты или времени."}), 400

        base_path = config['closed_audio_path'] if closed_session else config['public_audio_path']
        user_folder_path = os.path.join(base_path, user_folder)
        if config.get('create_year_subfolders', 'false') == 'true':
            year_folder = str(timestamp.year)
            case_folder_path = os.path.join(user_folder_path, year_folder, case_number)
        else:
            case_folder_path = os.path.join(user_folder_path, case_number)
        os.makedirs(case_folder_path, exist_ok=True)

        filename = f"{timestamp.strftime('%Y-%m-%d_%H-%M')}.mp3"
        file_path = os.path.join(case_folder_path, filename)

        if os.path.exists(file_path):
            os.remove(temp_path)
            if ajax:
                return jsonify({'error': "Аудиозапись в указанные время и дату уже существует по этому делу. Укажите другие данные."}), 400

        shutil.move(temp_path, file_path)

        record_id = None
        if not bool(closed_session):
            session = Session()
            record = AudioRecord(
                user_folder=user_folder,
                case_number=case_number,
                audio_date=timestamp,
                file_path=file_path,
                comment=comment,
                courtroom=courtroom,
                recognize_text=bool(recognize_text)
            )
            session.add(record)
            session.commit()
            # Получение ID записи перед закрытием сессии
            record_id = record.id
            session.close()

        if ajax:
            return jsonify({'success': os.path.abspath(file_path),
                            'id': record_id})

    return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                           courtrooms=get_available_courtrooms(),
                           title='Архивация аудиопротоколов')


@views.route('/archive')
def archive_viewer():
    return render_template('archive_viewer.html',
                           directories=os.listdir(config['public_audio_path']),
                           courtrooms=get_available_courtrooms(),
                           title="Архив аудиопротоколов")


@views.route('/upload_audio', methods=['POST'])
def upload_audio_ajax():
    return home_redirector(ajax=True)
