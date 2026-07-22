---
name: cwaa-verify-before-handoff
description: Verify CWAA changes before final handoff with focused Python compile, JavaScript syntax, route/contract, file-path, audio-processing, and smoke checks.
---

# CWAA Verify Before Handoff

Use this skill after making CWAA changes and before the final response. Match checks to the blast radius and avoid claiming full application validation unless it was actually run.

## Choose Checks

Use the existing Python environment. Prefer `.venv\Scripts\python.exe`; if unavailable, try `python`.

For changed Python files:

```powershell
.venv\Scripts\python.exe -m py_compile "CWAA Server.py" backend\api.py backend\views.py
```

For changed JavaScript files:

```powershell
node --check frontend\assets\archive_viewer.js
```

For HTML/template changes, there is no template compiler in this repo. Search changed ids, selectors, globals, and route names across frontend and backend:

```powershell
rg "changed_name|changed_selector|changed_route" backend frontend
```

For Flask route/API changes, prefer a small Flask test-client smoke when importable. At minimum, verify the changed endpoint and one related page route. Avoid tests that mutate `audio_archive.db` unless the task requires it and a backup/test database strategy is clear.

## MCP Index Checks

Use `cwaa-codebase-memory-mcp` after verification, especially for multi-file code changes.

- Before relying on graph output, confirm `index_status`, or create the CWAA index if missing.
- After substantial code/route/JS/contract changes, run `index_repository` for the CWAA root.
- After indexing, run `detect_changes` to review changed files and impacted symbols.
- For documentation-only changes, report that MCP refresh was not needed unless the instructions themselves changed MCP behavior.
- Never run `delete_project` as a verification step.

## Contract Checks

If a field, route, CSS selector, DOM id, JS global, config key, database column, or response shape changed, search for the old and new names:

```powershell
rg "old_name|new_name" backend frontend "CWAA Server.py"
```

Check affected layers:

- backend producer in `backend/views.py`, `backend/api.py`, or `backend/__init__.py`;
- frontend caller/renderer in `frontend/index.html`, `frontend/archive_viewer.html`, or `frontend/assets/archive_viewer.js`;
- database model and patch logic in `backend/models.py` and `backend/db.py`;
- path conversion and containment in `backend/path_resolver.py`;
- config/default behavior in `backend/utils.py`, `config.txt`, or UI controls in `CWAA Server.py`;
- empty/error/loading states and Russian user-facing text.

## Domain-Specific Checks

- Startup/routing: verify `backend/__init__.py`, `/healthz`, Waitress internal port selection, nginx external route, and controlled shutdown if changed.
- File serving/downloads: verify `_relative_path_inside`, `_x_accel_redirect`, `resolve_record_audio_path`, `resolve_record_text_path`, public/closed storage behavior, and UNC fallback if touched.
- Audio conversion/editing: verify ffmpeg/ffprobe command construction, `FFMPEG_SEMAPHORE`, temp-file cleanup, waveform cache behavior, and download names.
- Upload/archive flow: verify MP3 validation, date/time checks, duplicate detection, metadata writing, public versus closed session behavior, and `create_year_subfolders`.
- Recognition: verify `recognize_text_enabled`, queue count, `reset_transcription`, ASR executable detection, copy-back of TXT, replacement tags, and cleanup of ASR artifacts.
- Database/path migration: verify SQLite backup creation, idempotent column/index patching, relative path conversion, duplicate conflict detection, and rollback behavior.
- Backup service: verify backup config parsing, retention, selected include flags, SQLite snapshot behavior, and UNC/local path assumptions.
- Frontend archive viewer: verify search filters, playback, transcript render, replacement rule actions, downloads, and retranscription controls.
- PyInstaller packaging: verify `.spec`, `sys._MEIPASS`, included assets, nginx files, local ffmpeg/ffprobe, and icon handling if packaging-related files changed.

## Final Handoff Format

Report only high-signal facts:

- what changed;
- what checks passed;
- whether the MCP index was refreshed or why it was not needed;
- what was not checked and why;
- any remaining risk or follow-up that matters.

Do not claim a manual UI check, full app run, database validation, nginx check, audio conversion, ASR run, or packaging build unless it was actually performed.
