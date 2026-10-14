"""Offline display/search labels. Names are not legality or availability claims."""

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import hashlib
import json
import unicodedata

from .i18n import get_language, tr
from .pa9_creation import SOURCE_COMMIT, species_catalog


@dataclass(frozen=True)
class Choice:
    value: int
    label: str
    search: str
    number: int
    form: int | None = None


def normalize_query(value):
    text = unicodedata.normalize(
        "NFKD", unicodedata.normalize("NFKC", value).casefold()
    )
    letters = []
    for c in text:
        if (
            unicodedata.combining(c)
            and letters
            and unicodedata.name(letters[-1], "").startswith("LATIN")
        ):
            continue
        letters.append(c)
    text = "".join(letters)
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


@lru_cache(maxsize=1)
def _names():
    package = files("zasim").joinpath("data")
    raw = package.joinpath("ui_names.json").read_bytes()
    provenance = json.loads(
        package.joinpath("ui_names_provenance.json").read_text(encoding="utf-8")
    )
    if hashlib.sha256(raw).hexdigest() != provenance["data_sha256"]:
        raise ValueError("bundled display names failed their integrity check")
    data = json.loads(raw)
    if data["schema"] != 1 or data["source_commit"] != SOURCE_COMMIT:
        raise ValueError("unsupported display-name revision")
    return data


def choices(kind, language=None):
    return _choices(kind, language or get_language())


@lru_cache(maxsize=8)
def _choices(kind, language):
    if language not in ("ja", "en"):
        raise ValueError("unsupported display language")
    if kind == "species":
        return tuple(
            Choice(
                p["internal_id"],
                p["names"]["1" if language == "ja" else "2"],
                normalize_query(" ".join(p["names"].values()) + f" {p['species']}"),
                p["species"],
                p["form"],
            )
            for p in species_catalog()
        )
    if kind not in ("moves", "abilities", "items"):
        raise ValueError("unknown display catalog")
    data = _names()[kind]
    return tuple(
        Choice(
            index,
            ("なし" if language == "ja" else "None") if index == 0 else name,
            normalize_query(
                ("なし none " if index == 0 else "")
                + data["ja"][index]
                + " "
                + data["en"][index]
            ),
            index,
        )
        for index, name in enumerate(data[language])
        if name.strip()
    )


def search(kind, query, language=None):
    query = normalize_query(query.strip())
    rows = choices(kind, language or get_language())
    if not query:
        return rows
    if query.isascii() and query.isdecimal():
        return tuple(row for row in rows if row.number == int(query))
    words = query.split()
    return tuple(row for row in rows if all(word in row.search for word in words))


@lru_cache(maxsize=8)
def _labels(kind, language):
    return {row.value: row.label for row in choices(kind, language)}


def display_value(kind, value):
    try:
        label = _labels(kind, get_language()).get(int(value))
    except (OSError, ValueError, KeyError):
        return str(value)  # A missing display table must not rewrite record data.
    return f"{value}: {label}" if label else str(value)


def field_catalog(name):
    if name == "species_internal":
        return "species"
    if name == "held_item":
        return "items"
    if name == "ability":
        return "abilities"
    if name in {f"move{i}" for i in range(1, 5)} | {
        f"relearn_move{i}" for i in range(1, 5)
    }:
        return "moves"
    return None


def display_input_id(kind, text):
    """Accept a plain numeric input or an unchanged bilingual display label."""
    if ":" not in str(text):
        return text
    number, label = str(text).split(":", 1)
    value = int(number, 16 if number.strip().lower().startswith("0x") else 10)
    expected = {_labels(kind, lang).get(value) for lang in ("ja", "en")}
    if label.strip() not in expected:
        raise ValueError(
            tr(
                "名前と番号が一致しません。「選ぶ」を使うか、番号だけを入力してください。"
            )
        )
    return number
