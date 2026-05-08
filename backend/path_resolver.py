import os
from datetime import datetime

from backend import config
from backend.db import DB_PATH
from backend.db import Session
from backend.models import AudioRecord


def _norm(value):
    return os.path.normcase(os.path.abspath(value)) if value else ''


def _relative_inside_root(path, root):
    try:
        rel_path = os.path.relpath(path, root)
    except ValueError:
        return None
    normalized = os.path.normpath(rel_path)
    if normalized == os.curdir or (not normalized.startswith(os.pardir + os.sep) and normalized != os.pardir and not os.path.isabs(normalized)):
        return normalize_relative_path(normalized)
    return None


def _storage_roots():
    return [
        ('public', config.get('public_audio_path') or ''),
        ('closed', config.get('closed_audio_path') or ''),
    ]


def is_absolute_path(path):
    return bool(path) and os.path.isabs(path)


def normalize_relative_path(path):
    return os.path.normpath(path).replace('\\', '/')


def absolute_to_relative_path(path):
    if not path:
        return None

    for _storage_name, root in _storage_roots():
        if not root:
            continue
        rel_path = _relative_inside_root(path, root)
        if rel_path:
            return rel_path
    return None


def resolve_storage_path(path, preferred_storage='public'):
    if not path:
        return None, None, None

    if is_absolute_path(path):
        path_abs = os.path.abspath(path)
        for storage_name, root in _storage_roots():
            if not root:
                continue
            root_abs = os.path.abspath(root)
            rel_path = _relative_inside_root(path_abs, root_abs)
            if rel_path:
                return storage_name, path_abs, rel_path
        return None, path_abs, None

    rel_path = normalize_relative_path(path)
    ordered_roots = sorted(
        _storage_roots(),
        key=lambda item: 0 if item[0] == preferred_storage else 1
    )
    fallback = None
    for storage_name, root in ordered_roots:
        if not root:
            continue
        candidate = os.path.abspath(os.path.join(root, rel_path))
        if fallback is None:
            fallback = (storage_name, candidate, rel_path)
        if os.path.exists(candidate):
            return storage_name, candidate, rel_path
    return fallback or (None, rel_path, rel_path)


def resolve_record_audio_path(record):
    return resolve_storage_path(record.file_path, 'public')


def resolve_record_text_path(record):
    if not record.recognized_text_path:
        return None
    storage_name = 'public'
    if record.file_path:
        storage_name = resolve_record_audio_path(record)[0] or 'public'
    return resolve_storage_path(record.recognized_text_path, storage_name)[1]


def record_text_path_for_audio_path(audio_path):
    return os.path.splitext(audio_path)[0] + '.txt'


def create_database_backup(label='path_migration'):
    import sqlite3

    backup_dir = os.path.abspath(os.path.join('backups', label))
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_path = os.path.join(backup_dir, f'audio_archive_before_{label}_{timestamp}.db')

    source = sqlite3.connect(DB_PATH, timeout=60)
    target = sqlite3.connect(backup_path)
    try:
        source.execute('PRAGMA busy_timeout=60000;')
        source.backup(target)
        target.execute('PRAGMA journal_mode=DELETE;')
        target.commit()
        return backup_path
    finally:
        target.close()
        source.close()


def create_path_migration_backup():
    return create_database_backup('path_migration')


def migrate_absolute_paths_to_relative(dry_run=True, create_backup=True):
    session = Session()
    result = {
        'total': 0,
        'file_path_candidates': 0,
        'text_path_candidates': 0,
        'updated': 0,
        'skipped': 0,
        'backup_path': None,
        'errors': []
    }
    try:
        records = session.query(AudioRecord).all()
        result['total'] = len(records)
        for record in records:
            updates = {}
            if is_absolute_path(record.file_path):
                rel_audio = absolute_to_relative_path(record.file_path)
                if rel_audio:
                    duplicate = session.query(AudioRecord).filter(
                        AudioRecord.id != record.id,
                        AudioRecord.file_path == rel_audio
                    ).first()
                    if duplicate:
                        result['errors'].append(
                            f"ID={record.id}: относительный путь уже занят записью ID={duplicate.id}: {rel_audio}"
                        )
                        result['skipped'] += 1
                        continue
                    result['file_path_candidates'] += 1
                    updates['file_path'] = rel_audio

            if record.recognized_text_path and is_absolute_path(record.recognized_text_path):
                rel_text = absolute_to_relative_path(record.recognized_text_path)
                if rel_text:
                    result['text_path_candidates'] += 1
                    updates['recognized_text_path'] = rel_text

            if not updates:
                result['skipped'] += 1
                continue

            if not dry_run:
                for field, value in updates.items():
                    setattr(record, field, value)
            result['updated'] += 1

        if dry_run:
            session.rollback()
        else:
            if create_backup and result['updated'] > 0:
                result['backup_path'] = create_path_migration_backup()
            session.commit()
        return result
    except Exception as exc:
        session.rollback()
        result['errors'].append(str(exc))
        return result
    finally:
        session.close()
