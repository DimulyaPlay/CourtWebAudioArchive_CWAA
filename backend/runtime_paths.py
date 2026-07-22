import os


_nginx_audio_roots = {}


def _norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ''


def set_nginx_audio_root(storage_name, root_path):
    if root_path:
        _nginx_audio_roots[storage_name] = os.path.abspath(root_path)
    else:
        _nginx_audio_roots.pop(storage_name, None)


def get_nginx_audio_root(storage_name):
    return _nginx_audio_roots.get(storage_name)


def has_nginx_audio_root_for_config_path(storage_name, config_path):
    runtime_root = get_nginx_audio_root(storage_name)
    return bool(runtime_root and config_path and _norm(runtime_root) != _norm(config_path))


def clear_nginx_audio_roots():
    _nginx_audio_roots.clear()
