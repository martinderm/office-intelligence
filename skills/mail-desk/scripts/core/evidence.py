"""Evidence markdown updating and duplicate detection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import normalize_message_id


def update_evidence_file(
    evidence_spec: dict[str, Any],
    msg_id: str,
    workspace_root: Path | None = None,
) -> bool:
    """Update evidence markdown file with entry if not already present."""
    if not isinstance(evidence_spec, dict):
        return False

    rel_file = evidence_spec.get("file")
    entry_text = evidence_spec.get("entry")
    if not rel_file or not entry_text:
        return False

    ws_root = workspace_root or Path.cwd()
    target_md = (ws_root / rel_file).resolve() if not Path(rel_file).is_absolute() else Path(rel_file)
    target_md.parent.mkdir(parents=True, exist_ok=True)

    norm_id = normalize_message_id(msg_id)

    if target_md.exists():
        content = target_md.read_text(encoding="utf-8")
        if norm_id and norm_id in content.lower():
            # Already documented
            return True
        new_content = content.rstrip() + "\n\n" + entry_text.strip() + "\n"
    else:
        # Create minimal header if file is new
        title = target_md.stem
        header = f"# Evidence — {title}\n\n"
        new_content = header + entry_text.strip() + "\n"

    target_md.write_text(new_content, encoding="utf-8")
    return True
