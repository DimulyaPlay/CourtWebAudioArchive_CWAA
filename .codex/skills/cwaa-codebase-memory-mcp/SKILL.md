---
name: cwaa-codebase-memory-mcp
description: Use the connected codebase-memory-mcp knowledge graph for CWAA repository orientation, impact analysis, code search, architecture review, change detection, and index refresh after substantial code changes.
---

# CWAA Codebase Memory MCP

Use this skill whenever a Court Web Audio Archive (CWAA) task needs repository orientation, impact analysis, cross-file dependency checks, or a post-change graph refresh. The MCP index is a navigation layer; always confirm findings in the local source before editing.

## Project Identity

- Product: `CourtWebAudioArchive(CWAA)`, a Windows desktop-controlled Flask/Waitress/nginx application for archiving and browsing court audio protocols.
- Repository root: `C:\Users\CourtUser\PycharmProjects\CourtWebAudioArchive(CWAA)`.
- Expected MCP project name after indexing: `C-Users-CourtUser-PycharmProjects-CourtWebAudioArchive-CWAA`.
- Installed server: `C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe`.
- UI, when running: `http://127.0.0.1:9749`.

## Connection Rule

Do not rely on PyCharm/JetBrains MCP discovery for this server if it is slow or unavailable. Use the installed executable directly from the CWAA repository root:

```powershell
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli <tool> '<json>'
```

If a future Codex session exposes `codebase-memory-mcp` as a native MCP tool namespace, it may be used, but do not wait for discovery before using the executable.

## Preferred CLI Tool Flow

1. Confirm or create the index:
   - `cli list_projects '{}'` to check whether CWAA is already indexed;
   - if missing, run `cli index_repository '{"repo_path":"C:\\Users\\CourtUser\\PycharmProjects\\CourtWebAudioArchive(CWAA)","mode":"fast"}'`;
   - then run `cli index_status '{"project":"C-Users-CourtUser-PycharmProjects-CourtWebAudioArchive-CWAA"}'`.
2. Orient before editing:
   - `cli get_architecture ...` for unfamiliar areas or broad tasks;
   - `cli search_graph ...` for functions, classes, routes, modules, and Russian UI text;
   - `cli search_code ...` for text patterns with graph-enriched results;
   - `cli get_code_snippet ...` after `search_graph` gives an exact `qualified_name`;
   - `cli trace_path ...` for callers/callees, impact, and dependency direction;
   - `cli query_graph ...` for complex multi-hop or aggregate checks.
3. Confirm with local files:
   - use `rg` / `rg --files` and open relevant files before editing;
   - do not rely only on graph summaries when changing behavior, routes, paths, database access, process startup, or file operations.
4. After edits:
   - run normal project verification first;
   - use `detect_changes` to review changed files and impacted symbols;
   - refresh the index with `index_repository` after substantial changes.

## CWAA Orientation Map

- `CWAA Server.py`: PyQt desktop shell, config UI, nginx config generation, Waitress lifecycle, health monitoring, background thread startup and shutdown.
- `backend/__init__.py`: Flask app factory, static/template paths for source and PyInstaller `_MEIPASS`, blueprint registration, `/healthz`.
- `backend/views.py`: upload/archive pages, MP3 upload validation, metadata writing with `music_tag`, save-to-public/closed archive behavior.
- `backend/api.py`: archive search, audio serving through `X-Accel-Redirect`, downloads, Femida WAV conversion, temporary uploads, audio edit rendering, transcript export and replacement-rule APIs.
- `backend/db.py` and `backend/models.py`: SQLite/SQLAlchemy setup, WAL pragmas, backup snapshot helpers, schema/index patching, `AudioRecord` and `DownloadLog`.
- `backend/path_resolver.py`: relative/absolute path conversion for public and closed storage roots; migration backup and path migration.
- `backend/duplicate_resolver.py`: duplicate conflict discovery and merge/delete with DB backup.
- `backend/recognition_orchestrator.py`: GigaAM ASR integration, transcript copy-back, phrase replacement tagging, queue polling.
- `backend/backup_service.py`: backup settings window and scheduled archive/database backups.
- `frontend/index.html`: upload/import/edit page, Bootstrap/jQuery UI, temp audio editor and Femida import controls.
- `frontend/archive_viewer.html` and `frontend/assets/archive_viewer.js`: archive search, playback, transcript viewer, replacement-rule actions, downloads, retranscription.
- Root config/data files: `config.txt`, `backup_config.txt`, `courtrooms.txt`, `import_sources.txt`, `audio_archive.db`, `assets/phraseReplacement.txt`.

## CLI Commands

```powershell
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli list_projects '{}'
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli index_repository '{"repo_path":"C:\\Users\\CourtUser\\PycharmProjects\\CourtWebAudioArchive(CWAA)","mode":"fast"}'
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli index_status '{"project":"C-Users-CourtUser-PycharmProjects-CourtWebAudioArchive-CWAA"}'
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli search_graph '{"project":"C-Users-CourtUser-PycharmProjects-CourtWebAudioArchive-CWAA","query":"render_edit","limit":10}'
```

The CLI accepts JSON via raw argument, `--args-file`, or stdin. Prefer `--args-file` or stdin if quoting a path containing parentheses becomes brittle in PowerShell.

## When To Refresh The Index

Run `index_repository` for the CWAA root after:

- adding, deleting, renaming, or moving source files;
- changing public function/class/module boundaries;
- changing Flask routes, frontend endpoint callers, shared JSON response shapes, SQLAlchemy models, path resolution, backup behavior, ffmpeg wrappers, GigaAM ASR orchestration, nginx/Waitress startup, or PyInstaller packaging;
- a large refactor or multi-file change;
- generated agents or another tool changed code outside the current turn.

For small documentation edits, index refresh is optional. For a few-line change inside an existing function, refresh only when later work would benefit from a current graph.

## Safety Rules

- Never run `delete_project` against the CWAA project unless the user explicitly asks to remove the index.
- Do not let `index_repository` replace verification; it confirms graph ingestion, not behavior.
- Do not use MCP output to invent filesystem, SQLite, nginx, ASR, or ffmpeg facts. Verify against source, config, and local files.
- Do not use MCP to bypass dirty-worktree handling or data-safety rules for `audio_archive.db`, archive storage roots, temporary audio files, backups, or user config.
- If graph output conflicts with source files, trust source files and refresh/recheck the index.

## Useful Checks

Status:

```powershell
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli index_status '{"project":"C-Users-CourtUser-PycharmProjects-CourtWebAudioArchive-CWAA"}'
```

Fast refresh:

```powershell
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli index_repository '{"repo_path":"C:\\Users\\CourtUser\\PycharmProjects\\CourtWebAudioArchive(CWAA)","mode":"fast"}'
```

Change impact:

```powershell
& 'C:\Users\CourtUser\AppData\Local\Programs\codebase-memory-mcp\codebase-memory-mcp.exe' cli detect_changes '{"project":"C-Users-CourtUser-PycharmProjects-CourtWebAudioArchive-CWAA"}'
```
