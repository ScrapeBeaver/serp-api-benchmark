"""Reading and writing records: one JSON object per line, optionally gzipped.

A records file is append-only while a run is in progress and never edited afterwards.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from pydantic import ValidationError

from serp_bench.models import SCHEMA_VERSION, VALID_OUTCOMES, Record


class RecordError(Exception):
    """A records file that does not match the schema, with the line that is wrong."""


def _open(path: Path, mode: str) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")  # type: ignore[return-value]
    return open(path, mode, encoding="utf-8")


class RecordWriter:
    """Writes records as they are produced, so an interrupted run keeps what it measured."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = _open(path, "w")
        self.count = 0

    def write(self, record: Record) -> None:
        self._file.write(record.model_dump_json() + "\n")
        self.count += 1

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> RecordWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_records(path: Path) -> Iterator[Record]:
    """Yield the records of a file, validating each one."""
    if not path.is_file():
        raise RecordError(f"{path}: not found")
    try:
        yield from _read(path)
    except (EOFError, OSError) as exc:
        # A gzip stream with no end: the run that writes the file is still going, or it
        # was killed. Either way the file is not a finished run and must not be kept.
        raise RecordError(
            f"{path}: the file is incomplete ({exc}); the run that writes it has not "
            "finished, or was interrupted"
        ) from exc


def _read(path: Path) -> Iterator[Record]:
    with _open(path, "r") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = Record.model_validate_json(line)
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(part) for part in first["loc"])
                raise RecordError(f"{path}:{number}: {where}: {first['msg']}") from exc
            if record.schema_version != SCHEMA_VERSION:
                raise RecordError(
                    f"{path}:{number}: schema_version {record.schema_version}, "
                    f"this version reads {SCHEMA_VERSION}"
                )
            if record.outcome not in VALID_OUTCOMES:
                raise RecordError(f"{path}:{number}: unknown outcome '{record.outcome}'")
            if (record.outcome == "void") != (record.void_reason is not None):
                raise RecordError(f"{path}:{number}: void_reason must be set exactly for void")
            yield record
