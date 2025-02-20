from flask import request, jsonify, Blueprint, send_from_directory
import os
from .utils import get_file_hash, compare_files
from . import basedir, config


api = Blueprint('api', __name__)


