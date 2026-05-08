from collections import defaultdict

from backend.db import Session
from backend.models import AudioRecord
from backend.path_resolver import (
    absolute_to_relative_path,
    create_database_backup,
    is_absolute_path,
    normalize_relative_path
)


def _canonical_path(path):
    if not path:
        return ''
    if is_absolute_path(path):
        return absolute_to_relative_path(path) or path.lower()
    return normalize_relative_path(path).lower()


def _record_payload(record):
    return {
        'id': record.id,
        'user_folder': record.user_folder,
        'case_number': record.case_number,
        'audio_date': record.audio_date.strftime('%Y-%m-%d %H:%M:%S') if record.audio_date else '',
        'file_path': record.file_path,
        'comment': record.comment or '',
        'courtroom': record.courtroom or '',
        'recognize_text': bool(record.recognize_text),
        'recognized_text_path': record.recognized_text_path or '',
        'uploaded_at': record.uploaded_at.strftime('%Y-%m-%d %H:%M:%S') if record.uploaded_at else '',
        'uploaded_ip': record.uploaded_ip or ''
    }


def find_duplicate_conflicts():
    session = Session()
    try:
        records = session.query(AudioRecord).order_by(AudioRecord.id).all()
        groups_by_key = defaultdict(list)

        for record in records:
            event_key = ('event', record.user_folder, record.case_number, record.audio_date)
            groups_by_key[event_key].append(record.id)

            path_key = ('path', _canonical_path(record.file_path))
            if path_key[1]:
                groups_by_key[path_key].append(record.id)

        conflict_id_sets = []
        seen = set()
        for ids in groups_by_key.values():
            unique_ids = tuple(sorted(set(ids)))
            if len(unique_ids) < 2 or unique_ids in seen:
                continue
            seen.add(unique_ids)
            conflict_id_sets.append(unique_ids)

        conflicts = []
        for index, ids in enumerate(conflict_id_sets, start=1):
            conflict_records = session.query(AudioRecord).filter(AudioRecord.id.in_(ids)).order_by(AudioRecord.id).all()
            first = conflict_records[0]
            conflicts.append({
                'index': index,
                'ids': list(ids),
                'title': (
                    f"{first.user_folder} / {first.case_number} / "
                    f"{first.audio_date.strftime('%Y-%m-%d %H:%M') if first.audio_date else ''}"
                ),
                'records': [_record_payload(record) for record in conflict_records]
            })
        return conflicts
    finally:
        session.close()


def _merge_into_keep(keep, duplicate):
    if not keep.recognized_text_path and duplicate.recognized_text_path:
        keep.recognized_text_path = duplicate.recognized_text_path
    keep.recognize_text = bool(keep.recognize_text or duplicate.recognize_text)

    if not keep.courtroom and duplicate.courtroom:
        keep.courtroom = duplicate.courtroom

    duplicate_comment = (duplicate.comment or '').strip()
    keep_comment = (keep.comment or '').strip()
    if duplicate_comment and duplicate_comment != keep_comment:
        keep.comment = f"{keep_comment}\n{duplicate_comment}" if keep_comment else duplicate_comment

    if not keep.uploaded_ip and duplicate.uploaded_ip:
        keep.uploaded_ip = duplicate.uploaded_ip


def resolve_duplicate_conflict(ids, keep_id, merge=False, create_backup=True):
    ids = [int(item) for item in ids]
    keep_id = int(keep_id)
    if keep_id not in ids:
        raise ValueError("Выбранная запись не входит в конфликт")

    session = Session()
    backup_path = None
    try:
        records = session.query(AudioRecord).filter(AudioRecord.id.in_(ids)).all()
        if len(records) != len(set(ids)):
            raise ValueError("Не все записи конфликта найдены")

        record_by_id = {record.id: record for record in records}
        keep = record_by_id[keep_id]
        duplicates = [record for record in records if record.id != keep_id]

        if create_backup:
            backup_path = create_database_backup('duplicate_resolution')

        if merge:
            for duplicate in duplicates:
                _merge_into_keep(keep, duplicate)

        deleted_ids = [record.id for record in duplicates]
        for duplicate in duplicates:
            session.delete(duplicate)

        session.commit()
        return {
            'backup_path': backup_path,
            'keep_id': keep_id,
            'deleted_ids': deleted_ids
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
