"""Stable local project identities and readable, collision-resistant channels."""

import hashlib
from pathlib import Path
import re
import subprocess

CHANNEL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def project_context(cwd):
    cwd = Path(cwd).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("Agent working directory must be a directory")
    try:
        result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=5)
        root = Path(result.stdout.strip()).resolve() if result.returncode == 0 else cwd
    except (OSError, subprocess.TimeoutExpired):
        root = cwd
    key = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")[:35] or "project"
    return {"cwd": str(cwd), "project_root": str(root),
            "project_name": root.name or str(root), "project_channel": f"{slug}-{key}"}


def validate_channels(channels):
    if not isinstance(channels, list) or not 1 <= len(channels) <= 32:
        raise ValueError("Choose between 1 and 32 channels")
    if any(not isinstance(c, str) or not CHANNEL_NAME_RE.fullmatch(c) for c in channels):
        raise ValueError("Invalid channel name")
    return list(dict.fromkeys(channels))
