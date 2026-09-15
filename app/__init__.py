from pathlib import Path

def read_version() -> str:
    path = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "v0.0.1"
    return text or "v0.0.1"

__version__ = read_version()
