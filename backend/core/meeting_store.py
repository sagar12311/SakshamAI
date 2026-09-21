"""SQLite persistence for projects, meetings, transcripts, and voice profiles."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from config import get_settings


CAPTURE_STATES = {
    "recording",
    "stopping",
    "processing",
    "completed",
    "failed",
    "interrupted",
}
ASSISTANT_STATES = {"silent_monitoring", "private_request"}
PROCESSING_JOB_STATES = {"queued", "running", "completed", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MeetingStore:
    """Owns versioned meeting tables in Saksham's existing SQLite database."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path or get_settings().database_path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _migration_1(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                folder_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                title TEXT NOT NULL,
                capture_state TEXT NOT NULL,
                assistant_state TEXT NOT NULL,
                consent_confirmed INTEGER NOT NULL DEFAULT 0,
                sample_rate INTEGER NOT NULL DEFAULT 16000,
                last_sequence INTEGER NOT NULL DEFAULT -1,
                total_samples INTEGER NOT NULL DEFAULT 0,
                transcribed_samples INTEGER NOT NULL DEFAULT 0,
                pointer_samples INTEGER NOT NULL DEFAULT 0,
                audio_path TEXT,
                mom_path TEXT,
                processing_error TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                stopped_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meetings_project_started
            ON meetings(project_id, started_at DESC);

            CREATE INDEX IF NOT EXISTS idx_meetings_state
            ON meetings(capture_state);

            CREATE TABLE IF NOT EXISTS meeting_audio_frames (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                byte_count INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY (meeting_id, sequence)
            );

            CREATE TABLE IF NOT EXISTS meeting_transcript_segments (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                text TEXT NOT NULL,
                words_json TEXT NOT NULL DEFAULT '[]',
                speaker_cluster TEXT NOT NULL DEFAULT 'Speaker 1',
                speaker_label TEXT NOT NULL DEFAULT 'Speaker 1',
                profile_id TEXT,
                is_private INTEGER NOT NULL DEFAULT 0,
                is_provisional INTEGER NOT NULL DEFAULT 1,
                confidence REAL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meeting_segments_time
            ON meeting_transcript_segments(meeting_id, start_ms, end_ms);

            CREATE TABLE IF NOT EXISTS meeting_pointers (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                source_segment_ids_json TEXT NOT NULL DEFAULT '[]',
                is_provisional INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meeting_pointers_time
            ON meeting_pointers(meeting_id, start_ms, end_ms);

            CREATE TABLE IF NOT EXISTS meeting_mom_revisions (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                structured_json TEXT NOT NULL DEFAULT '{}',
                is_final INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meeting_mom_created
            ON meeting_mom_revisions(meeting_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS meeting_speaker_mappings (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                cluster_label TEXT NOT NULL,
                display_name TEXT NOT NULL,
                profile_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (meeting_id, cluster_label)
            );

            CREATE TABLE IF NOT EXISTS meeting_processing_jobs (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                provider TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS voice_profiles (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                profile_type TEXT NOT NULL,
                embedding_ciphertext BLOB NOT NULL,
                embedding_nonce BLOB NOT NULL,
                model_name TEXT NOT NULL,
                consent_confirmed INTEGER NOT NULL,
                consent_at TEXT NOT NULL,
                revoked_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_voice_profiles_active
            ON voice_profiles(profile_type, revoked_at);
            """
        )

    @staticmethod
    def _migration_2(db: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(meeting_transcript_segments)").fetchall()
        }
        if "speaker_cluster" not in columns:
            db.execute(
                """
                ALTER TABLE meeting_transcript_segments
                ADD COLUMN speaker_cluster TEXT NOT NULL DEFAULT 'Speaker 1'
                """
            )

    @staticmethod
    def _migration_3(db: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(meeting_transcript_segments)").fetchall()
        }
        if "words_json" not in columns:
            db.execute(
                """
                ALTER TABLE meeting_transcript_segments
                ADD COLUMN words_json TEXT NOT NULL DEFAULT '[]'
                """
            )

    @staticmethod
    def _migration_4(db: sqlite3.Connection) -> None:
        # Some early development databases recorded v1 before every meeting table existed.
        MeetingStore._migration_1(db)
        meeting_columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(meetings)").fetchall()
        }
        additions = {
            "expected_speaker_count": "INTEGER",
            "voice_memory_consent_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "voice_memory_consent_at": "TEXT",
            "voice_memory_consent_scope": "TEXT",
        }
        for name, definition in additions.items():
            if name not in meeting_columns:
                db.execute(f"ALTER TABLE meetings ADD COLUMN {name} {definition}")

        job_columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(meeting_processing_jobs)").fetchall()
        }
        if "attempt" not in job_columns:
            db.execute(
                "ALTER TABLE meeting_processing_jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
            )
        if "metadata_json" not in job_columns:
            db.execute(
                "ALTER TABLE meeting_processing_jobs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meeting_private_messages (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                command_type TEXT NOT NULL,
                status TEXT NOT NULL,
                text TEXT NOT NULL,
                anchor_ms INTEGER NOT NULL DEFAULT 0,
                execution_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meeting_private_messages_created
            ON meeting_private_messages(meeting_id, created_at);

            CREATE TABLE IF NOT EXISTS meeting_pending_speaker_mappings (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                speaker_ordinal INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                cluster_label TEXT,
                profile_status TEXT,
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (meeting_id, speaker_ordinal)
            );

            CREATE TABLE IF NOT EXISTS voice_profile_events (
                id TEXT PRIMARY KEY,
                profile_id TEXT REFERENCES voice_profiles(id) ON DELETE SET NULL,
                meeting_id TEXT REFERENCES meetings(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                consent_scope TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_voice_profile_events_profile
            ON voice_profile_events(profile_id, created_at);
            """
        )

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in db.execute("SELECT version FROM schema_migrations").fetchall()
            }
            migrations = {
                1: self._migration_1,
                2: self._migration_2,
                3: self._migration_3,
                4: self._migration_4,
            }
            for version, migration in migrations.items():
                if version in applied:
                    continue
                migration(db)
                db.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
            db.commit()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _create_project_sync(self, name: str, slug: str, folder_path: Path) -> dict[str, Any]:
        project_id = str(uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO projects(id, name, slug, folder_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, name, slug, str(folder_path), now, now),
            )
            db.commit()
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_from_row(row)

    async def create_project(self, name: str, slug: str, folder_path: Path) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(self._create_project_sync, name, slug, folder_path)

    def _list_projects_sync(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    async def list_projects(self) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_projects_sync)

    def _get_project_sync(self, project_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_from_row(row) if row else None

    async def get_project(self, project_id: str) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._get_project_sync, project_id)

    @staticmethod
    def _meeting_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["consent_confirmed"] = bool(result["consent_confirmed"])
        result["voice_memory_consent_confirmed"] = bool(
            result.get("voice_memory_consent_confirmed", False)
        )
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def _create_meeting_sync(
        self,
        project_id: str,
        title: str,
        sample_rate: int,
        audio_path: Path,
        metadata: dict[str, Any],
        expected_speaker_count: Optional[int],
        voice_memory_consent_confirmed: bool,
        voice_memory_consent_scope: Optional[str],
        meeting_id: Optional[str],
    ) -> dict[str, Any]:
        meeting_id = meeting_id or str(uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meetings(
                    id, project_id, title, capture_state, assistant_state,
                    consent_confirmed, sample_rate, audio_path, metadata_json,
                    expected_speaker_count, voice_memory_consent_confirmed,
                    voice_memory_consent_at, voice_memory_consent_scope,
                    started_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'recording', 'silent_monitoring', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    project_id,
                    title,
                    sample_rate,
                    str(audio_path),
                    _json(metadata),
                    expected_speaker_count,
                    int(voice_memory_consent_confirmed),
                    now if voice_memory_consent_confirmed else None,
                    voice_memory_consent_scope if voice_memory_consent_confirmed else None,
                    now,
                    now,
                    now,
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return self._meeting_from_row(row)

    async def create_meeting(
        self,
        project_id: str,
        title: str,
        sample_rate: int,
        audio_path: Path,
        metadata: Optional[dict[str, Any]] = None,
        expected_speaker_count: Optional[int] = None,
        voice_memory_consent_confirmed: bool = False,
        voice_memory_consent_scope: Optional[str] = None,
        meeting_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(
            self._create_meeting_sync,
            project_id,
            title,
            sample_rate,
            audio_path,
            metadata or {},
            expected_speaker_count,
            voice_memory_consent_confirmed,
            voice_memory_consent_scope,
            meeting_id,
        )

    def _get_meeting_sync(self, meeting_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return self._meeting_from_row(row) if row else None

    async def get_meeting(self, meeting_id: str) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._get_meeting_sync, meeting_id)

    def _list_meetings_sync(self, project_id: Optional[str], limit: int) -> list[dict[str, Any]]:
        query = "SELECT * FROM meetings"
        params: list[Any] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self._meeting_from_row(row) for row in rows]

    async def list_meetings(
        self,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_meetings_sync, project_id, limit)

    def _list_active_captures_sync(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM meetings
                WHERE capture_state IN ('recording', 'stopping')
                ORDER BY started_at
                """
            ).fetchall()
        return [self._meeting_from_row(row) for row in rows]

    async def list_active_captures(self) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_active_captures_sync)

    def _update_meeting_sync(self, meeting_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        allowed = {
            "title",
            "capture_state",
            "assistant_state",
            "last_sequence",
            "total_samples",
            "transcribed_samples",
            "pointer_samples",
            "audio_path",
            "mom_path",
            "processing_error",
            "stopped_at",
            "expected_speaker_count",
        }
        fields = {key: value for key, value in updates.items() if key in allowed}
        if not fields:
            return self._get_meeting_sync(meeting_id)
        if "capture_state" in fields and fields["capture_state"] not in CAPTURE_STATES:
            raise ValueError("Invalid capture state")
        if "assistant_state" in fields and fields["assistant_state"] not in ASSISTANT_STATES:
            raise ValueError("Invalid assistant state")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), meeting_id]
        with self._connect() as db:
            db.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", values)
            db.commit()
        return self._get_meeting_sync(meeting_id)

    async def update_meeting(self, meeting_id: str, **updates: Any) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._update_meeting_sync, meeting_id, updates)

    def _record_frame_sync(
        self,
        meeting_id: str,
        sequence: int,
        sample_count: int,
        previous_sequence: int,
        previous_samples: int,
    ) -> bool:
        now = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE meetings
                SET last_sequence = ?, total_samples = ?, updated_at = ?
                WHERE id = ? AND capture_state = 'recording'
                    AND last_sequence = ? AND total_samples = ?
                """,
                (
                    sequence,
                    previous_samples + sample_count,
                    now,
                    meeting_id,
                    previous_sequence,
                    previous_samples,
                ),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return False
            db.execute(
                """
                INSERT INTO meeting_audio_frames(
                    meeting_id, sequence, sample_count, byte_count, received_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (meeting_id, sequence, sample_count, sample_count * 2, now),
            )
            db.commit()
            return True

    async def record_frame(
        self,
        meeting_id: str,
        sequence: int,
        sample_count: int,
        previous_sequence: int,
        previous_samples: int,
    ) -> bool:
        await self.initialize()
        return await asyncio.to_thread(
            self._record_frame_sync,
            meeting_id,
            sequence,
            sample_count,
            previous_sequence,
            previous_samples,
        )

    def _add_segment_sync(self, meeting_id: str, segment: dict[str, Any]) -> dict[str, Any]:
        segment_id = str(segment.get("id") or uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meeting_transcript_segments(
                    id, meeting_id, start_ms, end_ms, text, words_json,
                    speaker_cluster, speaker_label,
                    profile_id, is_private, is_provisional, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    meeting_id,
                    int(segment["start_ms"]),
                    int(segment["end_ms"]),
                    str(segment["text"]).strip(),
                    _json(segment.get("words", [])),
                    segment.get("speaker_cluster", segment.get("speaker_label", "Speaker 1")),
                    segment.get("speaker_label", "Speaker 1"),
                    segment.get("profile_id"),
                    int(bool(segment.get("is_private", False))),
                    int(bool(segment.get("is_provisional", True))),
                    segment.get("confidence"),
                    now,
                ),
            )
            db.commit()
        return {
            "id": segment_id,
            "meeting_id": meeting_id,
            **segment,
            "created_at": now,
        }

    async def add_segment(self, meeting_id: str, segment: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(self._add_segment_sync, meeting_id, segment)

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["words"] = json.loads(result.pop("words_json") or "[]")
        result["is_private"] = bool(result["is_private"])
        result["is_provisional"] = bool(result["is_provisional"])
        return result

    def _list_segments_sync(
        self,
        meeting_id: str,
        include_private: bool,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM meeting_transcript_segments WHERE meeting_id = ?"
        params: list[Any] = [meeting_id]
        if not include_private:
            query += " AND is_private = 0"
        query += " ORDER BY start_ms, end_ms, created_at"
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self._segment_from_row(row) for row in rows]

    async def list_segments(
        self,
        meeting_id: str,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_segments_sync, meeting_id, include_private)

    def _set_segment_private_sync(self, segment_id: str, is_private: bool) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE meeting_transcript_segments SET is_private = ? WHERE id = ?",
                (int(is_private), segment_id),
            )
            db.commit()
            return cursor.rowcount == 1

    async def set_segment_private(self, segment_id: str, is_private: bool = True) -> bool:
        await self.initialize()
        return await asyncio.to_thread(self._set_segment_private_sync, segment_id, is_private)

    def _replace_final_segments_sync(self, meeting_id: str, segments: list[dict[str, Any]]) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM meeting_transcript_segments WHERE meeting_id = ?",
                (meeting_id,),
            )
            now = utc_now()
            for segment in segments:
                db.execute(
                    """
                    INSERT INTO meeting_transcript_segments(
                        id, meeting_id, start_ms, end_ms, text, words_json,
                        speaker_cluster, speaker_label,
                        profile_id, is_private, is_provisional, confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        str(segment.get("id") or uuid4()),
                        meeting_id,
                        int(segment["start_ms"]),
                        int(segment["end_ms"]),
                        str(segment["text"]).strip(),
                        _json(segment.get("words", [])),
                        segment.get("speaker_cluster", segment.get("speaker_label", "Speaker 1")),
                        segment.get("speaker_label", "Speaker 1"),
                        segment.get("profile_id"),
                        int(bool(segment.get("is_private", False))),
                        segment.get("confidence"),
                        now,
                    ),
                )
            db.commit()

    async def replace_final_segments(self, meeting_id: str, segments: list[dict[str, Any]]) -> None:
        await self.initialize()
        await asyncio.to_thread(self._replace_final_segments_sync, meeting_id, segments)

    def _add_pointer_sync(self, meeting_id: str, pointer: dict[str, Any]) -> dict[str, Any]:
        pointer_id = str(pointer.get("id") or uuid4())
        now = utc_now()
        sources = list(pointer.get("source_segment_ids", []))
        metadata = dict(pointer.get("metadata", {}))
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meeting_pointers(
                    id, meeting_id, category, content, start_ms, end_ms,
                    source_segment_ids_json, is_provisional, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pointer_id,
                    meeting_id,
                    pointer.get("category", "key_point"),
                    str(pointer["content"]).strip(),
                    int(pointer["start_ms"]),
                    int(pointer["end_ms"]),
                    _json(sources),
                    int(bool(pointer.get("is_provisional", True))),
                    _json(metadata),
                    now,
                ),
            )
            db.commit()
        return {"id": pointer_id, "meeting_id": meeting_id, **pointer, "created_at": now}

    async def add_pointer(self, meeting_id: str, pointer: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(self._add_pointer_sync, meeting_id, pointer)

    @staticmethod
    def _pointer_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["source_segment_ids"] = json.loads(result.pop("source_segment_ids_json") or "[]")
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["is_provisional"] = bool(result["is_provisional"])
        return result

    def _list_pointers_sync(
        self,
        meeting_id: str,
        include_private: bool,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM meeting_pointers WHERE meeting_id = ?"
        if not include_private:
            query += " AND category NOT LIKE 'private_%'"
        query += " ORDER BY start_ms, created_at"
        with self._connect() as db:
            rows = db.execute(query, (meeting_id,)).fetchall()
        return [self._pointer_from_row(row) for row in rows]

    async def list_pointers(
        self,
        meeting_id: str,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_pointers_sync, meeting_id, include_private)

    def _clear_generated_pointers_sync(self, meeting_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM meeting_pointers WHERE meeting_id = ? AND category NOT LIKE 'private_%'",
                (meeting_id,),
            )
            db.commit()

    async def clear_generated_pointers(self, meeting_id: str) -> None:
        await self.initialize()
        await asyncio.to_thread(self._clear_generated_pointers_sync, meeting_id)

    @staticmethod
    def _private_message_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def _add_private_message_sync(
        self,
        meeting_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        message_id = str(message.get("id") or uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meeting_private_messages(
                    id, meeting_id, role, command_type, status, text,
                    anchor_ms, execution_id, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    meeting_id,
                    str(message.get("role", "assistant")),
                    str(message.get("command_type", "response")),
                    str(message.get("status", "completed")),
                    str(message.get("text", "")).strip(),
                    max(0, int(message.get("anchor_ms", 0))),
                    message.get("execution_id"),
                    _json(message.get("metadata", {})),
                    now,
                ),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM meeting_private_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        return self._private_message_from_row(row)

    async def add_private_message(
        self,
        meeting_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(self._add_private_message_sync, meeting_id, message)

    def _list_private_messages_sync(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM meeting_private_messages
                WHERE meeting_id = ? ORDER BY created_at, id
                """,
                (meeting_id,),
            ).fetchall()
        return [self._private_message_from_row(row) for row in rows]

    async def list_private_messages(self, meeting_id: str) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_private_messages_sync, meeting_id)

    def _update_private_message_sync(
        self,
        message_id: str,
        command_type: str,
        status: str,
        execution_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            db.execute(
                """
                UPDATE meeting_private_messages
                SET command_type = ?, status = ?, execution_id = COALESCE(?, execution_id)
                WHERE id = ?
                """,
                (command_type, status, execution_id, message_id),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM meeting_private_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        return self._private_message_from_row(row) if row else None

    async def update_private_message(
        self,
        message_id: str,
        *,
        command_type: str,
        status: str,
        execution_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(
            self._update_private_message_sync,
            message_id,
            command_type,
            status,
            execution_id,
        )

    def _add_mom_revision_sync(
        self,
        meeting_id: str,
        content: str,
        structured: dict[str, Any],
        is_final: bool,
    ) -> dict[str, Any]:
        revision_id = str(uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meeting_mom_revisions(
                    id, meeting_id, content, structured_json, is_final, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (revision_id, meeting_id, content, _json(structured), int(is_final), now),
            )
            db.commit()
        return {
            "id": revision_id,
            "meeting_id": meeting_id,
            "content": content,
            "structured": structured,
            "is_final": is_final,
            "created_at": now,
        }

    async def add_mom_revision(
        self,
        meeting_id: str,
        content: str,
        structured: Optional[dict[str, Any]] = None,
        is_final: bool = False,
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(
            self._add_mom_revision_sync,
            meeting_id,
            content,
            structured or {},
            is_final,
        )

    def _get_latest_mom_sync(self, meeting_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM meeting_mom_revisions
                WHERE meeting_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["structured"] = json.loads(result.pop("structured_json") or "{}")
        result["is_final"] = bool(result["is_final"])
        return result

    async def get_latest_mom(self, meeting_id: str) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._get_latest_mom_sync, meeting_id)

    def _get_latest_final_mom_sync(self, meeting_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM meeting_mom_revisions
                WHERE meeting_id = ? AND is_final = 1
                ORDER BY created_at DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["structured"] = json.loads(result.pop("structured_json") or "{}")
        result["is_final"] = True
        return result

    async def get_latest_final_mom(self, meeting_id: str) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._get_latest_final_mom_sync, meeting_id)

    def _upsert_speaker_mapping_sync(
        self,
        meeting_id: str,
        cluster_label: str,
        display_name: str,
        profile_id: Optional[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meeting_speaker_mappings(
                    meeting_id, cluster_label, display_name, profile_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id, cluster_label) DO UPDATE SET
                    display_name = excluded.display_name,
                    profile_id = excluded.profile_id,
                    updated_at = excluded.updated_at
                """,
                (meeting_id, cluster_label, display_name, profile_id, now),
            )
            db.execute(
                """
                UPDATE meeting_transcript_segments
                SET speaker_label = ?, profile_id = ?
                WHERE meeting_id = ? AND speaker_cluster = ?
                """,
                (display_name, profile_id, meeting_id, cluster_label),
            )
            db.commit()
        return {
            "meeting_id": meeting_id,
            "cluster_label": cluster_label,
            "display_name": display_name,
            "profile_id": profile_id,
            "updated_at": now,
        }

    async def upsert_speaker_mapping(
        self,
        meeting_id: str,
        cluster_label: str,
        display_name: str,
        profile_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(
            self._upsert_speaker_mapping_sync,
            meeting_id,
            cluster_label,
            display_name,
            profile_id,
        )

    def _list_speaker_mappings_sync(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM meeting_speaker_mappings
                WHERE meeting_id = ? ORDER BY cluster_label
                """,
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_speaker_mappings(self, meeting_id: str) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_speaker_mappings_sync, meeting_id)

    def _upsert_pending_speaker_mapping_sync(
        self,
        meeting_id: str,
        speaker_ordinal: int,
        display_name: str,
        *,
        status: str = "pending",
        cluster_label: Optional[str] = None,
        profile_status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO meeting_pending_speaker_mappings(
                    meeting_id, speaker_ordinal, display_name, status,
                    cluster_label, profile_status, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id, speaker_ordinal) DO UPDATE SET
                    display_name = excluded.display_name,
                    status = excluded.status,
                    cluster_label = excluded.cluster_label,
                    profile_status = excluded.profile_status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    meeting_id,
                    speaker_ordinal,
                    display_name,
                    status,
                    cluster_label,
                    profile_status,
                    error,
                    now,
                ),
            )
            db.commit()
            row = db.execute(
                """
                SELECT * FROM meeting_pending_speaker_mappings
                WHERE meeting_id = ? AND speaker_ordinal = ?
                """,
                (meeting_id, speaker_ordinal),
            ).fetchone()
        return dict(row)

    async def upsert_pending_speaker_mapping(
        self,
        meeting_id: str,
        speaker_ordinal: int,
        display_name: str,
        **updates: Any,
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(
            self._upsert_pending_speaker_mapping_sync,
            meeting_id,
            speaker_ordinal,
            display_name,
            **updates,
        )

    def _list_pending_speaker_mappings_sync(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM meeting_pending_speaker_mappings
                WHERE meeting_id = ? ORDER BY speaker_ordinal
                """,
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_pending_speaker_mappings(self, meeting_id: str) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_pending_speaker_mappings_sync, meeting_id)

    @staticmethod
    def _processing_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json", "{}") or "{}")
        return result

    def _create_processing_job_sync(
        self,
        meeting_id: str,
        kind: str,
        state: str,
        provider: Optional[str],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if state not in PROCESSING_JOB_STATES:
            raise ValueError("Invalid processing job state")
        job_id = str(uuid4())
        now = utc_now()
        with self._connect() as db:
            attempt = int(
                db.execute(
                    """
                    SELECT COUNT(*) AS count FROM meeting_processing_jobs
                    WHERE meeting_id = ? AND kind = ?
                    """,
                    (meeting_id, kind),
                ).fetchone()["count"]
            ) + 1
            db.execute(
                """
                INSERT INTO meeting_processing_jobs(
                    id, meeting_id, kind, state, provider, attempt,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, meeting_id, kind, state, provider, attempt, _json(metadata), now, now),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM meeting_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._processing_job_from_row(row)

    async def create_processing_job(
        self,
        meeting_id: str,
        kind: str,
        state: str = "queued",
        provider: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(
            self._create_processing_job_sync,
            meeting_id,
            kind,
            state,
            provider,
            metadata or {},
        )

    def _update_processing_job_sync(
        self,
        job_id: str,
        state: str,
        provider: Optional[str],
        error: Optional[str],
        metadata: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if state not in PROCESSING_JOB_STATES:
            raise ValueError("Invalid processing job state")
        with self._connect() as db:
            db.execute(
                """
                UPDATE meeting_processing_jobs
                SET state = ?, provider = COALESCE(?, provider), error = ?,
                    metadata_json = COALESCE(?, metadata_json), updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    provider,
                    error,
                    _json(metadata) if metadata is not None else None,
                    utc_now(),
                    job_id,
                ),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM meeting_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._processing_job_from_row(row) if row else None

    async def update_processing_job(
        self,
        job_id: str,
        state: str,
        *,
        provider: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(
            self._update_processing_job_sync,
            job_id,
            state,
            provider,
            error,
            metadata,
        )

    def _list_processing_jobs_sync(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM meeting_processing_jobs
                WHERE meeting_id = ? ORDER BY created_at, kind
                """,
                (meeting_id,),
            ).fetchall()
        return [self._processing_job_from_row(row) for row in rows]

    async def list_processing_jobs(self, meeting_id: str) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_processing_jobs_sync, meeting_id)

    def _create_voice_profile_sync(self, profile: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(profile.get("id") or uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO voice_profiles(
                    id, display_name, profile_type, embedding_ciphertext,
                    embedding_nonce, model_name, consent_confirmed, consent_at,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    profile["display_name"],
                    profile["profile_type"],
                    profile["embedding_ciphertext"],
                    profile["embedding_nonce"],
                    profile["model_name"],
                    int(bool(profile["consent_confirmed"])),
                    profile.get("consent_at") or now,
                    _json(profile.get("metadata", {})),
                    now,
                    now,
                ),
            )
            db.commit()
        return {
            "id": profile_id,
            "display_name": profile["display_name"],
            "profile_type": profile["profile_type"],
            "model_name": profile["model_name"],
            "consent_confirmed": bool(profile["consent_confirmed"]),
            "consent_at": profile.get("consent_at") or now,
            "revoked_at": None,
            "metadata": profile.get("metadata", {}),
            "created_at": now,
            "updated_at": now,
        }

    async def create_voice_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(self._create_voice_profile_sync, profile)

    def _rename_voice_profile_sync(self, profile_id: str, display_name: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE voice_profiles SET display_name = ?, updated_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (display_name, utc_now(), profile_id),
            )
            db.commit()
            return cursor.rowcount == 1

    async def rename_voice_profile(self, profile_id: str, display_name: str) -> bool:
        await self.initialize()
        return await asyncio.to_thread(
            self._rename_voice_profile_sync,
            profile_id,
            display_name,
        )

    def _add_voice_profile_event_sync(
        self,
        profile_id: Optional[str],
        meeting_id: Optional[str],
        event_type: str,
        consent_scope: Optional[str],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        event_id = str(uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO voice_profile_events(
                    id, profile_id, meeting_id, event_type,
                    consent_scope, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    profile_id,
                    meeting_id,
                    event_type,
                    consent_scope,
                    _json(metadata),
                    now,
                ),
            )
            db.commit()
        return {
            "id": event_id,
            "profile_id": profile_id,
            "meeting_id": meeting_id,
            "event_type": event_type,
            "consent_scope": consent_scope,
            "metadata": metadata,
            "created_at": now,
        }

    async def add_voice_profile_event(
        self,
        *,
        profile_id: Optional[str],
        meeting_id: Optional[str],
        event_type: str,
        consent_scope: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        return await asyncio.to_thread(
            self._add_voice_profile_event_sync,
            profile_id,
            meeting_id,
            event_type,
            consent_scope,
            metadata or {},
        )

    @staticmethod
    def _voice_profile_from_row(row: sqlite3.Row, include_embedding: bool) -> dict[str, Any]:
        result = dict(row)
        result["consent_confirmed"] = bool(result["consent_confirmed"])
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        if not include_embedding:
            result.pop("embedding_ciphertext", None)
            result.pop("embedding_nonce", None)
        return result

    def _list_voice_profiles_sync(
        self,
        include_revoked: bool,
        include_embedding: bool,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM voice_profiles"
        if not include_revoked:
            query += " WHERE revoked_at IS NULL"
        query += " ORDER BY profile_type, display_name COLLATE NOCASE"
        with self._connect() as db:
            rows = db.execute(query).fetchall()
        return [self._voice_profile_from_row(row, include_embedding) for row in rows]

    async def list_voice_profiles(
        self,
        include_revoked: bool = False,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(
            self._list_voice_profiles_sync,
            include_revoked,
            include_embedding,
        )

    def _revoke_voice_profile_sync(self, profile_id: str) -> bool:
        now = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE voice_profiles SET revoked_at = ?, updated_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now, now, profile_id),
            )
            db.commit()
            return cursor.rowcount == 1

    async def revoke_voice_profile(self, profile_id: str) -> bool:
        await self.initialize()
        return await asyncio.to_thread(self._revoke_voice_profile_sync, profile_id)

    def _mark_active_interrupted_sync(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM meetings WHERE capture_state IN ('recording', 'stopping')"
            ).fetchall()
            meeting_ids = [str(row["id"]) for row in rows]
            if meeting_ids:
                now = utc_now()
                db.execute(
                    """
                    UPDATE meetings SET capture_state = 'interrupted', updated_at = ?
                    WHERE capture_state IN ('recording', 'stopping')
                    """,
                    (now,),
                )
                db.commit()
        return meeting_ids

    async def mark_active_interrupted(self) -> list[str]:
        await self.initialize()
        return await asyncio.to_thread(self._mark_active_interrupted_sync)

    def _meetings_with_expired_audio_sync(self, cutoff_iso: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM meetings
                WHERE audio_path IS NOT NULL AND stopped_at IS NOT NULL AND stopped_at < ?
                """,
                (cutoff_iso,),
            ).fetchall()
        return [self._meeting_from_row(row) for row in rows]

    async def meetings_with_expired_audio(self, cutoff_iso: str) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._meetings_with_expired_audio_sync, cutoff_iso)

    def _delete_meeting_sync(self, meeting_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            db.commit()
            return cursor.rowcount == 1

    async def delete_meeting(self, meeting_id: str) -> bool:
        await self.initialize()
        return await asyncio.to_thread(self._delete_meeting_sync, meeting_id)


_meeting_store: Optional[MeetingStore] = None


def get_meeting_store() -> MeetingStore:
    global _meeting_store
    if _meeting_store is None:
        _meeting_store = MeetingStore()
    return _meeting_store
