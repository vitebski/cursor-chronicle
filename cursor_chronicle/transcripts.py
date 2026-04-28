"""
Read newer Cursor agent transcript files stored under ~/.cursor/projects.
"""

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .utils import cursor_project_slug_for_path, parse_workspace_storage_meta

TRANSCRIPT_COMPOSER_PREFIX = "transcript:"


def make_transcript_composer_id(path: Path) -> str:
    """Build a stable composer id for transcript-backed dialogs."""
    return f"{TRANSCRIPT_COMPOSER_PREFIX}{path}"


def is_transcript_composer_id(composer_id: str) -> bool:
    """Return True when a composer id points at a JSONL agent transcript."""
    return composer_id.startswith(TRANSCRIPT_COMPOSER_PREFIX)


def transcript_path_from_composer_id(composer_id: str) -> Path:
    """Extract the transcript path from a transcript-backed composer id."""
    return Path(composer_id[len(TRANSCRIPT_COMPOSER_PREFIX):])


def iter_agent_transcripts(projects_dir: Path) -> List[Path]:
    """Return all agent transcript JSONL files in Cursor's project data root."""
    if not projects_dir.exists():
        return []
    transcripts = [
        path
        for path in projects_dir.glob("*/agent-transcripts/*/*.jsonl")
        if path.is_file()
    ]
    transcripts.sort(key=lambda path: str(path))
    return transcripts


def load_project_path_map(cursor_user_dir: Path) -> Dict[str, Tuple[str, str]]:
    """
    Map Cursor project slugs to display names and absolute folder paths.

    Cursor stores transcript folders as path-like slugs, for example
    /Users/me/project -> Users-me-project. The old app support data still keeps
    the original URIs, so we use it to recover readable project names.
    """
    project_map: Dict[str, Tuple[str, str]] = {}

    for uri in _iter_storage_json_project_uris(cursor_user_dir):
        _add_uri_to_project_map(project_map, uri)

    workspace_storage = cursor_user_dir / "workspaceStorage"
    if workspace_storage.exists():
        for workspace_json in workspace_storage.glob("*/workspace.json"):
            try:
                workspace_data = json.loads(workspace_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            project_name, folder_path = parse_workspace_storage_meta(workspace_data)
            if folder_path:
                project_map[cursor_project_slug_for_path(folder_path)] = (
                    project_name,
                    folder_path,
                )

    return project_map


def parse_transcript_summary(transcript_path: Path) -> Dict:
    """Build composer metadata for a JSONL transcript."""
    stat = transcript_path.stat()
    created_at = int(getattr(stat, "st_birthtime", stat.st_ctime) * 1000)
    last_updated = int(stat.st_mtime * 1000)
    first_user_text = _first_user_text(transcript_path)

    return {
        "composerId": make_transcript_composer_id(transcript_path),
        "name": _title_from_text(first_user_text) or transcript_path.stem,
        "lastUpdatedAt": last_updated,
        "createdAt": created_at,
        "transcriptPath": str(transcript_path),
    }


def get_transcript_messages(composer_id: str) -> List[Dict]:
    """Read messages from a transcript-backed composer id."""
    transcript_path = transcript_path_from_composer_id(composer_id)
    if not transcript_path.exists():
        return []

    messages: List[Dict] = []
    try:
        with transcript_path.open("r", encoding="utf-8") as transcript_file:
            for rowid, line in enumerate(transcript_file, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                messages.extend(_messages_from_entry(entry, transcript_path, rowid))
    except OSError:
        return []

    return messages


def _iter_storage_json_project_uris(cursor_user_dir: Path) -> Iterable[str]:
    storage_json = cursor_user_dir / "globalStorage" / "storage.json"
    if not storage_json.exists():
        return []

    try:
        storage_data = json.loads(storage_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    uris: List[str] = []
    backup_workspaces = storage_data.get("backupWorkspaces", {})
    if isinstance(backup_workspaces, dict):
        for key in ("folders", "workspaces"):
            entries = backup_workspaces.get(key, [])
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _append_uri_from_entry(uris, entry)

    profile_associations = storage_data.get("profileAssociations", {})
    if isinstance(profile_associations, dict):
        workspaces = profile_associations.get("workspaces", {})
        if isinstance(workspaces, dict):
            uris.extend(str(uri) for uri in workspaces.keys())

    return uris


def _append_uri_from_entry(uris: List[str], entry: Dict) -> None:
    for key in ("folderUri", "workspaceUri", "folder", "workspace"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            uris.append(value)


def _add_uri_to_project_map(
    project_map: Dict[str, Tuple[str, str]],
    uri: str,
) -> None:
    project_name, folder_path = parse_workspace_storage_meta({"folder": uri})
    if not folder_path:
        return
    project_map[cursor_project_slug_for_path(folder_path)] = (project_name, folder_path)


def _first_user_text(transcript_path: Path) -> str:
    try:
        with transcript_path.open("r", encoding="utf-8") as transcript_file:
            for line in transcript_file:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("role") != "user":
                    continue
                text = _extract_text(entry.get("message", {}).get("content"))
                if text.strip():
                    return text
    except OSError:
        return ""
    return ""


def _messages_from_entry(entry: Dict, transcript_path: Path, rowid: int) -> List[Dict]:
    role = entry.get("role")
    message_type = 1 if role == "user" else 2 if role == "assistant" else 0
    content = entry.get("message", {}).get("content")
    text = _extract_text(content).strip()
    messages: List[Dict] = []

    if text:
        messages.append(
            _message_dict(
                text=text,
                message_type=message_type,
                transcript_path=transcript_path,
                rowid=rowid,
            )
        )

    for tool_data in _extract_tool_calls(content):
        messages.append(
            _message_dict(
                text="",
                message_type=2,
                transcript_path=transcript_path,
                rowid=rowid,
                tool_data=tool_data,
            )
        )

    return messages


def _message_dict(
    text: str,
    message_type: int,
    transcript_path: Path,
    rowid: int,
    tool_data: Optional[Dict] = None,
) -> Dict:
    return {
        "text": text,
        "type": message_type,
        "bubble_id": f"{transcript_path.stem}:{rowid}",
        "key": f"transcript:{transcript_path}:{rowid}",
        "rowid": rowid,
        "tool_data": tool_data,
        "attached_files": [],
        "is_thought": False,
        "thinking_duration": 0,
        "thinking_content": "",
        "token_count": {},
        "usage_uuid": None,
        "server_bubble_id": None,
        "is_agentic": True,
        "capabilities_ran": {},
        "unified_mode": None,
        "use_web": False,
        "is_refunded": False,
    }


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item.get("type") == "tool_result" and isinstance(item.get("content"), str):
            parts.append(item["content"])
    return "\n".join(parts)


def _extract_tool_calls(content) -> List[Dict]:
    if not isinstance(content, list):
        return []

    tool_calls: List[Dict] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        tool_calls.append({
            "tool": None,
            "name": item.get("name", "unknown"),
            "status": "unknown",
            "rawArgs": item.get("input", {}),
            "result": None,
        })
    return tool_calls


def _title_from_text(text: str) -> str:
    user_query_match = re.search(
        r"<user_query>\s*(.*?)\s*</user_query>",
        text,
        flags=re.DOTALL,
    )
    if user_query_match:
        text = user_query_match.group(1)

    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if len(text) > 80:
        return text[:77].rstrip() + "..."
    return text
