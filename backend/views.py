from flask import render_template, Blueprint, request, jsonify
import os
from datetime import datetime
from backend import config

views = Blueprint('views', __name__)

ALLOWED_EXTENSIONS = {'mp3'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@views.route('/', methods=['GET', 'POST'])
def home_redirector():
    if request.method == 'POST':
        # Получаем данные из формы
        user_folder = request.form.get('user_folder')
        case_number = request.form.get('case_number')
        audio_file = request.files['audio_file']
        audio_date = request.form.get('audio_date')
        audio_time = request.form.get('audio_time')
        closed_session = request.form.get('closed_session')

        # Проверка на пустые поля
        if not user_folder or not case_number or not audio_date or not audio_time or not audio_file:
            return render_template('index.html', directories = os.listdir(config['public_audio_path']), title='Архивация аудиопротоколов', error="Все поля обязательны для заполнения.")

        if not allowed_file(audio_file.filename):
            return render_template('index.html', directories = os.listdir(config['public_audio_path']), title='Архивация аудиопротоколов', error="Неверный формат файла. Поддерживается только MP3.")

        # Формирование пути для хранения файла
        if not closed_session:
            user_folder_path = os.path.join(config['public_audio_path'], user_folder)
        else:
            user_folder_path = os.path.join(config['closed_audio_path'], user_folder)
        case_folder_path = os.path.join(user_folder_path, case_number)

        # Создание директории для пользователя и дела, если их нет
        os.makedirs(case_folder_path, exist_ok=True)

        # Формирование имени файла на основе даты и времени
        timestamp = datetime.strptime(f"{audio_date} {audio_time}", "%Y-%m-%d %H:%M")
        filename = f"{timestamp.strftime('%Y-%m-%d_%H-%M')}.mp3"
        file_path = os.path.join(case_folder_path, filename)

        if os.path.exists(file_path):
            return render_template('index.html', directories=os.listdir(config['public_audio_path']),
                            title='Архивация аудиопротоколов',
                            error="Аудиозапись в указанные время и дату уже существует по этмоу делу. Укажите другие данные.")
        audio_file.save(file_path)

        file_link = os.path.abspath(file_path)

        return render_template('index.html',directories = os.listdir(config['public_audio_path']), title='Архивация аудиопротоколов', success=f"{file_link}")

    return render_template('index.html',directories = os.listdir(config['public_audio_path']), title='Архивация аудиопротоколов')
