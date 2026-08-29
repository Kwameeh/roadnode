from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .observations import parse_utc, utc_now

SCHEMA_VERSION = 1

PRIORITY_ROUTINE = "routine"
PRIORITY_CRITICAL = "critical"
VALID_PRIORITIES = {PRIORITY_ROUTINE, PRIORITY_CRITICAL}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    qos INTEGER NOT NULL,
    retain INTEGER NOT NULL,
    priority TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    byte_size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_drain_order
    ON outbox (captured_at);
CREATE INDEX IF NOT EXISTS outbox_priority_order
    ON outbox (priority, captured_at);
CREATE TABLE IF NOT EXISTS outbox_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class OutboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutboxItem:
    """One durable, unacknowledged message."""

    message_id: str
    topic: str
    payload: bytes
    qos: int
    retain: bool
    priority: str
    captured_at: str
    enqueued_at: str
    attempts: int
    last_attempt_at: str | None
    byte_size: int


@dataclass(frozen=True)
class OutboxStats:
    depth: int
    bytes_used: int
    oldest_captured_at: str | None
    newest_captured_at: str | None
    dropped_messages: int
    dropped_imu_samples: int
    first_missing_captured_at: str | None
    last_missing_captured_at: str | None


def serialize_frame(frame: dict[str, Any]) -> bytes:
    """Compact wire bytes; insignificant whitespace is removed before storage."""
    return json.dumps(frame, separators=(",", ":"), sort_keys=False).encode("utf-8")


class SqliteOutbox:
    """Append-only durable MQTT queue.

    A row is deleted only after PUBACK. Every write commits with
    ``synchronous=FULL`` so an unexpected power loss cannot acknowledge a
    message the broker never received.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._ensure_schema_version()

    def _ensure_schema_version(self) -> None:
        row = self._connection.execute(
            "SELECT value FROM outbox_meta WHERE key='schemaVersion'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO outbox_meta (key, value) VALUES ('schemaVersion', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        if int(row["value"]) != SCHEMA_VERSION:
            raise OutboxError(
                f"unsupported outbox schema version: {row['value']}"
            )

    def _counter(self, key: str) -> int:
        row = self._connection.execute(
            "SELECT value FROM outbox_meta WHERE key=?", (key,)
        ).fetchone()
        return int(row["value"]) if row else 0

    def _set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO outbox_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _bump_counter(self, key: str, amount: int) -> None:
        if amount:
            self._set_meta(key, str(self._counter(key) + amount))

    def put(
        self,
        *,
        message_id: str,
        topic: str,
        payload: bytes,
        captured_at: str,
        qos: int = 1,
        retain: bool = False,
        priority: str = PRIORITY_ROUTINE,
        enqueued_at: str | None = None,
    ) -> bool:
        """Append one message. Returns False if `message_id` is already queued."""
        if not message_id.strip():
            raise ValueError("message_id must not be empty")
        if not topic.strip():
            raise ValueError("topic must not be empty")
        if qos not in (0, 1, 2):
            raise ValueError(f"unsupported qos: {qos}")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"unsupported priority: {priority}")
        parse_utc(captured_at)
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes")

        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO outbox ("
                " message_id, topic, payload, qos, retain, priority,"
                " captured_at, enqueued_at, attempts, last_attempt_at, byte_size"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
                (
                    message_id,
                    topic,
                    bytes(payload),
                    int(qos),
                    1 if retain else 0,
                    priority,
                    captured_at,
                    enqueued_at or utc_now(),
                    len(payload),
                ),
            )
            return cursor.rowcount > 0

    def oldest(self) -> OutboxItem | None:
        """Oldest unacknowledged message by capture time, then insertion order."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbox ORDER BY captured_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
        return _row_to_item(row) if row else None

    def batch(self, limit: int) -> tuple[OutboxItem, ...]:
        """Oldest-first page used by the drain loop."""
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM outbox ORDER BY captured_at ASC, rowid ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return tuple(_row_to_item(row) for row in rows)

    def delete(self, message_id: str) -> bool:
        """Remove one acknowledged message. Call only after PUBACK."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM outbox WHERE message_id=?", (message_id,)
            )
            return cursor.rowcount > 0

    def record_attempt(self, message_id: str, *, at: str | None = None) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE outbox SET attempts=attempts+1, last_attempt_at=? "
                "WHERE message_id=?",
                (at or utc_now(), message_id),
            )

    def depth(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
        return int(row["n"])

    def bytes_used(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(byte_size), 0) AS n FROM outbox"
            ).fetchone()
        return int(row["n"])

    def stats(self) -> OutboxStats:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS depth,"
                " COALESCE(SUM(byte_size), 0) AS bytes_used,"
                " MIN(captured_at) AS oldest,"
                " MAX(captured_at) AS newest FROM outbox"
            ).fetchone()
            return OutboxStats(
                depth=int(row["depth"]),
                bytes_used=int(row["bytes_used"]),
                oldest_captured_at=row["oldest"],
                newest_captured_at=row["newest"],
                dropped_messages=self._counter("droppedMessages"),
                dropped_imu_samples=self._counter("droppedImuSamples"),
                first_missing_captured_at=(
                    self._connection.execute(
                        "SELECT value FROM outbox_meta WHERE key='firstMissingCapturedAt'"
                    ).fetchone()
                    or {"value": None}
                )["value"],
                last_missing_captured_at=(
                    self._connection.execute(
                        "SELECT value FROM outbox_meta WHERE key='lastMissingCapturedAt'"
                    ).fetchone()
                    or {"value": None}
                )["value"],
            )

    def evict(
        self,
        *,
        max_bytes: int,
        max_age_seconds: int,
        now: datetime | None = None,
    ) -> int:
        """Enforce the size/age bound, dropping oldest routine frames first.

        Critical messages (DTC, status, metadata) are evicted only when no
        routine frame remains. Returns the number of evicted messages.
        """
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        evicted = 0
        with self._lock:
            if max_age_seconds > 0:
                rows = self._connection.execute(
                    "SELECT message_id, captured_at, priority FROM outbox"
                ).fetchall()
                expired = [
                    row["message_id"]
                    for row in rows
                    if (current - parse_utc(row["captured_at"])).total_seconds()
                    > max_age_seconds
                ]
                if expired:
                    evicted += self._drop(expired)

            if max_bytes > 0:
                while self.bytes_used() > max_bytes:
                    row = self._connection.execute(
                        "SELECT message_id FROM outbox "
                        "ORDER BY CASE priority WHEN ? THEN 0 ELSE 1 END,"
                        " captured_at ASC, rowid ASC LIMIT 1",
                        (PRIORITY_ROUTINE,),
                    ).fetchone()
                    if row is None:
                        break
                    evicted += self._drop([row["message_id"]])
        return evicted

    def _drop(self, message_ids: Iterable[str]) -> int:
        ids = list(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        rows = self._connection.execute(
            f"SELECT message_id, captured_at, payload FROM outbox "
            f"WHERE message_id IN ({placeholders})",
            ids,
        ).fetchall()
        if not rows:
            return 0

        imu_samples = 0
        captures: list[str] = []
        for row in rows:
            captures.append(row["captured_at"])
            imu_samples += _imu_sample_count(row["payload"])

        self._connection.execute(
            f"DELETE FROM outbox WHERE message_id IN ({placeholders})", ids
        )
        self._bump_counter("droppedMessages", len(rows))
        self._bump_counter("droppedImuSamples", imu_samples)

        first = min(captures)
        last = max(captures)
        existing_first = self._connection.execute(
            "SELECT value FROM outbox_meta WHERE key='firstMissingCapturedAt'"
        ).fetchone()
        if existing_first is None or first < existing_first["value"]:
            self._set_meta("firstMissingCapturedAt", first)
        existing_last = self._connection.execute(
            "SELECT value FROM outbox_meta WHERE key='lastMissingCapturedAt'"
        ).fetchone()
        if existing_last is None or last > existing_last["value"]:
            self._set_meta("lastMissingCapturedAt", last)
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SqliteOutbox":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _imu_sample_count(payload: bytes) -> int:
    """Best-effort IMU accounting for eviction reporting."""
    try:
        document = json.loads(bytes(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0
    if not isinstance(document, dict):
        return 0
    imu = document.get("payload", {}).get("imu", {})
    if not isinstance(imu, dict):
        return 0
    samples = imu.get("samples")
    if isinstance(samples, list):
        return len(samples)
    count = imu.get("sampleCount")
    return int(count) if isinstance(count, int) else 0


def _row_to_item(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        message_id=row["message_id"],
        topic=row["topic"],
        payload=bytes(row["payload"]),
        qos=int(row["qos"]),
        retain=bool(row["retain"]),
        priority=row["priority"],
        captured_at=row["captured_at"],
        enqueued_at=row["enqueued_at"],
        attempts=int(row["attempts"]),
        last_attempt_at=row["last_attempt_at"],
        byte_size=int(row["byte_size"]),
    )
