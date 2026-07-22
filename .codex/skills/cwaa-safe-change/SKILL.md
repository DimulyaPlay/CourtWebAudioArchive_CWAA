---
name: cwaa-safe-change
description: Work safely on CWAA code, configs, templates, scripts, or docs with narrow edits, dirty-worktree awareness, local-data safety, and rollback-safe habits. Use before code changes, configuration changes, route changes, database-facing changes, file/path handling changes, audio processing changes, or service lifecycle changes.
---

# CWAA Safe Change

Use this skill as the default guardrail before editing Court Web Audio Archive (CWAA). The application manages local court audio archives, a SQLite database, network shares, generated audio/text files, backups, and service processes, so edits should be narrow and verified.

## Default Workflow

1. Check the current working tree with `git status --short` before editing. Treat existing changes as user-owned unless you made them in this task.
2. Use `cwaa-codebase-memory-mcp` before broad exploration:
   - `list_projects` / `index_status` to confirm the graph is ready, or `index_repository` if CWAA has not been indexed yet;
   - `get_architecture` for unfamiliar areas;
   - `search_graph` / `search_code` to locate symbols, routes, files, and Russian UI text;
   - `trace_path` to inspect callers/callees before contract changes.
3. Confirm graph findings with `rg` / `rg --files` and by reading source files. Read both the backend producer and frontend consumer before changing an endpoint or response shape.
4. Make the smallest coherent change that solves the task. Keep edits close to the existing module and style.
5. Use focused verification before handoff; use `cwaa-verify-before-handoff`.
6. Refresh the MCP index after substantial code or structure changes.

## Hard Stops

Stop and ask before doing any of these unless the user explicitly requested it:

- deleting or rewriting large directories/files;
- changing `audio_archive.db`, `audio_archive.db-wal`, `audio_archive.db-shm`, archive folders, generated transcripts, temporary audio files, or backup archives directly;
- running destructive git commands or reverting user changes;
- changing nginx/Waitress routing, ports, startup order, process shutdown, or `/healthz` semantics;
- changing database uniqueness rules, path migration, duplicate resolution, or backup retention without checking current data-safety behavior;
- widening file serving paths or `X-Accel-Redirect` handling without proving paths stay inside configured storage roots;
- changing public response formats without checking all frontend consumers;
- changing ffmpeg/GigaAM invocation, temp-file cleanup, or recognition queue behavior without considering long-running process and file-lock behavior on Windows.

## CWAA Editing Rules

- Preserve the existing stack: PyQt desktop UI, Flask blueprints, Waitress, nginx, SQLAlchemy/SQLite, Bootstrap, jQuery, and Bootstrap Icons.
- Keep source and PyInstaller paths compatible. When changing asset/template/resource loading, check both normal filesystem mode and `_MEIPASS` packaging assumptions.
- Keep `backend.views` responsible for page rendering and upload flow, and `backend.api` responsible for JSON/file endpoints unless a broader refactor is explicitly requested.
- For database changes, prefer additive, idempotent patches in `backend/db.py` that tolerate existing SQLite files. Do not create destructive migrations.
- Do not change `backend.utils.version` except for a specific release/version task.
- For audio operations, use existing helpers such as `_resolve_tool`, `_run_ffmpeg`, `_run_ffprobe`, `_archive_mp3_encode_args`, `FFMPEG_SEMAPHORE`, and `TEMP_MP3_FOLDER` where applicable.
- For file serving and downloads, keep path normalization through `backend.path_resolver` or `_relative_path_inside`; never concatenate untrusted paths into a served file path without containment checks.
- For public/closed archives, respect `config['public_audio_path']`, `config['closed_audio_path']`, relative path storage, and `create_year_subfolders`.
- For recognition, preserve `recognize_text_enabled`, `recognize_text_default`, `recognize_text_from_audio_path`, `GigaAM_ASR\GigaAM_ASR.exe`, replacement tags, and queue semantics unless the task changes them.
- For frontend changes, stay consistent with Bootstrap/jQuery patterns in `frontend/index.html` and `frontend/assets/archive_viewer.js`; keep UI dense and operational.

## Dirty Worktree Handling

Use `git status --short` to understand the worktree. If a file has unrelated user changes, do not overwrite or revert them. If you must edit the same file, read the relevant hunks first and patch around them.

If the same file changed during your work and the source is unclear, re-read it before patching again.

## Data And Filesystem Safety

- Treat `audio_archive.db` and its WAL/SHM files as live local data.
- Prefer backup helpers already present in `backend/db.py`, `backend/path_resolver.py`, and `backend/duplicate_resolver.py` for operations that alter paths or delete duplicate DB rows.
- Be cautious around UNC paths such as `\\SRSFEMIDA\...`; they may be unavailable, slow, or permission-dependent in tests.
- Do not assume `ffmpeg.exe`, `ffprobe.exe`, nginx, or `GigaAM_ASR.exe` are available on PATH; this project carries local binaries and helper resolution.
- Do not start or stop production-like services unless the task requires it. If started, make sure no needed background session is left running at handoff.

## MCP Guardrail

MCP results are hints for navigation and impact analysis. They are not authority for current config, database contents, filesystem availability, service state, or business rules. If MCP and source disagree, trust the source, refresh the index, and re-run the query.
