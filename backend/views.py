from flask import render_template, Blueprint, request, jsonify
import os
from datetime import datetime
from pydub import AudioSegment
from backend import config
from backend.db import Session
from backend.models import AudioRecord
from backend.utils import get_available_courtrooms

views = Blueprint('views', __name__)

ALLOWED_EXTENSIONS = {'mp3'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@views.route('/', methods=['GET', 'POST'])
def home_redirector():
    if request.method == 'POST':
        user_folder = request.form.get('user_folder')
        case_number = request.form.get('case_number')
        audio_file = request.files['audio_file']
        audio_date = request.form.get('audio_date')
        audio_time = request.form.get('audio_time')
        courtroom = request.form.get('courtroom')
        comment = request.form.get('comment')
        recognize_text = request.form.get('recognize_text')
        closed_session = request.form.get('closed_session')
        if not user_folder or not case_number or not audio_date or not audio_time or not audio_file:
            return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                                   courtrooms=get_available_courtrooms(),
                                   title='Архивация аудиопротоколов',
                                   error="Все поля обязательны для заполнения.")
        if not allowed_file(audio_file.filename):
            return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                                   courtrooms=get_available_courtrooms(),
                                   title='Архивация аудиопротоколов',
                                   error="Неверный формат файла. Поддерживается только MP3.")
        try:
            audio_segment = AudioSegment.from_file(audio_file.stream, format="mp3")
            duration_sec = len(audio_segment) / 1000
            if duration_sec < 3:
                return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                                       courtrooms=get_available_courtrooms(),
                                       title='Архивация аудиопротоколов',
                                       error="Файл слишком короткий. Минимальная длина — 3 секунды.")
        except Exception as e:
            return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                                   courtrooms=get_available_courtrooms(),
                                   title='Архивация аудиопротоколов',
                                   error="Файл поврежден или не является корректным MP3.")
        # Определяем базовый путь
        base_path = config['closed_audio_path'] if closed_session else config['public_audio_path']
        user_folder_path = os.path.join(base_path, user_folder)

        # Получаем год из даты
        try:
            timestamp = datetime.strptime(f"{audio_date} {audio_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                                   courtrooms=get_available_courtrooms(),
                                   title='Архивация аудиопротоколов',
                                   error="Неверный формат даты или времени.")

        # Добавляем подпапку года, если флаг включен
        if config.get('create_year_subfolders', 'false') == 'true':
            year_folder = str(timestamp.year)
            case_folder_path = os.path.join(user_folder_path, year_folder, case_number)
        else:
            case_folder_path = os.path.join(user_folder_path, case_number)

        os.makedirs(case_folder_path, exist_ok=True)

        filename = f"{timestamp.strftime('%Y-%m-%d_%H-%M')}.mp3"
        file_path = os.path.join(case_folder_path, filename)

        if os.path.exists(file_path):
            return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                                   courtrooms=get_available_courtrooms(),
                                   title='Архивация аудиопротоколов',
                                   error="Аудиозапись в указанные время и дату уже существует по этому делу. Укажите другие данные.")

        audio_file.save(file_path)
        if not bool(closed_session):
            session = Session()
            record = AudioRecord(
                user_folder=user_folder,
                case_number=case_number,
                audio_date=timestamp,
                file_path=file_path,
                comment=comment,
                courtroom=courtroom,
                recognize_text = bool(recognize_text) if not bool(closed_session) else False
            )
            session.add(record)
            session.commit()
            session.close()
        file_link = os.path.abspath(file_path)
        return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                               courtrooms=get_available_courtrooms(),
                               title='Архивация аудиопротоколов',
                               success=f"{file_link}")

    return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                           courtrooms=get_available_courtrooms(),
                           title='Архивация аудиопротоколов')


@views.route('/archive')
def archive_viewer():
    return render_template('archive_viewer.html',
                           directories=os.listdir(config['public_audio_path']),
                           courtrooms=get_available_courtrooms(),
                           title="Архив аудиопротоколов")