"""
Backup and restore Cursor IDE database files.

Creates compressed backups of Cursor IDE data, storing them in a safe location
separate from Cursor's data directory. Supports listing backups and restoring
from a specific backup.

Backup format: .tar.xz
"""

import io
import json
import lzma
import os
import shutil
import sqlite3
import stat as stat_module
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .backup_formatters import (
    _format_size,
    format_backup_list,
    format_backup_summary,
    format_restore_summary,
)
from .utils import (
    CURSOR_PROJECTS_DIR_ENV,
    CURSOR_USER_DIR_ENV,
    get_cursor_paths,
    get_cursor_projects_dir,
    parse_workspace_storage_meta,
)

# Default backup directory
DEFAULT_BACKUP_DIR = Path.home() / ".cursor-chronicle" / "backups"

# Backup filename pattern
BACKUP_PREFIX = "cursor_backup_"
BACKUP_SUFFIX = ".tar.xz"
BACKUP_TYPE_MANUAL = "manual"
BACKUP_TYPE_PRE_RESTORE = "pre_restore"

# Metadata filename inside the archive
BACKUP_META_FILE = "backup_meta.json"

# LZMA compression preset (0-9, 9 = maximum compression)
LZMA_PRESET = 3

# Emit progress callback at most once per this many bytes while compressing.
PROGRESS_UPDATE_INTERVAL_BYTES = 2 * 1024 * 1024

SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")
WORKSPACE_BACKUP_FILENAMES = ("state.vscdb", "workspace.json")

FileSnapshot = Tuple[Path, str, int, float]

# Re-export formatting functions for backward compatibility
__all__ = [
    "create_backup",
    "list_backups",
    "latest_restorable_backup",
    "restore_backup",
    "get_backup_dir",
    "format_backup_summary",
    "format_backup_list",
    "format_restore_summary",
    "_format_size",
]


def get_backup_dir(config: Optional[Dict] = None) -> Path:
    """Get the backup directory path from config or default."""
    if config and "backup_path" in config:
        return Path(config["backup_path"])
    return DEFAULT_BACKUP_DIR


def _collect_cursor_files() -> Tuple[Path, List[Path]]:
    """Collect Cursor data files required to preserve chat history."""
    cursor_config_path, workspace_storage_path, global_storage_path = get_cursor_paths()
    cursor_root = cursor_config_path.parent
    roots = [cursor_root] if cursor_root.exists() else []

    projects_root = get_cursor_projects_dir()
    if projects_root.exists() and _should_collect_cursor_projects(
        cursor_config_path,
        projects_root,
    ):
        roots.append(projects_root)

    if not roots:
        return cursor_root, []

    if len(roots) == 1:
        base_path = roots[0]
    else:
        base_path = Path(os.path.commonpath([str(root) for root in roots]))

    files_to_backup = []
    _append_sqlite_file(files_to_backup, global_storage_path)
    _append_required_file(files_to_backup, global_storage_path.parent / "storage.json")

    _append_workspace_storage_files(files_to_backup, workspace_storage_path)

    if projects_root in roots:
        _append_agent_transcripts(files_to_backup, projects_root)

    files_to_backup.sort(key=lambda p: str(p))
    return base_path, files_to_backup


def _append_workspace_storage_files(
    files_to_backup: List[Path],
    workspace_storage_path: Path,
) -> None:
    if not workspace_storage_path.exists():
        return

    try:
        workspace_dirs = list(workspace_storage_path.iterdir())
    except OSError:
        return

    for workspace_dir in workspace_dirs:
        try:
            if not workspace_dir.is_dir() or workspace_dir.is_symlink():
                continue
        except OSError:
            continue
        for filename in WORKSPACE_BACKUP_FILENAMES:
            path = workspace_dir / filename
            if path.name == "state.vscdb":
                _append_sqlite_file(files_to_backup, path)
            else:
                _append_required_file(files_to_backup, path)


def _append_agent_transcripts(files_to_backup: List[Path], projects_root: Path) -> None:
    try:
        transcripts = projects_root.glob("*/agent-transcripts/*/*.jsonl")
        for transcript in transcripts:
            _append_required_file(files_to_backup, transcript)
    except OSError:
        return


def _append_sqlite_file(files_to_backup: List[Path], db_path: Path) -> None:
    _append_required_file(files_to_backup, db_path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        _append_required_file(
            files_to_backup,
            db_path.with_name(f"{db_path.name}{suffix}"),
        )


def _append_required_file(files_to_backup: List[Path], file_path: Path) -> None:
    try:
        if file_path.is_file() and not file_path.is_symlink():
            files_to_backup.append(file_path)
    except OSError:
        return


def _should_collect_cursor_projects(cursor_config_path: Path, projects_root: Path) -> bool:
    """Include ~/.cursor/projects only when resolving real Cursor data."""
    if os.environ.get(CURSOR_PROJECTS_DIR_ENV):
        return True

    if os.environ.get(CURSOR_USER_DIR_ENV):
        return False

    return cursor_config_path == _default_cursor_user_dir_for_home(
        projects_root.parent.parent,
    )


def _default_cursor_user_dir_for_home(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User"
    return home / ".config" / "Cursor" / "User"


def _build_backup_metadata(files: List[Path], base_path: Path) -> Dict:
    """Build metadata dict for the backup archive."""
    return _build_backup_metadata_from_snapshots(
        _snapshot_backup_files(files, base_path),
        base_path,
    )


def _snapshot_backup_files(files: List[Path], base_path: Path) -> List[FileSnapshot]:
    """Capture file metadata while ignoring files that disappear mid-backup."""
    snapshots = []
    for file_path in files:
        try:
            if file_path.is_symlink():
                continue
            file_stat = file_path.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(file_stat.st_mode):
            continue
        snapshots.append((
            file_path,
            _archive_path(file_path, base_path),
            file_stat.st_size,
            file_stat.st_mtime,
        ))
    return snapshots


def _build_backup_metadata_from_snapshots(
    snapshots: List[FileSnapshot],
    base_path: Path,
    backup_type: str = BACKUP_TYPE_MANUAL,
) -> Dict:
    """Build metadata dict for already-snapshotted files."""
    file_entries = []
    total_size = 0

    for _, rel_path, size, modified in snapshots:
        total_size += size
        file_entries.append({
            "path": rel_path,
            "size": size,
            "modified": datetime.fromtimestamp(modified).isoformat(),
        })

    return {
        "created_at": datetime.now().isoformat(),
        "backup_type": backup_type,
        "cursor_base_path": str(base_path),
        "total_files": len(snapshots),
        "total_size_bytes": total_size,
        "files": file_entries,
    }


def _archive_path(file_path: Path, base_path: Path) -> str:
    try:
        return str(file_path.relative_to(base_path))
    except ValueError:
        return str(file_path)


def _cleanup_partial_backups(backup_dir: Path) -> None:
    """Remove stale partial backup files left from interrupted runs."""
    for partial in backup_dir.glob(f".{BACKUP_PREFIX}*{BACKUP_SUFFIX}.partial"):
        try:
            partial.unlink()
        except OSError:
            # Best-effort cleanup: stale files should never block listing/backup.
            continue


def create_backup(
    backup_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[Dict], None]] = None,
    backup_type: str = BACKUP_TYPE_MANUAL,
) -> Dict:
    """Create a compressed backup of all Cursor files."""
    if backup_dir is None:
        backup_dir = DEFAULT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_partial_backups(backup_dir)

    base_path, files = _collect_cursor_files()
    snapshots = _snapshot_backup_files(files, base_path)
    if not snapshots:
        return {
            "backup_path": None, "total_files": 0, "total_size": 0,
            "compressed_size": 0, "compression_ratio": 0.0,
            "created_at": datetime.now().isoformat(),
            "error": "No Cursor files found to backup.",
        }

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = backup_dir / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"
    tmp_backup_path = backup_dir / f".{backup_path.name}.partial"
    if tmp_backup_path.exists():
        tmp_backup_path.unlink()

    metadata = _build_backup_metadata_from_snapshots(
        snapshots,
        base_path,
        backup_type=backup_type,
    )
    total_size = metadata["total_size_bytes"]
    total_files = len(snapshots)

    bytes_processed = 0
    last_reported_bytes = 0

    def _report_progress(current: int, file_path: str, force: bool = False) -> None:
        nonlocal last_reported_bytes
        if not progress_callback:
            return
        if total_size > 0:
            percent = int(min(bytes_processed, total_size) * 100 / total_size)
        else:
            percent = 100
        if not force and (bytes_processed - last_reported_bytes) < PROGRESS_UPDATE_INTERVAL_BYTES:
            return
        last_reported_bytes = bytes_processed
        progress_callback({
            "current": current,
            "total": total_files,
            "file_path": file_path,
            "percent": percent,
            "bytes_processed": bytes_processed,
            "bytes_total": total_size,
            "phase": "compressing",
        })

    class _ProgressReader:
        def __init__(self, source_file, on_read):
            self._source_file = source_file
            self._on_read = on_read

        def read(self, size: int = -1):
            chunk = self._source_file.read(size)
            if chunk:
                self._on_read(len(chunk))
            return chunk

    try:
        with tarfile.open(str(tmp_backup_path), "w:xz", preset=LZMA_PRESET) as tar:
            meta_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
            meta_info = tarfile.TarInfo(name=BACKUP_META_FILE)
            meta_info.size = len(meta_bytes)
            tar.addfile(meta_info, io.BytesIO(meta_bytes))

            for idx, (file_path, rel_path, file_size, _) in enumerate(snapshots, 1):
                def _on_read(chunk_len: int) -> None:
                    nonlocal bytes_processed
                    bytes_processed += chunk_len
                    _report_progress(current=idx, file_path=rel_path, force=False)

                try:
                    with file_path.open("rb") as src_file:
                        tar_info = tar.gettarinfo(
                            name=str(file_path),
                            arcname=rel_path,
                            fileobj=src_file,
                        )
                        src_file.seek(0)
                        tar.addfile(
                            tarinfo=tar_info,
                            fileobj=_ProgressReader(src_file, _on_read),
                        )
                except FileNotFoundError:
                    bytes_processed += file_size

                # Force a callback at file boundary for many small files.
                _report_progress(current=idx, file_path=rel_path, force=True)

        tmp_backup_path.replace(backup_path)
    except BaseException:
        if tmp_backup_path.exists():
            tmp_backup_path.unlink()
        raise

    compressed_size = backup_path.stat().st_size
    ratio = ((1 - compressed_size / total_size) * 100) if total_size > 0 else 0.0

    return {
        "backup_path": str(backup_path), "total_files": total_files,
        "total_size": total_size, "compressed_size": compressed_size,
        "compression_ratio": round(ratio, 1), "created_at": metadata["created_at"],
    }


def list_backups(backup_dir: Optional[Path] = None) -> List[Dict]:
    """List all available backups, sorted by date (newest first)."""
    if backup_dir is None:
        backup_dir = DEFAULT_BACKUP_DIR
    if not backup_dir.exists():
        return []
    _cleanup_partial_backups(backup_dir)

    backups = []
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.startswith(BACKUP_PREFIX) or not entry.name.endswith(BACKUP_SUFFIX):
            continue

        backup_info = {
            "filename": entry.name, "path": str(entry),
            "size": entry.stat().st_size, "created_at": None, "metadata": None,
        }

        name_part = entry.name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
        try:
            dt = datetime.strptime(name_part, "%Y-%m-%d_%H-%M-%S")
            backup_info["created_at"] = dt.isoformat()
        except ValueError:
            pass

        meta_data = _read_backup_metadata(entry)
        if meta_data:
            backup_info["metadata"] = meta_data
            backup_info["backup_type"] = meta_data.get("backup_type", BACKUP_TYPE_MANUAL)
            backup_info["is_pre_restore"] = (
                backup_info["backup_type"] == BACKUP_TYPE_PRE_RESTORE
            )
            if not backup_info["created_at"] and "created_at" in meta_data:
                backup_info["created_at"] = meta_data["created_at"]
        else:
            backup_info["backup_type"] = BACKUP_TYPE_MANUAL
            backup_info["is_pre_restore"] = False

        backups.append(backup_info)

    backups.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return backups


def latest_restorable_backup(backups: List[Dict]) -> Optional[Dict]:
    """Return the newest normal backup, skipping safety backups created by restore."""
    for backup in backups:
        if not backup.get("is_pre_restore"):
            return backup
    return None


def _read_backup_metadata(backup_path: Path) -> Optional[Dict]:
    """
    Read backup metadata efficiently from a tar.xz archive.

    `backup_meta.json` is written as the first archive member in create_backup().
    Reading the first member with tar.next() avoids a full archive scan that
    happens with random member lookup on compressed streams.
    """
    try:
        with tarfile.open(str(backup_path), "r:xz") as tar:
            first_member = tar.next()
            if not first_member:
                return None

            # Fast path: expected archive layout produced by create_backup().
            if first_member.name == BACKUP_META_FILE:
                meta_file = tar.extractfile(first_member)
                if not meta_file:
                    return None
                return json.loads(meta_file.read().decode("utf-8"))

            # Fallback for non-standard archives.
            for member in tar:
                if member.name == BACKUP_META_FILE:
                    meta_file = tar.extractfile(member)
                    if not meta_file:
                        return None
                    return json.loads(meta_file.read().decode("utf-8"))
    except (tarfile.TarError, lzma.LZMAError, OSError, json.JSONDecodeError):
        return None

    return None


def _validate_backup(backup_path: Path) -> Tuple[bool, str, Optional[Dict]]:
    """Validate a backup archive before restoration."""
    if not backup_path.exists():
        return False, f"Backup file not found: {backup_path}", None
    if not backup_path.is_file():
        return False, f"Not a file: {backup_path}", None

    try:
        with tarfile.open(str(backup_path), "r:xz") as tar:
            members = tar.getnames()
            if not members:
                return False, "Backup archive is empty.", None

            metadata = None
            if BACKUP_META_FILE in members:
                meta_file = tar.extractfile(BACKUP_META_FILE)
                if meta_file:
                    metadata = json.loads(meta_file.read().decode("utf-8"))

            has_db = any(m.endswith(".vscdb") for m in members)
            has_agent_transcripts = any(
                "/agent-transcripts/" in m and m.endswith(".jsonl")
                for m in members
            )
            if not has_db and not has_agent_transcripts:
                return (
                    False,
                    "Backup contains no database files (.vscdb) "
                    "or agent transcript files (.jsonl).",
                    metadata,
                )
            return True, "Backup is valid.", metadata

    except (tarfile.TarError, lzma.LZMAError) as e:
        return False, f"Invalid or corrupted backup archive: {e}", None
    except OSError as e:
        return False, f"Cannot read backup file: {e}", None


def _is_safe_tar_member(member_name: str) -> bool:
    member_path = Path(member_name)
    if member_path.is_absolute():
        return False
    return ".." not in member_path.parts


def _join_archive_suffix(base_path: Path, suffix: str) -> Path:
    return base_path.joinpath(*Path(suffix).parts)


def _resolve_restore_destination(
    member_name: str,
    target_base: Path,
    cursor_user_dir: Path,
    projects_dir: Path,
) -> Path:
    """Map archive paths from any supported backup layout to current Cursor paths."""
    normalized = member_name.replace("\\", "/")

    project_marker = ".cursor/projects/"
    if project_marker in normalized:
        suffix = normalized.split(project_marker, 1)[1]
        return _join_archive_suffix(projects_dir, suffix)

    user_markers = (
        "Library/Application Support/Cursor/User/",
        "AppData/Roaming/Cursor/User/",
        ".config/Cursor/User/",
    )
    for marker in user_markers:
        if marker in normalized:
            suffix = normalized.split(marker, 1)[1]
            return _join_archive_suffix(cursor_user_dir, suffix)

    if normalized.startswith("User/"):
        return _join_archive_suffix(cursor_user_dir, normalized[len("User/"):])

    if normalized.startswith(("globalStorage/", "workspaceStorage/")):
        return _join_archive_suffix(cursor_user_dir, normalized)

    return target_base / Path(member_name)


def _workspace_ids_by_folder(workspace_storage_path: Path) -> Dict[str, str]:
    """Return target-machine workspace ids keyed by folder path."""
    workspace_ids: Dict[str, Tuple[str, float]] = {}
    if not workspace_storage_path.exists():
        return {}

    try:
        workspace_dirs = list(workspace_storage_path.iterdir())
    except OSError:
        return {}

    for workspace_dir in workspace_dirs:
        workspace_json = workspace_dir / "workspace.json"
        if not workspace_json.is_file():
            continue
        try:
            workspace_data = json.loads(workspace_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        _, folder_path = parse_workspace_storage_meta(workspace_data)
        if not folder_path:
            continue

        try:
            modified = max(
                workspace_json.stat().st_mtime,
                (workspace_dir / "state.vscdb").stat().st_mtime,
            )
        except OSError:
            modified = 0

        existing = workspace_ids.get(folder_path)
        if not existing or modified >= existing[1]:
            workspace_ids[folder_path] = (workspace_dir.name, modified)

    return {
        folder_path: workspace_id
        for folder_path, (workspace_id, _) in workspace_ids.items()
    }


def _path_from_cursor_uri(uri) -> str:
    if isinstance(uri, dict):
        folder_path = uri.get("fsPath") or uri.get("path")
        if isinstance(folder_path, str) and folder_path:
            return folder_path
        external = uri.get("external")
        if isinstance(external, str) and external:
            return parse_workspace_storage_meta({"folder": external})[1]
    if isinstance(uri, str) and uri:
        return parse_workspace_storage_meta({"folder": uri})[1]
    return ""


def _rebind_workspace_identifier(workspace_identifier, workspace_ids: Dict[str, str]) -> bool:
    if not isinstance(workspace_identifier, dict):
        return False

    folder_path = _path_from_cursor_uri(workspace_identifier.get("uri"))
    target_workspace_id = workspace_ids.get(folder_path)
    if not target_workspace_id or workspace_identifier.get("id") == target_workspace_id:
        return False

    workspace_identifier["id"] = target_workspace_id
    return True


def _rebind_agent_history_workspace_ids(
    global_storage_path: Path,
    workspace_ids: Dict[str, str],
) -> None:
    """Point restored agent history at the target machine's workspace ids."""
    if not workspace_ids or not global_storage_path.exists():
        return

    try:
        conn = sqlite3.connect(global_storage_path)
        cursor = conn.cursor()
    except sqlite3.Error:
        return

    try:
        _rebind_composer_headers(cursor, workspace_ids)
        _rebind_agent_projects(cursor, workspace_ids, "glass.localAgentProjects.v1")
        _rebind_agent_projects(cursor, workspace_ids, "glass.cloudAgentProjects.v1")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
    finally:
        conn.close()


def _rebind_composer_headers(cursor, workspace_ids: Dict[str, str]) -> None:
    cursor.execute(
        "SELECT value FROM ItemTable WHERE key = 'composer.composerHeaders'"
    )
    result = cursor.fetchone()
    if not result:
        return
    try:
        data = json.loads(result[0])
    except json.JSONDecodeError:
        return

    changed = False
    for composer in data.get("allComposers", []):
        if isinstance(composer, dict):
            changed = (
                _rebind_workspace_identifier(
                    composer.get("workspaceIdentifier"),
                    workspace_ids,
                )
                or changed
            )

    if changed:
        cursor.execute(
            "UPDATE ItemTable SET value = ? WHERE key = 'composer.composerHeaders'",
            (json.dumps(data, separators=(",", ":")),),
        )


def _rebind_agent_projects(cursor, workspace_ids: Dict[str, str], key: str) -> None:
    cursor.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
    result = cursor.fetchone()
    if not result:
        return
    try:
        projects = json.loads(result[0])
    except json.JSONDecodeError:
        return
    if not isinstance(projects, list):
        return

    changed = False
    for project in projects:
        if isinstance(project, dict):
            changed = (
                _rebind_workspace_identifier(project.get("workspace"), workspace_ids)
                or changed
            )

    if changed:
        cursor.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (json.dumps(projects, separators=(",", ":")), key),
        )


def _cleanup_stale_sqlite_sidecars(destinations: List[Path]) -> None:
    """Remove existing WAL/SHM sidecars when the backup does not restore them."""
    destination_set = {destination.resolve() for destination in destinations}
    for destination in destinations:
        if destination.name.endswith(".vscdb"):
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar = destination.with_name(f"{destination.name}{suffix}")
                if sidecar.resolve() in destination_set:
                    continue
                try:
                    sidecar.unlink()
                except OSError:
                    continue


def restore_backup(
    backup_path: Path,
    create_pre_restore_backup: bool = True,
    backup_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[Dict], None]] = None,
) -> Dict:
    """Restore Cursor database files from a backup archive."""
    result = {
        "restored_files": 0, "pre_restore_backup": None,
        "errors": [], "success": False,
    }

    is_valid, message, metadata = _validate_backup(backup_path)
    if not is_valid:
        result["errors"].append(message)
        return result

    cursor_config_path = get_cursor_paths()[0]
    target_base = Path(metadata["cursor_base_path"]) if metadata and "cursor_base_path" in metadata else cursor_config_path
    projects_dir = get_cursor_projects_dir()
    current_workspace_ids = _workspace_ids_by_folder(
        cursor_config_path / "workspaceStorage"
    )

    try:
        with tarfile.open(str(backup_path), "r:xz") as tar:
            members = [
                m for m in tar.getmembers()
                if m.name != BACKUP_META_FILE and _is_safe_tar_member(m.name)
            ]
    except (tarfile.TarError, lzma.LZMAError, OSError) as e:
        result["errors"].append(f"Error reading backup members: {e}")
        return result

    destinations = [
        _resolve_restore_destination(
            member.name,
            target_base,
            cursor_config_path,
            projects_dir,
        )
        for member in members
        if member.isfile()
    ]
    _cleanup_stale_sqlite_sidecars(destinations)

    if create_pre_restore_backup:
        try:
            pre_backup = create_backup(
                backup_dir=backup_dir,
                backup_type=BACKUP_TYPE_PRE_RESTORE,
            )
            if pre_backup.get("backup_path"):
                result["pre_restore_backup"] = pre_backup["backup_path"]
        except Exception as e:
            result["errors"].append(f"Warning: Could not create pre-restore backup: {e}")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with tarfile.open(str(backup_path), "r:xz") as tar:
                total = len(members)

                for idx, member in enumerate(members, 1):
                    if not member.isfile():
                        continue
                    src_file = tar.extractfile(member)
                    if not src_file:
                        continue
                    dst = _resolve_restore_destination(
                        member.name,
                        target_base,
                        cursor_config_path,
                        projects_dir,
                    )
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        with dst.open("wb") as output_file:
                            shutil.copyfileobj(src_file, output_file)
                        os.utime(dst, (member.mtime, member.mtime))
                        result["restored_files"] += 1
                    except OSError as e:
                        result["errors"].append(f"Failed to restore {member.name}: {e}")

                    if progress_callback:
                        percent = int(idx * 100 / total) if total > 0 else 0
                        progress_callback({
                            "current": idx, "total": total,
                            "file_path": member.name, "percent": percent,
                        })

        except (tarfile.TarError, lzma.LZMAError, OSError) as e:
            result["errors"].append(f"Error during extraction: {e}")
            return result

    _rebind_agent_history_workspace_ids(
        cursor_config_path / "globalStorage" / "state.vscdb",
        current_workspace_ids,
    )

    result["success"] = result["restored_files"] > 0 and not result["errors"]
    return result
