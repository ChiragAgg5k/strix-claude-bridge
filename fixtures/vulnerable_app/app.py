"""Intentionally vulnerable local fixture for credential-free bridge validation."""

from pathlib import Path

DATA_ROOT = Path(__file__).parent / "data"


def read_file(user_path: str) -> str:
    # STRIX_DRY_RUN_PATH_TRAVERSAL: deliberately missing containment validation.
    return (DATA_ROOT / user_path).read_text()
