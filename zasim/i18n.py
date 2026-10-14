"""Small, toolkit-independent Japanese/English presentation layer.

Japanese source messages are stable identifiers. Only display text is translated;
protocol fields, file formats, user input and JSON field names stay unchanged.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
import locale
import os
from pathlib import Path
import re
from string import Formatter
import tempfile

from .file_save import read_regular_bytes
from .json_limits import validate_json_depth

LANGUAGES = ("ja", "en")
LANGUAGE_NAMES = {"ja": "日本語", "en": "English"}
_language = "ja"  # Library callers retain historical behavior; launchers select locale.


def validate_language(language: str) -> str:
    if language not in LANGUAGES:
        raise ValueError("language must be 'ja' or 'en'")
    return language


def get_language() -> str:
    return _language


def set_language(language: str) -> None:
    global _language
    _language = validate_language(language)


def _fields(text: str) -> list[tuple[str, str, str | None]]:
    result = []
    for _literal, name, spec, conversion in Formatter().parse(text):
        if name is not None:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                raise ValueError("translation placeholders must be simple names")
            result.append((name, spec, conversion))
    return sorted(result)


@lru_cache(maxsize=1)
def english_catalog() -> dict[str, str]:
    value = json.loads(files("zasim").joinpath("locales/en.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid translation catalog")
    for source, target in value.items():
        if not isinstance(source, str) or not isinstance(target, str) or not target:
            raise ValueError("empty or invalid translation")
        if _fields(source) != _fields(target):
            raise ValueError("translation placeholder mismatch")
    return value


def tr(message: str, *, language: str | None = None, **values: object) -> str:
    selected = get_language() if language is None else validate_language(language)
    text = english_catalog().get(message, message) if selected == "en" else message
    return text.format(**values) if values else text


def detect_language() -> str:
    """Explicit environment override, then OS locale; non-Japanese defaults to EN."""
    override = os.environ.get("ZA_TRADE_LANG")
    if override in LANGUAGES:
        return override
    hint = next((os.environ[k] for k in ("LC_ALL", "LC_MESSAGES", "LANG")
                 if os.environ.get(k)), "")
    if not hint or hint in ("C", "POSIX", "C.UTF-8"):
        try:
            hint = locale.getlocale()[0] or hint
        except (ValueError, TypeError):
            pass
    return "ja" if hint.lower().startswith("ja") else "en"


def preference_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData/Roaming"))
    else:
        configured = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "za-trade-lab" / "preferences.json"


def _read_preferences(path: Path) -> dict:
    if path.is_symlink():
        raise OSError("preference file must not be a symbolic link")
    if not path.exists():
        return {}
    raw = read_regular_bytes(path, max_size=65536)
    validate_json_depth(raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except RecursionError as exc:
        raise ValueError("preference JSON nesting is too deep") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid preference object")
    return data


def preferred_language() -> str:
    if os.environ.get("ZA_TRADE_LANG") in LANGUAGES:
        return os.environ["ZA_TRADE_LANG"]
    try:
        selected = _read_preferences(preference_path()).get("language")
        if selected in LANGUAGES:
            return selected
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    return detect_language()


def save_language(language: str, *, path: Path | None = None) -> None:
    """Save only language, preserving other settings; never store paths or keys."""
    validate_language(language)
    target = preference_path() if path is None else Path(path)
    # A malformed existing file is preserved, not silently overwritten.
    data = _read_preferences(target)
    data["language"] = language
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".language-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
