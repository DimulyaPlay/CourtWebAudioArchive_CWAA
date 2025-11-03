import traceback

from flask import Flask
import os
import sys
from datetime import timedelta
from .utils import read_create_config


config = read_create_config()
basedir = os.getcwd()


def create_app():
    try:
        if getattr(sys, 'frozen', False):
            template_folder = os.path.join(sys._MEIPASS, f'frontend/')
            static_folder = os.path.join(sys._MEIPASS, f'frontend/assets')
        else:
            template_folder = os.path.join(basedir, f'frontend')
            static_folder = os.path.join(basedir, 'frontend', 'assets')
        app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
        app.config['SECRET_KEY'] = 'ndfjknsdflkghnfhjkgndbfd dfghmdghnm'
        app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
        app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(seconds=31536000)
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"connect_args": {"check_same_thread": False, "timeout": 60}}
        from .views import views
        from .api import api
        app.register_blueprint(views, url_prefix='/')
        app.register_blueprint(api, url_prefix='/api/')
        return app, ""
    except Exception as e:
        traceback.print_exc()
        return 0, "create_app:"+str(e)
