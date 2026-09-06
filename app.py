"""Research Companion OS - local-first research memory server.

SQLite is the machine source of truth; Obsidian is a generated, human-readable
projection. PDF extraction/rendering and CPU OCR are provided by the optional
PDF stack in requirements.txt (the native build bundles it).
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import math
import os
import re
import sqlite3
import subprocess
import base64
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pdf_library import copy_into_library, inspect_pdf, ocr_page, render_page, safe_name


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def default_data_root() -> Path:
    """Return a per-user data directory without embedding a machine path."""
    configured = os.getenv("RESEARCH_COMPANION_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
        if base:
            return (Path(base) / "ResearchCompanion").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "ResearchCompanion").resolve()
    base = os.getenv("XDG_DATA_HOME")
    return ((Path(base) if base else Path.home() / ".local" / "share") / "research-companion").resolve()


DATA_ROOT = default_data_root()
DEFAULT_DB = DATA_ROOT / "research_companion.db"
DEFAULT_VAULT = DATA_ROOT / "vault"
DEFAULT_WORKSPACE = DATA_ROOT
DEFAULT_AGENT_COMMAND = "codex exec --skip-git-repo-check --model gpt-5.6-luna -c model_reasoning_effort=low"
LEGACY_DEFAULT_AGENT_COMMAND = "codex exec --model gpt-5.6-luna -c model_reasoning_effort=low"
NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

MEMORY_TYPES = {
    "observation", "decision", "decision_rationale", "experiment", "finding",
    "failure", "root_cause", "hypothesis", "evidence", "question", "idea",
    "procedure", "project_narrative", "skill", "researcher_model", "note",
}
MEMORY_STATES = {"HOT", "WARM", "COLD", "ARCHIVED"}
TRUSTED_ORIGINS = {
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "http://[::1]:8765",
    "http://tauri.localhost",
    "https://tauri.localhost",
}
APP_REQUEST_HEADER = "X-Research-Companion"
APP_REQUEST_VALUE = "desktop"
PROJECTION_DIR = "Research Companion"
VAULT_HOME = "Home.md"
VAULT_ROOT_GUIDE = "Research Companion.md"
AGENT_MEMORY_BLOCK = re.compile(r"\[\[memory\]\]\s*(\{.*?\})\s*\[\[/memory\]\]", re.IGNORECASE | re.DOTALL)


class ClosingConnection(sqlite3.Connection):
    """Make ``with connect()`` close the handle as well as committing it."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slug(value: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", value, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_]+", "-", value) or "untitled"


def json_load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def tokens(text: str) -> list[str]:
    # Keeps Japanese runs intact while still supporting English identifiers.
    return re.findall(r"[\w一-龯ぁ-んァ-ヶー]{2,}", (text or "").lower(), flags=re.UNICODE)


def vectorize(text: str, dimensions: int = 128) -> list[float]:
    """Small deterministic local embedding fallback, suitable for a notebook PC."""
    vector = [0.0] * dimensions
    words = tokens(text)
    grams = words + [text[i : i + 3].lower() for i in range(max(0, len(text) - 2))]
    for item in grams:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 8) for v in vector]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


class ResearchStore:
    """SQLite persistence and cognitive operations."""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self.lock = threading.RLock()
        self.process_lock = threading.RLock()
        self.active_processes: dict[str, subprocess.Popen[bytes]] = {}
        self.run_states: dict[str, dict[str, Any]] = {}
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def _init_db(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
                    status TEXT DEFAULT 'active', current_objective TEXT DEFAULT '',
                    active_questions TEXT DEFAULT '[]', active_hypotheses TEXT DEFAULT '[]',
                    current_blockers TEXT DEFAULT '[]', recent_findings TEXT DEFAULT '[]',
                    important_decisions TEXT DEFAULT '[]', current_assumptions TEXT DEFAULT '[]',
                    next_actions TEXT DEFAULT '[]', relevant_skills TEXT DEFAULT '[]',
                    risk_items TEXT DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, project_id TEXT, type TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, source_type TEXT DEFAULT 'direct_observation', source_id TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.7, importance REAL DEFAULT 0.5, novelty REAL DEFAULT 0.5,
                    reuse_probability REAL DEFAULT 0.5, rediscovery_cost REAL DEFAULT 0.5,
                    dependency_count INTEGER DEFAULT 0, activation REAL DEFAULT 0.5,
                    state TEXT DEFAULT 'WARM', created_at TEXT NOT NULL, last_accessed TEXT NOT NULL,
                    access_count INTEGER DEFAULT 0, metadata_json TEXT DEFAULT '{}',
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id TEXT PRIMARY KEY, vector_json TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    relation TEXT NOT NULL, strength REAL DEFAULT 0.5, created_at TEXT NOT NULL,
                    UNIQUE(source_id, target_id, relation),
                    FOREIGN KEY(source_id) REFERENCES memories(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event_type TEXT NOT NULL, entity_id TEXT,
                    payload_json TEXT DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, interval_seconds INTEGER NOT NULL,
                    last_run TEXT, next_run TEXT, enabled INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT,
                    workspace_dir TEXT DEFAULT '', agent_command TEXT DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, metadata_json TEXT DEFAULT '{}', created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS pdf_documents (
                    id TEXT PRIMARY KEY, project_id TEXT, title TEXT NOT NULL,
                    original_filename TEXT NOT NULL, stored_path TEXT NOT NULL,
                    discipline TEXT NOT NULL DEFAULT 'Uncategorized', author TEXT DEFAULT '',
                    page_count INTEGER NOT NULL DEFAULT 0, file_size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL, extracted_text TEXT DEFAULT '', summary TEXT DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS pdf_pages (
                    id TEXT PRIMARY KEY, pdf_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                    text TEXT DEFAULT '', translation TEXT DEFAULT '', translation_status TEXT DEFAULT 'not_started',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(pdf_id, page_number),
                    FOREIGN KEY(pdf_id) REFERENCES pdf_documents(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED, title, content, type, project_id UNINDEXED
                );
                INSERT OR IGNORE INTO jobs(id,name,interval_seconds,next_run) VALUES
                    ('job_hourly','hourly_maintenance',3600,NULL),
                    ('job_daily','daily_research_review',86400,NULL),
                    ('job_weekly','weekly_cross_project_review',604800,NULL);
                """
            )
        DEFAULT_VAULT.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            defaults = {
                "vault_path": str(DEFAULT_VAULT),
                "workspace_dir": str(DEFAULT_WORKSPACE),
                "agent_command": DEFAULT_AGENT_COMMAND,
                "agent_timeout": "180",
            }
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (key, value, NOW()))
            db.execute(
                "UPDATE settings SET value=?,updated_at=? WHERE key='agent_command' AND value=?",
                (DEFAULT_AGENT_COMMAND, NOW(), LEGACY_DEFAULT_AGENT_COMMAND),
            )
            for row in db.execute("SELECT name,interval_seconds FROM jobs WHERE next_run IS NULL").fetchall():
                next_run = datetime.now(timezone.utc) + timedelta(seconds=int(row["interval_seconds"]))
                db.execute("UPDATE jobs SET next_run=? WHERE name=?", (next_run.isoformat(timespec="seconds"), row["name"]))
            # Older releases appended the agent's stderr stream to successful
            # assistant messages. Remove that diagnostic tail once, while
            # leaving user text and genuine failed-agent diagnostics intact.
            for row in db.execute("SELECT id,content,metadata_json FROM chat_messages WHERE role='assistant' AND content LIKE '%[stderr]%'").fetchall():
                metadata = json_load(row["metadata_json"], {})
                if isinstance(metadata, dict) and metadata.get("exit_code", 0) != 0:
                    continue
                cleaned = re.split(r"\r?\n\[stderr\]\s*", row["content"], maxsplit=1)[0].rstrip()
                if cleaned and cleaned != row["content"]:
                    db.execute("UPDATE chat_messages SET content=? WHERE id=?", (cleaned, row["id"]))
        if not self.projects():
            self.create_project({
                "name": "Research Companion OS",
                "description": "長期研究開発のためのローカル研究伴走基盤",
                "current_objective": "最初の研究目標を定義する",
                "next_actions": ["最初のDecisionまたはQuestionを記録する"],
            })

    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for field in (
            "active_questions", "active_hypotheses", "current_blockers", "recent_findings",
            "important_decisions", "current_assumptions", "next_actions", "relevant_skills", "risk_items",
            "metadata_json",
        ):
            if field in item:
                item[field[:-5] if field.endswith("_json") else field] = json_load(item.pop(field), [] if field != "metadata_json" else {})
        return item

    def project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def projects(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [self._row(row) for row in db.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def delete_project(self, project_id: str) -> dict[str, Any] | None:
        """Permanently remove one project and its private research records.

        Archiving is represented by the existing ``status=archived`` update.
        This destructive operation is deliberately separate and returns counts
        so the UI can show exactly what will disappear before confirmation.
        """
        with self.lock, self.connect() as db:
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                return None
            project_count = int(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
            if project_count <= 1:
                raise ValueError("最後のプロジェクトは削除できません")
            memory_ids = [r[0] for r in db.execute("SELECT id FROM memories WHERE project_id=?", (project_id,)).fetchall()]
            conversation_ids = [r[0] for r in db.execute("SELECT id FROM conversations WHERE project_id=?", (project_id,)).fetchall()]
            pdf_rows = db.execute("SELECT id,stored_path FROM pdf_documents WHERE project_id=?", (project_id,)).fetchall()
            pdf_ids = [r[0] for r in pdf_rows]
            for conversation_id in conversation_ids:
                db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            for memory_id in memory_ids:
                db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            db.execute("DELETE FROM pdf_documents WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        library_root = (Path(self.settings()["vault_path"]).expanduser().resolve() / PROJECTION_DIR / "PDF Library").resolve()
        for row in pdf_rows:
            path = Path(row[1]).expanduser().resolve()
            if library_root in path.parents and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        return {"project_id": project_id, "name": project["name"], "memories": len(memory_ids), "conversations": len(conversation_ids), "pdfs": len(pdf_ids)}

    def pdfs(self, project_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses, args = [], []
        if project_id:
            clauses.append("project_id=?"); args.append(project_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute(f"SELECT id,project_id,title,original_filename,stored_path,discipline,author,page_count,file_size,sha256,summary,created_at,updated_at FROM pdf_documents{where} ORDER BY updated_at DESC LIMIT ?", (*args, max(1, min(int(limit), 100000)))).fetchall()
            return [dict(row) for row in rows]

    def pdf(self, pdf_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM pdf_documents WHERE id=?", (pdf_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["pages"] = [dict(page) for page in db.execute("SELECT id,page_number,text,translation,translation_status,created_at,updated_at FROM pdf_pages WHERE pdf_id=? ORDER BY page_number", (pdf_id,)).fetchall()]
            return item

    def _pdf_path(self, pdf_id: str) -> Path:
        with self.connect() as db:
            row = db.execute("SELECT stored_path FROM pdf_documents WHERE id=?", (pdf_id,)).fetchone()
        if not row:
            raise ValueError("PDFが見つかりません")
        path = Path(row[0]).expanduser().resolve()
        if not path.is_file():
            raise ValueError("保存済みPDFが見つかりません。Vaultの場所を確認してください")
        return path

    def import_pdf(self, data: dict[str, Any]) -> dict[str, Any]:
        project_id = data.get("project_id") or (self.projects()[0]["id"] if self.projects() else None)
        if project_id and not self.project(project_id):
            raise ValueError("project not found")
        source_path: Path | None = None
        temporary_path: Path | None = None
        try:
            if data.get("path"):
                source_path = Path(str(data["path"])).expanduser().resolve()
            elif data.get("data_base64"):
                raw = str(data["data_base64"])
                if "," in raw and raw.lower().startswith("data:"):
                    raw = raw.split(",", 1)[1]
                temporary_path = Path(self.db_path).resolve().parent / f".pdf-upload-{uuid.uuid4().hex}.pdf"
                temporary_path.write_bytes(base64.b64decode(raw, validate=True))
                source_path = temporary_path
            else:
                raise ValueError("PDFファイルを選択してください")
            info = inspect_pdf(source_path)
            ocr_pages: list[int] = []
            if data.get("ocr", True):
                for page_number, text in enumerate(info["pages"], 1):
                    if text:
                        continue
                    try:
                        recovered = ocr_page(source_path, page_number)
                    except RuntimeError:
                        recovered = ""
                    if recovered:
                        info["pages"][page_number - 1] = recovered
                        ocr_pages.append(page_number)
                info["text"] = "\n\n".join(f"[Page {index + 1}]\n{text}" for index, text in enumerate(info["pages"]) if text)
            with self.connect() as db:
                duplicate = db.execute("SELECT id FROM pdf_documents WHERE sha256=? AND (project_id=? OR (? IS NULL AND project_id IS NULL)) LIMIT 1", (info["sha256"], project_id, project_id)).fetchone()
            if duplicate:
                return {"duplicate": True, "document": self.pdf(duplicate[0])}
            pdf_id = uid("pdf")
            discipline = str(data.get("discipline") or "Uncategorized").strip()[:80] or "Uncategorized"
            vault = Path(self.settings()["vault_path"]).expanduser().resolve()
            stored_path = copy_into_library(source_path, vault, pdf_id, discipline)
            title = str(data.get("title") or info["title"] or source_path.stem).strip()[:240] or source_path.stem
            now = NOW()
            with self.lock, self.connect() as db:
                db.execute("INSERT INTO pdf_documents(id,project_id,title,original_filename,stored_path,discipline,author,page_count,file_size,sha256,extracted_text,summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (pdf_id, project_id, title, source_path.name, str(stored_path), discipline, info["author"], info["page_count"], info["file_size"], info["sha256"], info["text"], "", now, now))
                for page_number, text in enumerate(info["pages"], 1):
                    db.execute("INSERT INTO pdf_pages(id,pdf_id,page_number,text,created_at,updated_at) VALUES(?,?,?,?,?,?)", (uid("pdfpage"), pdf_id, page_number, text, now, now))
            return {"duplicate": False, "ocr_pages": ocr_pages, "document": self.pdf(pdf_id)}
        finally:
            if temporary_path and temporary_path.is_file():
                temporary_path.unlink()

    def delete_pdf(self, pdf_id: str) -> bool:
        with self.lock, self.connect() as db:
            row = db.execute("SELECT stored_path FROM pdf_documents WHERE id=?", (pdf_id,)).fetchone()
            if not row:
                return False
            path = Path(row[0]).expanduser().resolve()
            vault = Path(self.settings()["vault_path"]).expanduser().resolve()
            library_root = (vault / PROJECTION_DIR / "PDF Library").resolve()
            db.execute("DELETE FROM pdf_documents WHERE id=?", (pdf_id,))
        if library_root in path.parents and path.is_file():
            path.unlink()
        return True

    def summarize_pdf(self, pdf_id: str) -> dict[str, Any]:
        document = self.pdf(pdf_id)
        if not document:
            raise ValueError("PDFが見つかりません")
        text = document.get("extracted_text", "").strip()
        if not text:
            raise ValueError("文字を抽出できないPDFです。画像PDFにはOCRが必要です")
        settings = self.settings()
        prompt = "You are a careful research-paper assistant. Summarize the supplied PDF text in Japanese. " \
            "Separate confirmed claims, methods, limitations, and practical implications. Do not invent details. " \
            "Return concise Markdown with headings and preserve page references when visible.\n\nPDF:\n" + text[:60000]
        output, code = self._agent_command_with_id(settings["agent_command"], prompt, settings["workspace_dir"], int(settings.get("agent_timeout", "180")), uid("pdfsummary"))
        if code != 0:
            raise ValueError(f"要約エージェントが失敗しました (exit {code})\n{output[-1200:]}")
        now = NOW()
        with self.lock, self.connect() as db:
            db.execute("UPDATE pdf_documents SET summary=?,updated_at=? WHERE id=?", (output.strip(), now, pdf_id))
        memory = self.create_memory({"project_id": document.get("project_id"), "type": "finding", "title": f"PDF要約: {document['title']}", "content": output.strip(), "source_type": "pdf_summary", "source_id": pdf_id, "importance": .75, "confidence": .65, "metadata": {"pdf_id": pdf_id, "page_count": document["page_count"]}})
        return {"document": self.pdf(pdf_id), "memory": memory}

    def translate_pdf_page(self, pdf_id: str, page_number: int) -> dict[str, Any]:
        document = self.pdf(pdf_id)
        if not document:
            raise ValueError("PDFが見つかりません")
        page = next((page for page in document["pages"] if page["page_number"] == page_number), None)
        if not page:
            raise ValueError("ページ番号が範囲外です")
        if not page["text"].strip():
            now = NOW()
            with self.connect() as db:
                db.execute("UPDATE pdf_pages SET translation_status=?,updated_at=? WHERE pdf_id=? AND page_number=?", ("needs_ocr", now, pdf_id, page_number))
            raise ValueError("このページは画像PDFのため文字を抽出できません。CPU OCRまたは外部OCRを設定してください")
        settings = self.settings()
        prompt = "Translate the following research PDF page from English to Japanese. Preserve equations, citations, names, and uncertainty. Return only the translation.\n\n" + page["text"][:24000]
        output, code = self._agent_command_with_id(settings["agent_command"], prompt, settings["workspace_dir"], int(settings.get("agent_timeout", "180")), uid("pdftranslate"))
        if code != 0:
            raise ValueError(f"翻訳エージェントが失敗しました (exit {code})\n{output[-1200:]}")
        now = NOW()
        with self.lock, self.connect() as db:
            db.execute("UPDATE pdf_pages SET translation=?,translation_status=?,updated_at=? WHERE pdf_id=? AND page_number=?", (output.strip(), "translated", now, pdf_id, page_number))
        return {"pdf_id": pdf_id, "page_number": page_number, "translation": output.strip(), "translation_status": "translated"}

    def create_project(self, data: dict[str, Any]) -> dict[str, Any]:
        project_id = data.get("id") or uid("proj")
        now = NOW()
        fields = {
            "name": data.get("name") or "Untitled Research Project",
            "description": data.get("description", ""), "status": data.get("status", "active"),
            "current_objective": data.get("current_objective", ""),
        }
        arrays = ("active_questions", "active_hypotheses", "current_blockers", "recent_findings", "important_decisions", "current_assumptions", "next_actions", "relevant_skills", "risk_items")
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,description,status,current_objective,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, fields["name"], fields["description"], fields["status"], fields["current_objective"], now, now),
            )
            for key in arrays:
                if key in data:
                    db.execute(f"UPDATE projects SET {key}=? WHERE id=?", (json.dumps(data[key], ensure_ascii=False), project_id))
        return self.project(project_id)  # type: ignore[return-value]

    def update_project(self, project_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "description", "status", "current_objective", "active_questions", "active_hypotheses", "current_blockers", "recent_findings", "important_decisions", "current_assumptions", "next_actions", "relevant_skills", "risk_items"}
        values = []
        sets = []
        for key, value in data.items():
            if key in allowed:
                sets.append(f"{key}=?")
                values.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
        if not sets:
            return self.project(project_id)
        sets.append("updated_at=?"); values.extend([NOW(), project_id])
        with self.lock, self.connect() as db:
            db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", values)
        return self.project(project_id)

    def _sync_fts(self, db: sqlite3.Connection, memory_id: str) -> None:
        db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        row = db.execute("SELECT id,title,content,type,project_id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row:
            db.execute("INSERT INTO memory_fts(memory_id,title,content,type,project_id) VALUES(?,?,?,?,?)", tuple(row))

    def create_memory(self, data: dict[str, Any]) -> dict[str, Any]:
        memory_id = data.get("id") or uid("mem")
        now = NOW()
        memory_type = data.get("type", "note") if data.get("type", "note") in MEMORY_TYPES else "note"
        state = data.get("state", "WARM") if data.get("state", "WARM") in MEMORY_STATES else "WARM"
        values = (
            memory_id, data.get("project_id"), memory_type, data.get("title") or memory_type.title(), data.get("content", ""),
            data.get("source_type", "direct_observation"), data.get("source_id", ""), float(data.get("confidence", 0.7)),
            float(data.get("importance", 0.5)), float(data.get("novelty", 0.5)), float(data.get("reuse_probability", 0.5)),
            float(data.get("rediscovery_cost", 0.5)), int(data.get("dependency_count", 0)), float(data.get("activation", 0.5)),
            state, now, now, int(data.get("access_count", 0)), json.dumps(data.get("metadata", {}), ensure_ascii=False),
        )
        with self.lock, self.connect() as db:
            db.execute("""INSERT INTO memories(id,project_id,type,title,content,source_type,source_id,confidence,importance,novelty,reuse_probability,rediscovery_cost,dependency_count,activation,state,created_at,last_accessed,access_count,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            db.execute("INSERT INTO memory_embeddings(memory_id,vector_json) VALUES(?,?)", (memory_id, json.dumps(vectorize(values[3] + "\n" + values[4]))))
            self._sync_fts(db, memory_id)
            for related in data.get("related_memory_ids", []):
                self._edge(db, memory_id, related, data.get("relation", "RELATED_TO"), 0.6)
            db.execute("INSERT INTO events(id,event_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?)", (uid("evt"), "memory_created", memory_id, json.dumps({"type": memory_type}), now))
        return self.memory(memory_id)  # type: ignore[return-value]

    def memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["metadata"] = json_load(item.pop("metadata_json"), {})
            return item

    def memories(self, project_id: str | None = None, memory_type: str | None = None, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, args = [], []
        if project_id:
            clauses.append("project_id=?"); args.append(project_id)
        if memory_type:
            clauses.append("type=?"); args.append(memory_type)
        if state:
            clauses.append("state=?"); args.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM memories{where} ORDER BY last_accessed DESC LIMIT ?", (*args, max(1, min(limit, 100000)))).fetchall()
            result = []
            for row in rows:
                item = dict(row); item["metadata"] = json_load(item.pop("metadata_json"), {}); result.append(item)
            return result

    def _edge(self, db: sqlite3.Connection, source: str, target: str, relation: str, strength: float) -> None:
        if source == target:
            return
        db.execute("INSERT OR IGNORE INTO edges(id,source_id,target_id,relation,strength,created_at) VALUES(?,?,?,?,?,?)", (uid("edge"), source, target, relation, strength, NOW()))

    def edges(self, memory_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if memory_id:
                rows = db.execute("SELECT * FROM edges WHERE source_id=? OR target_id=? ORDER BY strength DESC", (memory_id, memory_id)).fetchall()
            else:
                rows = db.execute("SELECT * FROM edges ORDER BY strength DESC LIMIT 500").fetchall()
            return [dict(row) for row in rows]

    def update_memory(self, memory_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "project_id", "type", "title", "content", "source_type", "source_id",
            "confidence", "importance", "novelty", "reuse_probability", "rediscovery_cost",
            "dependency_count", "activation", "state", "metadata",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in data.items():
            if key not in allowed:
                continue
            if key == "type" and value not in MEMORY_TYPES:
                continue
            if key == "state" and value not in MEMORY_STATES:
                continue
            if key == "metadata":
                value = json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)
            sets.append(f"{key if key != 'metadata' else 'metadata_json'}=?")
            values.append(value)
        if not sets:
            return self.memory(memory_id)
        sets.append("last_accessed=?")
        values.extend([NOW(), memory_id])
        with self.lock, self.connect() as db:
            exists = db.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not exists:
                return None
            db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", values)
            self._sync_fts(db, memory_id)
            db.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
            item = db.execute("SELECT title,content FROM memories WHERE id=?", (memory_id,)).fetchone()
            db.execute("INSERT INTO memory_embeddings(memory_id,vector_json) VALUES(?,?)", (memory_id, json.dumps(vectorize(item["title"] + "\n" + item["content"]))))
        return self.memory(memory_id)

    def delete_memory(self, memory_id: str) -> bool:
        with self.lock, self.connect() as db:
            exists = db.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not exists:
                return False
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return True

    def search(self, query: str, project_id: str | None = None, intent: str = "recall", limit: int = 12) -> list[dict[str, Any]]:
        qvec = vectorize(query)
        terms = tokens(query)
        lexical: dict[str, float] = {}
        if terms:
            fts_query = " OR ".join('"' + term.replace('"', '') + '"' for term in terms[:16])
            with self.connect() as db:
                try:
                    rows = db.execute("SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? LIMIT 100", (fts_query,)).fetchall()
                    ranks = [abs(float(r["rank"])) for r in rows] or [1.0]
                    max_rank = max(ranks) or 1.0
                    lexical = {r["memory_id"]: abs(float(r["rank"])) / max_rank for r in rows}
                except sqlite3.OperationalError:
                    lexical = {}
        candidates = self.memories(project_id=project_id, limit=500)
        with self.connect() as db:
            emb_rows = db.execute("SELECT memory_id,vector_json FROM memory_embeddings").fetchall()
            vectors = {r["memory_id"]: json_load(r["vector_json"], []) for r in emb_rows}
            degrees = {r["id"]: 0 for r in db.execute("SELECT id FROM memories").fetchall()}
            for r in db.execute("SELECT source_id,target_id FROM edges").fetchall():
                degrees[r["source_id"]] = degrees.get(r["source_id"], 0) + 1
                degrees[r["target_id"]] = degrees.get(r["target_id"], 0) + 1
        now = time.time()
        scored = []
        for item in candidates:
            age_days = max(0.0, (now - datetime.fromisoformat(item["last_accessed"]).timestamp()) / 86400) if item.get("last_accessed") else 30
            temporal = math.exp(-age_days / 90.0)
            semantic = cosine(qvec, vectors.get(item["id"], []))
            project_score = 1.0 if project_id and item.get("project_id") == project_id else 0.25
            utility = (item["importance"] * 0.25 + item["reuse_probability"] * 0.2 + item["rediscovery_cost"] * 0.25 + item["confidence"] * 0.15 + item["dependency_count"] / 10 * 0.15)
            type_boost = 0.0
            if intent in {"failure", "debug"} and item["type"] in {"failure", "root_cause", "experiment"}: type_boost = 0.25
            if intent in {"decision", "planning"} and item["type"] in {"decision", "decision_rationale", "question", "hypothesis"}: type_boost = 0.25
            if intent in {"evidence", "literature"} and item["type"] in {"evidence", "finding", "observation"}: type_boost = 0.25
            noise = 0.55 if item["state"] == "ARCHIVED" else 0.25 if item["state"] == "COLD" else 0.0
            score = semantic * 0.28 + lexical.get(item["id"], 0.0) * 0.24 + min(1.0, degrees.get(item["id"], 0) / 6) * 0.08 + temporal * 0.10 + project_score * 0.12 + utility * 0.18 + type_boost - noise
            item["score"] = round(score, 5)
            item["score_breakdown"] = {"semantic": round(semantic, 3), "bm25": round(lexical.get(item["id"], 0.0), 3), "temporal": round(temporal, 3), "utility": round(utility, 3), "graph_degree": degrees.get(item["id"], 0)}
            scored.append(item)
        scored.sort(key=lambda x: x["score"], reverse=True)
        selected = scored[: max(1, min(limit, 50))]
        with self.lock, self.connect() as db:
            for item in selected:
                db.execute("UPDATE memories SET access_count=access_count+1,last_accessed=?,activation=MIN(1.0,activation+0.05),state=CASE WHEN state='COLD' THEN 'WARM' ELSE state END WHERE id=?", (NOW(), item["id"]))
        return selected

    def compile_context(self, project_id: str, query: str = "", limit: int = 12) -> dict[str, Any]:
        project = self.project(project_id)
        if not project:
            raise ValueError("project not found")
        query = query or project.get("current_objective", "") or project["name"]
        relevant = self.search(query, project_id, "recall", limit)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in relevant:
            grouped.setdefault(item["type"], []).append(item)
        def bullets(values: list[Any], fallback: str = "(none recorded)") -> str:
            return "\n".join(f"- {v}" for v in values[:10]) if values else fallback
        lines = [
            "# Agent Context Packet", "", f"Generated: {NOW()}", "", "## PROJECT", project["name"],
            f"Status: {project['status']}", f"Description: {project.get('description','')}", "",
            "## CURRENT OBJECTIVE", project.get("current_objective", "(not set)"), "",
            "## CURRENT BLOCKERS", bullets(project.get("current_blockers", [])), "",
            "## ACTIVE QUESTIONS", bullets(project.get("active_questions", [])), "",
            "## ACTIVE HYPOTHESES", bullets(project.get("active_hypotheses", [])), "",
            "## IMPORTANT DECISIONS", bullets(project.get("important_decisions", [])), "",
            "## NEXT LIKELY ACTION", bullets(project.get("next_actions", []), "- Review the highest-scoring relevant memory and choose a testable next step."), "",
            "## RELEVANT MEMORY",
        ]
        for item in relevant:
            provenance = f"{item['source_type']}:{item['source_id']}" if item["source_id"] else item["source_type"]
            lines += [f"### [{item['type'].upper()}] {item['title']}", f"{item['content'][:1600]}", f"_provenance: {provenance} | confidence: {item['confidence']:.2f} | score: {item['score']:.3f}_", ""]
        lines += ["## OPERATING RULES", "- Treat provenance as authoritative metadata, not as a substitute for verification.", "- Distinguish direct statements, observations, inferences, and speculation.", "- Reuse past failures and decisions before proposing a new path.", "- Keep the working set small; record durable changes back into the OS."]
        return {"project": project, "query": query, "memories": relevant, "packet": "\n".join(lines)}

    def overview(self, project_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        if not project:
            raise ValueError("project not found")
        memories = self.memories(project_id, limit=500)
        counts = {state: sum(1 for m in memories if m["state"] == state) for state in MEMORY_STATES}
        type_counts = {kind: sum(1 for m in memories if m["type"] == kind) for kind in MEMORY_TYPES if any(m["type"] == kind for m in memories)}
        memory_ids = {m["id"] for m in memories}
        edges = [edge for edge in self.edges() if edge["source_id"] in memory_ids or edge["target_id"] in memory_ids]
        return {"project": project, "counts": counts, "type_counts": type_counts, "memories": memories[:20], "edges": edges}

    def forget(self) -> dict[str, Any]:
        now = time.time(); changed = []
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE state != 'ARCHIVED'").fetchall()
            for row in rows:
                age = max(0.0, (now - datetime.fromisoformat(row["last_accessed"]).timestamp()) / 86400)
                retention = row["importance"] * .25 + row["reuse_probability"] * .2 + row["rediscovery_cost"] * .25 + row["dependency_count"] / 10 * .1 + row["activation"] * .1 + row["confidence"] * .1
                if row["type"] in {"decision", "failure", "evidence", "hypothesis", "question"}:
                    retention += .12
                new_state = "HOT" if retention >= .78 and age < 14 else "WARM" if retention >= .48 and age < 60 else "COLD" if retention >= .25 else "ARCHIVED"
                if row["state"] != new_state:
                    db.execute("UPDATE memories SET state=? WHERE id=?", (new_state, row["id"])); changed.append({"id": row["id"], "from": row["state"], "to": new_state})
            db.execute("INSERT INTO events(id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (uid("evt"), "forgetting_cycle", json.dumps({"changed": len(changed)}), NOW()))
        return {"changed": changed, "count": len(changed)}

    def consolidate(self, project_id: str | None = None) -> dict[str, Any]:
        items = self.memories(project_id, limit=500)
        with self.connect() as db:
            vectors = {r["memory_id"]: json_load(r["vector_json"], []) for r in db.execute("SELECT * FROM memory_embeddings")}
        clusters: list[list[dict[str, Any]]] = []
        used: set[str] = set()
        for item in items:
            if item["id"] in used or item["state"] == "ARCHIVED": continue
            cluster = [item]
            for other in items:
                if other["id"] in used or other["id"] == item["id"] or other["type"] != item["type"]: continue
                if cosine(vectors.get(item["id"], []), vectors.get(other["id"], [])) >= .88:
                    cluster.append(other)
            if len(cluster) >= 2:
                clusters.append(cluster); used.update(x["id"] for x in cluster)
        created = []
        for cluster in clusters:
            summary = "\n".join(f"- {x['title']}: {x['content'][:500]}" for x in cluster[:8])
            consolidated = self.create_memory({"project_id": cluster[0]["project_id"], "type": cluster[0]["type"], "title": f"Consolidated {cluster[0]['type']} ({len(cluster)} records)", "content": f"Rule-based consolidation of related records. Original records remain traceable in the archive.\n\n{summary}", "source_type": "consolidation", "source_id": ",".join(x["id"] for x in cluster), "importance": max(x["importance"] for x in cluster), "reuse_probability": max(x["reuse_probability"] for x in cluster), "rediscovery_cost": max(x["rediscovery_cost"] for x in cluster), "state": "WARM"})
            with self.lock, self.connect() as db:
                for original in cluster:
                    db.execute("UPDATE memories SET state='ARCHIVED' WHERE id=?", (original["id"],))
                    self._edge(db, consolidated["id"], original["id"], "CONSOLIDATES", .9)
            created.append(consolidated["id"])
        return {"clusters": len(clusters), "created": created}

    def _conversation_filename(self, conversation: dict[str, Any]) -> str:
        return f"{conversation['id']}-{slug(conversation['title'])}.md"

    def sync_obsidian(self, vault: str, project_id: str | None = None) -> dict[str, Any]:
        """Build a navigable, plugin-free Obsidian knowledge base.

        SQLite remains canonical. The generated Vault is deliberately organized
        around landing pages, project maps, typed memories, and backlinks rather
        than exposing a flat transcript directory.
        """
        vault_path = Path(vault).expanduser().resolve()
        vault_path.mkdir(parents=True, exist_ok=True)
        manifest_path = vault_path / ".research-companion-manifest.json"
        previous_manifest = json_load(manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else None, {})
        previous_files = previous_manifest.get("files", {}) if isinstance(previous_manifest, dict) else {}
        projects = [self.project(project_id)] if project_id else self.projects()
        all_projects = [p for p in self.projects() if p]
        all_memories = self.memories(limit=100000)
        all_conversations = [self.conversation(c["id"]) or c for c in self.conversations(limit=100000)]
        all_pdfs = self.pdfs(limit=100000)
        written: list[str] = []
        # A project-scoped sync must not delete projections belonging to other
        # projects. A full sync is the cleanup boundary for the whole Vault.
        current_files: dict[str, str] = dict(previous_files) if project_id else {}
        conflicts: list[str] = []

        def write_projection(path: Path, text: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            normalized = text.rstrip() + "\n"
            path_key = str(path)
            if path.is_file() and path_key in previous_files:
                current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if current_hash != previous_files[path_key]:
                    # Never destroy a hand-edited projection. Keep the stale
                    # generated page and put the fresh projection beside it.
                    update_path = path.with_name(f"{path.stem} (Research Companion update){path.suffix}")
                    if not update_path.is_file() or update_path.read_text(encoding="utf-8") != normalized:
                        update_path.write_text(normalized, encoding="utf-8")
                    current_files[path_key] = previous_files[path_key]
                    conflicts.append(str(path))
                    return
            path.write_text(normalized, encoding="utf-8")
            written.append(str(path))
            current_files[path_key] = hashlib.sha256(path.read_bytes()).hexdigest()

        project_paths = {
            p["id"]: f"{PROJECTION_DIR}/Projects/{slug(p['name'])}/Project Overview"
            for p in all_projects
        }
        memory_paths: dict[str, str] = {}
        for memory in all_memories:
            if memory.get("project_id") in project_paths:
                project_slug = slug(next(p["name"] for p in all_projects if p["id"] == memory["project_id"]))
                memory_paths[memory["id"]] = f"{PROJECTION_DIR}/Projects/{project_slug}/Memory/{memory['type']}/{memory['id']}-{slug(memory['title'])}"
        conversation_paths = {
            c["id"]: f"{PROJECTION_DIR}/Conversations/{self._conversation_filename(c)[:-3]}"
            for c in all_conversations
        }
        pdf_paths = {
            pdf["id"]: f"{PROJECTION_DIR}/Projects/{slug(next((p['name'] for p in all_projects if p['id'] == pdf.get('project_id')), 'Uncategorized'))}/Papers/{safe_name(pdf.get('discipline', 'Uncategorized'), 'Uncategorized')}/{pdf['id']}-{safe_name(pdf['title'])}"
            for pdf in all_pdfs if pdf.get("project_id") in project_paths
        }

        # A vault landing page explains the generated structure without relying
        # on Dataview or another Obsidian plugin.
        home_lines = [
            "<!-- Generated by Research Companion. Do not edit this file directly. -->",
            "---", "tags: [research-companion, home]", "type: index", "---",
            "# Research Companion", "",
            "このVaultはResearch Companionの研究知識ベースです。SQLiteを正本とし、このフォルダはObsidianで読むための投影です。", "",
            "## 使い方", "",
            "- プロジェクトを選び、Project Overviewから研究状態を確認する。",
            "- MemoryはDecision・Question・Evidenceなどの種類ごとに辿る。",
            "- 各Memoryの「Knowledge links」から関連知識と元会話へ移動する。",
            "- アプリの管理画面から検索・編集・同期を行う。", "",
            "## Projects", "",
        ]
        for project in all_projects:
            project_memories = [m for m in all_memories if m.get("project_id") == project["id"]]
            home_lines.append(f"- [[{project_paths[project['id']]}|{project['name']}]] — {len(project_memories)} memories / {project['status']}")
        home_lines += ["", "## Recent conversations", ""]
        for conversation in all_conversations[:12]:
            link = conversation_paths[conversation["id"]]
            home_lines.append(f"- [[{link}|{conversation['title']}]]")
        home_lines += ["", "## Commands", "", "- `/help` — コマンド一覧", "- `/status` — 現在の研究状態", "- `/search 検索語` — 知識検索", "- `/decision 内容` — Decisionを保存", "- `/question 内容` — Questionを保存", "- `/sync` — Vaultを再生成"]
        write_projection(vault_path / PROJECTION_DIR / VAULT_HOME, "\n".join(home_lines))
        write_projection(vault_path / VAULT_ROOT_GUIDE, "<!-- Generated by Research Companion. Do not edit this file directly. -->\n# Research Companion\n\n[[Research Companion/Home|Vault Homeを開く]]")

        for project in filter(None, projects):
            pslug = slug(project["name"])
            pdir = vault_path / PROJECTION_DIR / "Projects" / pslug
            project_memories = [m for m in all_memories if m.get("project_id") == project["id"]]
            project_conversations = [c for c in all_conversations if c.get("project_id") == project["id"]]
            project_pdfs = [pdf for pdf in all_pdfs if pdf.get("project_id") == project["id"]]
            overview_lines = [
                "<!-- Generated by Research Companion. Do not edit this file directly. -->",
                "---", f"id: {project['id']}", f"status: {project['status']}", f"updated_at: {project['updated_at']}", "tags: [research-companion, project]", "type: project", "---",
                f"# {project['name']}", "", project.get("description", ""), "",
                "## Navigation", "", f"- [[{PROJECTION_DIR}/Home|Vault Home]]", "- [[Memory Index|Memory Index]]", "",
                "## Research State", "", f"**Status:** {project['status']}", "", f"**Current objective:** {project.get('current_objective') or '(not set)'}", "",
                "### Active questions", *(f"- {x}" for x in project.get("active_questions", []) or ["(none recorded)"]), "",
                "### Active hypotheses", *(f"- {x}" for x in project.get("active_hypotheses", []) or ["(none recorded)"]), "",
                "### Blockers", *(f"- {x}" for x in project.get("current_blockers", []) or ["(none recorded)"]), "",
                "### Next actions", *(f"- {x}" for x in project.get("next_actions", []) or ["(none recorded)"]), "",
                "## Knowledge map", "",
            ]
            for memory_type in sorted({m["type"] for m in project_memories}):
                typed = [m for m in project_memories if m["type"] == memory_type]
                overview_lines.append(f"- [[Memory Index#{memory_type.title()}|{memory_type.title()}]] ({len(typed)})")
            if not project_memories:
                overview_lines.append("- まだMemoryがありません。チャットで `/remember` または `/decision` を使ってください。")
            overview_lines += ["", "## Conversations", ""]
            for conversation in project_conversations[:20]:
                overview_lines.append(f"- [[{conversation_paths[conversation['id']]}|{conversation['title']}]]")
            overview_lines += ["", "## Papers", ""]
            if project_pdfs:
                overview_lines.extend(f"- [[{pdf_paths[pdf['id']]}|{pdf['title']}]] — {pdf['discipline']} · {pdf['page_count']} pages" for pdf in project_pdfs if pdf["id"] in pdf_paths)
            else:
                overview_lines.append("- まだPDFがありません。管理画面のPDF Libraryから追加できます。")
            write_projection(pdir / "Project Overview.md", "\n".join(overview_lines))
            # Keep the old path as a readable compatibility link for existing
            # users who bookmarked the original projection.
            write_projection(pdir / "Project State.md", "<!-- Generated by Research Companion. Do not edit this file directly. -->\n# Project State\n\n[[Project Overview|Open the current project overview]]")

            index_lines = [
                "<!-- Generated by Research Companion. Do not edit this file directly. -->",
                "---", f"project_id: {project['id']}", "tags: [research-companion, memory-index]", "type: index", "---",
                f"# {project['name']} — Memory Index", "", "Project: [[Project Overview]]", "",
            ]
            for memory_type in sorted({m["type"] for m in project_memories}):
                index_lines += [f"## {memory_type.title()}", ""]
                for memory in [m for m in project_memories if m["type"] == memory_type]:
                    filename = memory_paths.get(memory["id"], "").rsplit("/", 1)[-1]
                    index_lines.append(f"- [[Memory/{memory_type}/{filename}|{memory['title']}]] — {memory['state']} · confidence {memory['confidence']:.2f}")
                index_lines.append("")
            if not project_memories:
                index_lines.append("まだMemoryがありません。")
            write_projection(pdir / "Memory Index.md", "\n".join(index_lines))

            for memory in project_memories:
                related_links: list[str] = []
                for edge in self.edges(memory["id"]):
                    related_id = edge["target_id"] if edge["source_id"] == memory["id"] else edge["source_id"]
                    if related_id in memory_paths:
                        related = self.memory(related_id)
                        related_links.append(f"- [[{memory_paths[related_id]}|{related['title'] if related else related_id}]] — {edge['relation']}")
                knowledge_links = [f"- Project: [[{project_paths[project['id']]}|{project['name']}]]"]
                source_conversation = conversation_paths.get(memory.get("source_id")) if memory.get("source_type") in {"chat_message", "slash_command", "agent_decided"} else None
                if source_conversation:
                    knowledge_links.append(f"- Source conversation: [[{source_conversation}|open conversation]]")
                if related_links:
                    knowledge_links += ["", "### Related memories", *related_links]
                content = [
                    "<!-- Generated by Research Companion. Do not edit this file directly. -->",
                    "---", f"id: {memory['id']}", f"type: {memory['type']}", f"state: {memory['state']}", f"source_type: {memory['source_type']}", f"confidence: {memory['confidence']:.2f}", f"importance: {memory['importance']:.2f}", f"created_at: {memory['created_at']}", "tags: [research-companion, memory]", "---",
                    f"# {memory['title']}", "", memory["content"], "", "## Knowledge links", *knowledge_links,
                    "", "## Provenance", f"- type: {memory['source_type']}", f"- id: {memory['source_id'] or '(none)'}",
                ]
                filename = memory_paths[memory["id"]].rsplit("/", 1)[-1]
                write_projection(pdir / "Memory" / memory["type"] / f"{filename}.md", "\n".join(content))

            for pdf in project_pdfs:
                pdf_note = [
                    "<!-- Generated by Research Companion. Do not edit this file directly. -->",
                    "---", f"id: {pdf['id']}", f"project_id: {project['id']}", f"discipline: {pdf['discipline']}", f"page_count: {pdf['page_count']}", "tags: [research-companion, paper]", "type: paper", "---",
                    f"# {pdf['title']}", "", f"- Project: [[{project_paths[project['id']]}|{project['name']}]]", f"- Original filename: `{pdf['original_filename']}`", f"- Stored PDF: `{pdf['stored_path']}`", "",
                    "## Summary", "", pdf.get("summary") or "まだ要約されていません。アプリのPDF Libraryから要約を実行してください。", "",
                    "## Reading notes", "", "このページはPDFのメタデータと要約を保持する投影です。原文ページと翻訳はアプリのPDF Readerで確認してください。",
                ]
                pdf_filename = pdf_paths[pdf["id"]].rsplit("/", 1)[-1]
                write_projection(pdir / "Papers" / safe_name(pdf.get("discipline", "Uncategorized"), "Uncategorized") / f"{pdf_filename}.md", "\n".join(pdf_note))

        # Transcripts are retained, but they now link back into the knowledge
        # graph and list any durable memories created from the conversation.
        for conversation in all_conversations:
            if project_id and conversation.get("project_id") != project_id:
                continue
            lines = [
                "<!-- Generated by Research Companion. Do not edit this file directly. -->",
                "---", f"id: {conversation['id']}", f"project_id: {conversation.get('project_id') or ''}", "tags: [research-companion, conversation]", "type: conversation", "---",
                f"# {conversation['title']}", "", f"- [[{PROJECTION_DIR}/Home|Vault Home]]",
            ]
            if conversation.get("project_id") in project_paths:
                lines.append(f"- [[{project_paths[conversation['project_id']]}|Project Overview]]")
            conversation_memories = [m for m in all_memories if m.get("source_id") == conversation["id"] and m.get("source_type") in {"chat_message", "slash_command", "agent_decided"}]
            if conversation_memories:
                lines += ["", "## Durable knowledge", ""]
                lines.extend(f"- [[{memory_paths[m['id']]}|{m['title']}]]" for m in conversation_memories if m["id"] in memory_paths)
            lines += ["", "## Transcript", ""]
            for message in conversation["messages"]:
                lines += [f"### {message['role'].title()} — {message['created_at']}", "", message["content"], ""]
            write_projection(vault_path / PROJECTION_DIR / "Conversations" / self._conversation_filename(conversation), "\n".join(lines))

        removed: list[str] = []
        for old_path, old_hash in previous_files.items():
            if old_path in current_files:
                continue
            candidate = Path(old_path)
            try:
                if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == old_hash:
                    candidate.unlink(); removed.append(str(candidate))
            except OSError:
                continue
        manifest_path.write_text(json.dumps({"version": 2, "files": current_files}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"vault": str(vault_path), "written": written, "removed": removed, "conflicts": conflicts, "count": len(written)}

    def import_obsidian(self, vault: str, project_id: str | None = None) -> dict[str, Any]:
        """Import only user-authored, explicitly tagged notes from the Vault."""
        vault_path = Path(vault).expanduser().resolve()
        if not vault_path.is_dir():
            return {"vault": str(vault_path), "imported": [], "count": 0}
        default_project = project_id or (self.projects()[0]["id"] if self.projects() else None)
        imported: list[str] = []
        excluded_roots = {PROJECTION_DIR.lower(), "projects", "conversations"}
        for path in vault_path.rglob("*.md"):
            try:
                relative = path.relative_to(vault_path)
            except ValueError:
                continue
            if relative.parts and relative.parts[0].lower() in excluded_roots:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "research-companion" not in raw.lower():
                continue
            frontmatter: dict[str, str] = {}
            body = raw
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) == 3:
                    body = parts[2].lstrip("\r\n")
                    for line in parts[1].splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            frontmatter[key.strip().lower()] = value.strip().strip("\"'")
            note_project = frontmatter.get("project_id") or default_project
            if project_id and note_project != project_id:
                continue
            note_type = frontmatter.get("type", "note")
            if note_type not in MEMORY_TYPES:
                note_type = "note"
            heading = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), path.stem)
            source_id = relative.as_posix()
            with self.connect() as db:
                exists = db.execute("SELECT 1 FROM memories WHERE source_type='obsidian_note' AND source_id=? LIMIT 1", (source_id,)).fetchone()
            if exists:
                continue
            memory = self.create_memory({
                "project_id": note_project,
                "type": note_type,
                "title": heading[:120] or path.stem,
                "content": body.strip(),
                "source_type": "obsidian_note",
                "source_id": source_id,
                "confidence": .75,
            })
            imported.append(memory["id"])
        return {"vault": str(vault_path), "imported": imported, "count": len(imported)}

    def settings(self) -> dict[str, str]:
        with self.connect() as db:
            settings = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}
        # Existing databases created before the Codex default was introduced
        # may still contain an empty command. Expose the new default without
        # overwriting the user's settings row on every server start.
        if not settings.get("agent_command", "").strip():
            settings["agent_command"] = DEFAULT_AGENT_COMMAND
        return settings

    def update_settings(self, data: dict[str, Any]) -> dict[str, str]:
        allowed = {"vault_path", "workspace_dir", "agent_command", "agent_timeout"}
        before = self.settings()
        before_vault = Path(before["vault_path"]).expanduser().resolve()
        with self.lock, self.connect() as db:
            for key, value in data.items():
                if key in allowed and value is not None:
                    if key == "vault_path" and not str(value).strip(): value = str(DEFAULT_VAULT)
                    if key == "workspace_dir" and not str(value).strip(): value = str(DEFAULT_WORKSPACE)
                    db.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), NOW()))
        current = self.settings()
        current_vault = Path(current["vault_path"]).expanduser().resolve()
        current_vault.mkdir(parents=True, exist_ok=True)
        if current_vault != before_vault:
            self._move_pdf_library(before_vault, current_vault)
        return current

    def _move_pdf_library(self, old_vault: Path, new_vault: Path) -> None:
        """Move registered PDF originals when the active Vault changes.

        Only paths previously created inside the old Research Companion PDF
        Library are eligible. Arbitrary files elsewhere are never touched.
        Missing originals remain referenced at their recorded location so a
        settings change cannot silently destroy the only known copy.
        """
        old_root = (old_vault / PROJECTION_DIR / "PDF Library").resolve()
        if not old_root.is_dir():
            return
        with self.connect() as db:
            rows = db.execute("SELECT id,stored_path,discipline FROM pdf_documents").fetchall()
        moved: list[tuple[str, str]] = []
        for row in rows:
            source = Path(row["stored_path"]).expanduser().resolve()
            if source == old_root or old_root not in source.parents or not source.is_file():
                continue
            target = copy_into_library(source, new_vault, row["id"], row["discipline"])
            try:
                source.unlink()
            except OSError:
                if target.is_file():
                    target.unlink()
                continue
            moved.append((str(target), row["id"]))
        if moved:
            with self.lock, self.connect() as db:
                for stored_path, pdf_id in moved:
                    db.execute("UPDATE pdf_documents SET stored_path=?,updated_at=? WHERE id=?", (stored_path, NOW(), pdf_id))
        try:
            for directory in sorted(old_root.rglob("*"), reverse=True):
                if directory.is_dir():
                    directory.rmdir()
            old_root.rmdir()
        except OSError:
            pass

    def create_conversation(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or {}
        now = NOW(); conversation_id = data.get("id") or uid("chat")
        project_id = data.get("project_id") or (self.projects()[0]["id"] if self.projects() else None)
        item = (conversation_id, data.get("title") or "New research chat", project_id, data.get("workspace_dir", ""), data.get("agent_command", ""), now, now)
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO conversations(id,title,project_id,workspace_dir,agent_command,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", item)
        return self.conversation(conversation_id)  # type: ignore[return-value]

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not row: return None
            item = dict(row)
            item["messages"] = [dict(m) for m in db.execute("SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()]
            for message in item["messages"]: message["metadata"] = json_load(message.pop("metadata_json"), {})
            return item

    def conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT c.*, (SELECT content FROM chat_messages m WHERE m.conversation_id=c.id ORDER BY m.created_at DESC LIMIT 1) AS last_message FROM conversations c ORDER BY c.updated_at DESC LIMIT ?", (max(1, min(limit, 100000)),))]

    def delete_conversation(self, conversation_id: str) -> bool:
        conversation = self.conversation(conversation_id)
        if not conversation:
            return False
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        # Conversation markdown is a generated projection, so remove only the
        # exact file belonging to this conversation when it is in the active vault.
        try:
            vault = Path(self.settings()["vault_path"]).expanduser().resolve()
            for directory in (vault / PROJECTION_DIR / "Conversations", vault / "Conversations"):
                for transcript in directory.glob(f"{conversation_id}-*.md"):
                    if transcript.is_file():
                        transcript.unlink()
        except OSError:
            pass
        return True

    def _save_chat_message(self, db: sqlite3.Connection, conversation_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        db.execute("INSERT INTO chat_messages(id,conversation_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?,?)", (uid("msg"), conversation_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), NOW()))

    def _agent_command(self, command: str, prompt: str, cwd: str, timeout: int) -> tuple[str, int]:
        return self._agent_command_with_id(command, prompt, cwd, timeout, uid("run"))

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5)
            else:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def cancel_agent(self, run_id: str) -> bool:
        with self.process_lock:
            process = self.active_processes.get(run_id)
        if not process:
            return False
        self._terminate_process(process)
        return True

    def run_status(self, run_id: str) -> dict[str, Any]:
        with self.process_lock:
            state = self.run_states.get(run_id)
            active = run_id in self.active_processes
            if not state:
                return {"run_id": run_id, "status": "not_found", "output": ""}
            stdout = bytes(state.get("stdout", b""))
            stderr = bytes(state.get("stderr", b""))
            return {
                "run_id": run_id,
                "status": "running" if active else state.get("status", "finished"),
                "output": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": state.get("exit_code"),
            }

    def workspace_status(self, cwd: str) -> dict[str, Any]:
        workdir = Path(cwd).expanduser().resolve()
        if not workdir.exists() or not workdir.is_dir():
            return {"path": str(workdir), "is_git": False, "error": "作業ディレクトリが存在しません"}
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "status", "--short"],
                capture_output=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"path": str(workdir), "is_git": False, "error": str(exc)}
        if result.returncode != 0:
            return {"path": str(workdir), "is_git": False, "changed_files": []}
        changed_files = result.stdout.decode("utf-8", errors="replace").splitlines()
        return {
            "path": str(workdir),
            "is_git": True,
            "changed_files": changed_files[:100],
            "changed_count": len(changed_files),
            "truncated": len(changed_files) > 100,
        }

    def _agent_command_with_id(self, command: str, prompt: str, cwd: str, timeout: int, run_id: str) -> tuple[str, int]:
        workdir = Path(cwd).expanduser().resolve()
        if not workdir.exists() or not workdir.is_dir():
            return f"作業ディレクトリが存在しません: {workdir}", 2
        command = command.strip()
        if not command:
            return "エージェントコマンドが未設定です。右上の設定から、例: `codex exec` や `claude -p` を指定してください。", 2
        # {prompt} is replaced with one safely quoted argument. Without it, the
        # prompt is sent on stdin, which works with most CLI agents.
        rendered = command
        stdin = prompt
        if "{prompt}" in command:
            rendered = command.replace("{prompt}", subprocess.list2cmdline([prompt]))
            stdin = ""
        try:
            # Codex expects UTF-8 on stdin. Keep the transport byte-oriented
            # instead of letting Windows choose the active code page.
            proc = subprocess.Popen(rendered, cwd=str(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
            with self.process_lock:
                self.active_processes[run_id] = proc
            with self.process_lock:
                self.run_states[run_id] = {"status": "running", "stdout": bytearray(), "stderr": bytearray(), "exit_code": None}

            def drain(stream: Any, key: str) -> None:
                try:
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        with self.process_lock:
                            buffer = self.run_states.get(run_id, {}).get(key)
                            if buffer is not None:
                                buffer.extend(chunk)
                                del buffer[:-2_000_000]
                except (OSError, ValueError):
                    pass

            readers = [
                threading.Thread(target=drain, args=(proc.stdout, "stdout"), daemon=True),
                threading.Thread(target=drain, args=(proc.stderr, "stderr"), daemon=True),
            ]
            for reader in readers: reader.start()
            timed_out = False
            try:
                if proc.stdin is not None:
                    proc.stdin.write(stdin.encode("utf-8"))
                    proc.stdin.close()
                proc.wait(timeout=max(10, min(timeout, 900)))
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
            except (BrokenPipeError, OSError) as exc:
                with self.process_lock:
                    self.run_states[run_id]["status"] = "error"
                    self.run_states[run_id]["exit_code"] = 1
                return f"エージェントへの入力に失敗しました: {exc}", 1
            finally:
                for reader in readers: reader.join(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
                with self.process_lock:
                    state = self.run_states[run_id]
                    state["exit_code"] = proc.returncode
                    state["status"] = "finished" if proc.returncode == 0 else "failed"
                    self.active_processes.pop(run_id, None)
            output = bytes(state["stdout"]).decode("utf-8", errors="replace").strip()
            stderr = bytes(state["stderr"]).decode("utf-8", errors="replace").strip()
            if proc.returncode == -9 or proc.returncode == 1 and not output:
                return "エージェントがキャンセルされました。", 130
            if timed_out:
                return (output + "\n\nエージェントをタイムアウトで終了しました。\n").strip(), 124
            if proc.returncode != 0:
                diagnostic = stderr[-4000:] if stderr else "エージェントからエラー詳細が返されませんでした。"
                return (output + "\n\n[Agent error details]\n" + diagnostic).strip(), proc.returncode
            # Codex and other CLIs write progress, environment information, and
            # token counters to stderr even on success. That is diagnostics,
            # not part of the assistant response, so keep it out of the chat.
            return output or "エージェントから出力がありませんでした。", proc.returncode
        except OSError as exc:
            return f"エージェントの起動に失敗しました: {exc}", 1

    def _chat_memory(self, project_id: str | None, user_message: str, assistant_message: str, conversation_id: str) -> tuple[str, dict[str, Any] | None]:
        """Let the agent mark durable knowledge without exposing the protocol to the user."""
        match = AGENT_MEMORY_BLOCK.search(assistant_message)
        visible_message = assistant_message
        agent_memory: dict[str, Any] | None = None
        if match:
            visible_message = (assistant_message[:match.start()] + assistant_message[match.end():]).strip()
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict) and str(parsed.get("content", "")).strip():
                    agent_memory = parsed
            except (json.JSONDecodeError, TypeError):
                agent_memory = None

        explicit = user_message.strip().lower().startswith(("/remember", "/decision", "/failure", "/question", "/evidence", "/finding"))
        legacy_marker = "[[remember]]" in visible_message.lower()
        if legacy_marker:
            visible_message = re.sub(r"\s*\[\[remember\]\]\s*", "\n", visible_message, flags=re.IGNORECASE).strip()
        if agent_memory:
            def score(name: str, fallback: float) -> float:
                try:
                    return max(0.0, min(1.0, float(agent_memory.get(name, fallback))))
                except (TypeError, ValueError):
                    return fallback

            memory = self.create_memory({
                "project_id": project_id,
                "type": agent_memory.get("type", "note"),
                "title": str(agent_memory.get("title") or "Agent insight")[:120],
                "content": str(agent_memory["content"]).strip(),
                "source_type": "agent_decided",
                "source_id": conversation_id,
                "importance": score("importance", .6),
                "confidence": score("confidence", .7),
                "reuse_probability": score("reuse_probability", .6),
                "rediscovery_cost": score("rediscovery_cost", .6),
                "metadata": {"capture_mode": "agent_decided", "reason": str(agent_memory.get("reason", ""))[:500]},
            })
            return visible_message or "エージェントが重要な知見を保存しました。", memory

        if not explicit and not legacy_marker:
            return visible_message, None
        first = user_message.strip().splitlines()[0] if user_message.strip() else "Chat insight"
        memory_type = "note"
        for prefix, kind in (("/decision", "decision"), ("/failure", "failure"), ("/question", "question"), ("/evidence", "evidence"), ("/finding", "finding")):
            if user_message.strip().lower().startswith(prefix):
                memory_type = kind; first = user_message.strip()[len(prefix):].strip() or kind.title(); break
        if user_message.strip().lower().startswith("/remember"):
            first = user_message.strip()[9:].strip() or first
        memory = self.create_memory({
            "project_id": project_id, "type": memory_type, "title": first[:120],
            "content": f"User: {user_message.strip()}\n\nAgent: {visible_message.strip()}",
            "source_type": "chat_message", "source_id": conversation_id,
            "importance": .7 if explicit else .55, "confidence": .65,
        })
        return visible_message, memory

    def _finish_chat(self, conversation_id: str, assistant: str, exit_code: int, workspace: str,
                     command: str, settings: dict[str, str], run_id: str,
                     memory: dict[str, Any] | None = None, imported: dict[str, Any] | None = None,
                     context_used: bool = False) -> dict[str, Any]:
        workspace_status = self.workspace_status(workspace)
        with self.lock, self.connect() as db:
            self._save_chat_message(db, conversation_id, "assistant", assistant, {"exit_code": exit_code, "workspace_dir": workspace, "agent_command": command, "workspace_status": workspace_status})
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (NOW(), conversation_id))
        transcript = self._write_conversation_projection(conversation_id)
        project_id = self.conversation(conversation_id).get("project_id") if self.conversation(conversation_id) else None
        if project_id:
            self.sync_obsidian(settings["vault_path"], project_id)
        return {"conversation_id": conversation_id, "run_id": run_id, "message": {"role": "assistant", "content": assistant, "exit_code": exit_code}, "memory": memory, "imported_obsidian": imported or {"count": 0}, "workspace_status": workspace_status, "transcript_path": transcript, "context_used": context_used, "settings": self.settings()}

    def _handle_slash_command(self, message: str, project_id: str | None, conversation_id: str,
                              settings: dict[str, str]) -> tuple[str, dict[str, Any] | None, dict[str, Any]] | None:
        match = re.match(r"^/([a-zA-Z][\w-]*)(?:\s+(.*))?$", message, flags=re.DOTALL)
        if not match:
            return None
        name = match.group(1).lower()
        body = (match.group(2) or "").strip()
        memory_types = {
            "remember": "note", "decision": "decision", "failure": "failure",
            "question": "question", "hypothesis": "hypothesis", "evidence": "evidence",
            "finding": "finding", "experiment": "experiment", "procedure": "procedure",
        }
        if name in memory_types:
            if not body:
                return (f"/{name} には保存する内容を入力してください。例: /{name} 内容", None, {"count": 0})
            if "|" in body:
                title, content = (part.strip() for part in body.split("|", 1))
            else:
                title, content = body[:120], body
            memory = self.create_memory({
                "project_id": project_id, "type": memory_types[name], "title": title or memory_types[name].title(),
                "content": content, "source_type": "slash_command", "source_id": conversation_id,
                "confidence": .9, "importance": .75,
            })
            return (f"{memory_types[name].title()}として知識ベースに保存しました。\n\n{memory['title']}", memory, {"count": 0})
        if name == "help":
            return ("使えるコマンド:\n\n"
                    "/remember 内容 — メモを保存\n/decision 内容 — Decisionを保存\n/failure 内容 — Failureを保存\n"
                    "/question 内容 — Questionを保存\n/hypothesis 内容 — Hypothesisを保存\n/evidence 内容 — Evidenceを保存\n"
                    "/finding 内容 — Findingを保存\n/experiment 内容 — Experimentを保存\n/procedure 内容 — Procedureを保存\n"
                    "/objective 内容 — 研究目標を更新\n/status — 現在の研究状態\n/search キーワード — 知識を検索\n"
                    "/context — Agent Context Packetを表示\n/sync — Vaultを同期\n/import — タグ付きノートを取り込み\n"
                    "/forget — 古いMemoryを整理\n/consolidate — 類似Memoryを統合", None, {"count": 0})
        if name == "objective":
            if not project_id:
                return ("プロジェクトが選択されていません。", None, {"count": 0})
            if not body:
                project = self.project(project_id)
                return (f"Current objective: {project.get('current_objective') or '(not set)'}", None, {"count": 0})
            project = self.update_project(project_id, {"current_objective": body})
            return (f"研究目標を更新しました。\n\nCurrent objective: {project['current_objective']}", None, {"count": 0})
        if name == "status":
            if not project_id:
                return ("プロジェクトが選択されていません。", None, {"count": 0})
            overview = self.overview(project_id)
            project = overview["project"]
            counts = " / ".join(f"{state}: {overview['counts'].get(state, 0)}" for state in MEMORY_STATES)
            return (f"# {project['name']}\n\n**Objective**\n{project.get('current_objective') or '(not set)'}\n\n"
                    f"**Blockers**\n{chr(10).join('- ' + x for x in project.get('current_blockers', [])) or '- なし'}\n\n"
                    f"**Next actions**\n{chr(10).join('- ' + x for x in project.get('next_actions', [])) or '- なし'}\n\n"
                    f"**Memory**\n{counts}", None, {"count": 0})
        if name == "search":
            if not body:
                return ("/search の後に検索語を入力してください。", None, {"count": 0})
            results = self.search(body, project_id, "recall", 8)
            if not results:
                return ("該当する知識が見つかりませんでした。", None, {"count": 0})
            text = "# Search results\n\n" + "\n\n".join(f"- **[{x['type']}] {x['title']}** — {x['state']}\n  {x['content'][:320]}" for x in results)
            return (text, None, {"count": 0})
        if name == "context":
            if not project_id:
                return ("プロジェクトが選択されていません。", None, {"count": 0})
            return (self.compile_context(project_id, body, 12)["packet"], None, {"count": 0})
        if name == "sync":
            result = self.sync_obsidian(settings["vault_path"])
            return (f"Vaultを同期しました。{result['count']}ファイルを更新しました。", None, result)
        if name == "import":
            result = self.import_obsidian(settings["vault_path"], project_id)
            return (f"タグ付きObsidianノートを{result['count']}件取り込みました。", None, result)
        if name == "forget":
            result = self.forget()
            return (f"Memoryの状態を更新しました。{result['count']}件が変更されました。", None, result)
        if name == "consolidate":
            result = self.consolidate(project_id)
            return (f"Memoryを統合しました。{result['clusters']}クラスタを処理しました。", None, result)
        return None

    def _write_conversation_projection(self, conversation_id: str) -> str:
        settings = self.settings(); vault = Path(settings["vault_path"]).expanduser().resolve(); vault.mkdir(parents=True, exist_ok=True)
        conversation = self.conversation(conversation_id)
        if not conversation: raise ValueError("conversation not found")
        directory = vault / PROJECTION_DIR / "Conversations"; directory.mkdir(parents=True, exist_ok=True)
        filename = self._conversation_filename(conversation)
        lines = ["<!-- Generated by Research Companion. Do not edit this file directly. -->", "---", f"id: {conversation_id}", f"project_id: {conversation.get('project_id') or ''}", "tags: [research-companion, conversation]", "type: conversation", "---", f"# {conversation['title']}", "", f"- [[{PROJECTION_DIR}/Home|Vault Home]]", ""]
        for message in conversation["messages"]:
            lines += [f"## {message['role'].title()} — {message['created_at']}", "", message["content"], ""]
        path = directory / filename; path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def chat(self, data: dict[str, Any]) -> dict[str, Any]:
        message = str(data.get("message", "")).strip()
        if not message: raise ValueError("message is required")
        project_id = data.get("project_id") or (self.projects()[0]["id"] if self.projects() else None)
        conversation_id = data.get("conversation_id")
        conversation = self.conversation(conversation_id) if conversation_id else None
        if not conversation:
            conversation = self.create_conversation({"project_id": project_id, "title": message[:60]})
            conversation_id = conversation["id"]
        settings = self.settings()
        workspace = str(data.get("workspace_dir") or conversation.get("workspace_dir") or settings["workspace_dir"])
        command = str(data.get("agent_command") if data.get("agent_command") is not None else conversation.get("agent_command") or settings["agent_command"])
        timeout = int(data.get("agent_timeout") or settings.get("agent_timeout", "180"))
        with self.lock, self.connect() as db:
            self._save_chat_message(db, conversation_id, "user", message, {"workspace_dir": workspace})
            title = message[:60] if conversation.get("title") == "New research chat" else conversation["title"]
            db.execute("UPDATE conversations SET title=?,project_id=?,workspace_dir=?,agent_command=?,updated_at=? WHERE id=?", (title, project_id, workspace, command, NOW(), conversation_id))

        # App commands are deterministic and do not need to start an external
        # process. Unknown slash-prefixed text remains available to arbitrary
        # agents, so custom agent workflows are not restricted.
        local_command = self._handle_slash_command(message, project_id, conversation_id, settings)
        if local_command is not None:
            assistant, memory, command_result = local_command
            return self._finish_chat(conversation_id, assistant, 0, workspace, "local", settings,
                                     str(data.get("run_id") or uid("local")), memory, command_result, False)

        imported = self.import_obsidian(settings["vault_path"], project_id)
        context = self.compile_context(project_id, message, 8)["packet"] if project_id else ""
        history = self.conversation(conversation_id)["messages"][-11:-1]
        prompt = "You are the user's research/coding agent. Work in the supplied directory. Preserve provenance and do not invent facts.\n\n" \
            "After answering, independently judge whether this exchange contains a durable, project-specific insight worth remembering. " \
            "Do not save greetings, routine progress, temporary suggestions, unverified speculation, or information that is only useful for this turn. " \
            "If it is worth saving, append exactly one machine-readable block at the very end, after the normal answer, using this format: " \
            "[[memory]]{\"type\":\"finding\",\"title\":\"short title\",\"content\":\"durable insight\",\"importance\":0.0,\"confidence\":0.0,\"reuse_probability\":0.0,\"rediscovery_cost\":0.0,\"reason\":\"why it will matter later\"}[[/memory]] " \
            "Use a valid Memory type, numeric scores from 0 to 1, and omit the block when nothing is durable. " \
            "The application removes this block before showing or storing the visible response.\n\nRESEARCH CONTEXT:\n" + context + "\n\nRECENT CHAT:\n" + "\n".join(f"{m['role']}: {m['content']}" for m in history) + f"\n\nUSER:\n{message}"
        run_id = str(data.get("run_id") or uid("run"))
        assistant, exit_code = self._agent_command_with_id(command, prompt, workspace, timeout, run_id)
        if exit_code != 0: assistant = f"Agent error (exit {exit_code})\n\n{assistant}"
        if exit_code == 0:
            assistant, memory = self._chat_memory(project_id, message, assistant, conversation_id)
        else:
            memory = None
        return self._finish_chat(conversation_id, assistant, exit_code, workspace, command, settings, run_id, memory, imported, bool(context))

    def jobs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY name")]

    def run_job(self, job_name: str) -> dict[str, Any]:
        if job_name == "hourly_maintenance": result = {"forgetting": self.forget()}
        elif job_name == "daily_research_review": result = {"forgetting": self.forget(), "review": "project state remains user-controlled; no unverified facts were invented"}
        elif job_name == "weekly_cross_project_review": result = {"consolidation": self.consolidate()}
        else: raise ValueError("unknown job")
        with self.lock, self.connect() as db:
            row = db.execute("SELECT interval_seconds FROM jobs WHERE name=?", (job_name,)).fetchone()
            interval = int(row["interval_seconds"]) if row else 3600
            next_run = datetime.now(timezone.utc) + timedelta(seconds=interval)
            db.execute("UPDATE jobs SET last_run=?,next_run=? WHERE name=?", (NOW(), next_run.isoformat(timespec="seconds"), job_name))
        return result


class Scheduler(threading.Thread):
    daemon = True
    def __init__(self, store: ResearchStore):
        super().__init__(name="research-companion-scheduler"); self.store = store; self.stop_event = threading.Event()
    def run(self) -> None:
        while not self.stop_event.wait(30):
            try:
                for job in self.store.jobs():
                    if not job["enabled"]: continue
                    due = not job["next_run"] or datetime.fromisoformat(job["next_run"]).timestamp() <= time.time()
                    if due: self.store.run_job(job["name"])
            except Exception:
                # Background work must never take down the local API.
                pass


class Handler(http.server.BaseHTTPRequestHandler):
    store: ResearchStore
    server_version = "ResearchCompanion/1.0"
    def log_message(self, fmt: str, *args: Any) -> None: return
    def _trusted_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if not origin or origin in TRUSTED_ORIGINS else None
    def _require_app_request(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin not in TRUSTED_ORIGINS:
            self._send(403, {"error": "untrusted origin"})
            return False
        if self.headers.get(APP_REQUEST_HEADER) != APP_REQUEST_VALUE:
            self._send(403, {"error": "missing application request header"})
            return False
        return True
    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") if content_type == "application/json" else payload.encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", f"{content_type}; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
        origin = self._trusted_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers(); self.wfile.write(body)
    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(body)
    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")
    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin")
        if origin not in TRUSTED_ORIGINS:
            return self._send(403, {"error": "untrusted origin"})
        self.send_response(204); self.send_header("Access-Control-Allow-Origin", origin); self.send_header("Vary", "Origin"); self.send_header("Access-Control-Allow-Headers", f"Content-Type, {APP_REQUEST_HEADER}"); self.send_header("Access-Control-Allow-Methods", "DELETE,GET,POST,PUT,OPTIONS"); self.end_headers()
    def do_GET(self) -> None:
        parsed = urlparse(self.path); path = parsed.path; q = parse_qs(parsed.query)
        try:
            if path == "/api/health": return self._send(200, {"ok": True, "service": "Research Companion OS", "time": NOW()})
            if path == "/api/settings": return self._send(200, self.store.settings())
            if path == "/api/projects": return self._send(200, self.store.projects())
            if path == "/api/conversations": return self._send(200, self.store.conversations())
            if path == "/api/pdfs": return self._send(200, self.store.pdfs(q.get("project_id", [None])[0], int(q.get("limit", [1000])[0])))
            if path.startswith("/api/pdfs/"):
                parts = path.split("/")
                pdf_id = parts[3]
                if len(parts) == 5 and parts[4] == "file":
                    pdf_path = self.store._pdf_path(pdf_id)
                    return self._send_bytes(200, pdf_path.read_bytes(), "application/pdf")
                if len(parts) == 6 and parts[4] == "page":
                    return self._send_bytes(200, render_page(self.store._pdf_path(pdf_id), int(parts[5])), "image/png")
                return self._send(200, self.store.pdf(pdf_id) or {"error": "not found"})
            if path.startswith("/api/runs/"):
                return self._send(200, self.store.run_status(path.rsplit("/", 1)[1]))
            if path.startswith("/api/conversations/"):
                return self._send(200, self.store.conversation(path.rsplit("/", 1)[1]) or {"error": "not found"})
            if path.startswith("/api/projects/"):
                project_id = path.split("/")[3] if path.endswith("/overview") else path.rsplit("/", 1)[1]
                if path.endswith("/overview"): return self._send(200, self.store.overview(project_id))
                return self._send(200, self.store.project(project_id) or {"error": "not found"})
            if path == "/api/memories": return self._send(200, self.store.memories(q.get("project_id", [None])[0], q.get("type", [None])[0], q.get("state", [None])[0], int(q.get("limit", [100])[0])))
            if path == "/api/graph": return self._send(200, {"edges": self.store.edges()})
            if path == "/api/jobs": return self._send(200, self.store.jobs())
            if path == "/api/memories/" or path == "/api/context/": return self._send(404, {"error": "not found"})
            if path.startswith("/api/memories/"): return self._send(200, self.store.memory(path.rsplit("/", 1)[1]) or {"error": "not found"})
            if path.startswith("/api/context/"):
                project_id = path.rsplit("/", 1)[1]; return self._send(200, self.store.compile_context(project_id, q.get("q", [""])[0], int(q.get("limit", [12])[0])))
            if path == "/" or path == "/index.html": return self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html")
            asset = (STATIC / path.removeprefix("/")).resolve()
            if STATIC in asset.parents and asset.is_file():
                content_type = "text/css" if asset.suffix == ".css" else "application/javascript" if asset.suffix == ".js" else "text/plain"
                return self._send(200, asset.read_text(encoding="utf-8"), content_type)
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})
    def do_POST(self) -> None:
        if not self._require_app_request(): return
        path = urlparse(self.path).path
        try:
            data = self._json()
            if path == "/api/projects":
                result = self.store.create_project(data)
                self.store.sync_obsidian(self.store.settings()["vault_path"], result["id"])
                return self._send(201, result)
            if path == "/api/pdfs":
                result = self.store.import_pdf(data)
                self.store.sync_obsidian(self.store.settings()["vault_path"], data.get("project_id"))
                return self._send(201, result)
            if path.startswith("/api/pdfs/") and path.endswith("/summarize"):
                pdf_id = path.split("/")[3]
                result = self.store.summarize_pdf(pdf_id)
                self.store.sync_obsidian(self.store.settings()["vault_path"], result["document"].get("project_id"))
                return self._send(200, result)
            if path.startswith("/api/pdfs/") and path.endswith("/translate"):
                pdf_id = path.split("/")[3]
                return self._send(200, self.store.translate_pdf_page(pdf_id, int(data.get("page_number", 0))))
            if path == "/api/conversations": return self._send(201, self.store.create_conversation(data))
            if path == "/api/chat": return self._send(200, self.store.chat(data))
            if path == "/api/chat/cancel": return self._send(200, {"cancelled": self.store.cancel_agent(str(data.get("run_id", "")))})
            if path == "/api/memories":
                result = self.store.create_memory(data)
                self.store.sync_obsidian(self.store.settings()["vault_path"], data.get("project_id"))
                return self._send(201, result)
            if path == "/api/search": return self._send(200, {"results": self.store.search(data.get("query", ""), data.get("project_id"), data.get("intent", "recall"), int(data.get("limit", 12)))})
            if path == "/api/context/compile": return self._send(200, self.store.compile_context(data["project_id"], data.get("query", ""), int(data.get("limit", 12))))
            if path == "/api/maintenance/forget":
                result = self.store.forget()
                self.store.sync_obsidian(self.store.settings()["vault_path"])
                return self._send(200, result)
            if path == "/api/maintenance/consolidate":
                result = self.store.consolidate(data.get("project_id"))
                self.store.sync_obsidian(self.store.settings()["vault_path"], data.get("project_id"))
                return self._send(200, result)
            if path == "/api/obsidian/sync":
                vault = str(data.get("vault") or self.store.settings()["vault_path"])
                return self._send(200, self.store.sync_obsidian(vault, data.get("project_id")))
            if path == "/api/obsidian/import":
                vault = str(data.get("vault") or self.store.settings()["vault_path"])
                return self._send(200, self.store.import_obsidian(vault, data.get("project_id")))
            if path == "/api/jobs/run":
                result = self.store.run_job(data["name"])
                self.store.sync_obsidian(self.store.settings()["vault_path"])
                return self._send(200, result)
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})
    def do_DELETE(self) -> None:
        if not self._require_app_request(): return
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/conversations/"):
                conversation_id = path.rsplit("/", 1)[1]
                if not self.store.delete_conversation(conversation_id):
                    return self._send(404, {"error": "conversation not found"})
                self.store.sync_obsidian(self.store.settings()["vault_path"])
                return self._send(200, {"deleted": True, "conversation_id": conversation_id})
            if path.startswith("/api/projects/"):
                project_id = path.rsplit("/", 1)[1]
                result = self.store.delete_project(project_id)
                if not result:
                    return self._send(404, {"error": "project not found"})
                self.store.sync_obsidian(self.store.settings()["vault_path"])
                return self._send(200, {"deleted": True, **result})
            if path.startswith("/api/pdfs/"):
                pdf_id = path.rsplit("/", 1)[1]
                if not self.store.delete_pdf(pdf_id):
                    return self._send(404, {"error": "PDF not found"})
                self.store.sync_obsidian(self.store.settings()["vault_path"])
                return self._send(200, {"deleted": True, "pdf_id": pdf_id})
            if path.startswith("/api/memories/"):
                memory_id = path.rsplit("/", 1)[1]
                if not self.store.delete_memory(memory_id):
                    return self._send(404, {"error": "memory not found"})
                self.store.sync_obsidian(self.store.settings()["vault_path"])
                return self._send(200, {"deleted": True, "memory_id": memory_id})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})
    def do_PUT(self) -> None:
        if not self._require_app_request(): return
        path = urlparse(self.path).path
        try:
            data = self._json()
            if path == "/api/settings":
                settings = self.store.update_settings(data)
                self.store.sync_obsidian(settings["vault_path"])
                return self._send(200, settings)
            if path.startswith("/api/projects/"):
                result = self.store.update_project(path.rsplit("/", 1)[1], data)
                if result:
                    self.store.sync_obsidian(self.store.settings()["vault_path"], result["id"])
                return self._send(200, result or {"error": "not found"})
            if path.startswith("/api/memories/"):
                result = self.store.update_memory(path.rsplit("/", 1)[1], data)
                if result:
                    self.store.sync_obsidian(self.store.settings()["vault_path"], result.get("project_id"))
                return self._send(200, result or {"error": "not found"})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Companion OS")
    parser.add_argument("--host", default=os.getenv("RESEARCH_COMPANION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RESEARCH_COMPANION_PORT", "8765")))
    parser.add_argument("--db", default=os.getenv("RESEARCH_COMPANION_DB", str(DEFAULT_DB)))
    parser.add_argument("--enable-scheduler", action="store_true", help="enable background maintenance jobs")
    args = parser.parse_args()
    Handler.store = ResearchStore(args.db)
    scheduler = Scheduler(Handler.store) if args.enable_scheduler or os.getenv("RESEARCH_COMPANION_ENABLE_SCHEDULER") == "1" else None
    if scheduler: scheduler.start()
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    port_file = os.getenv("RESEARCH_COMPANION_PORT_FILE")
    if port_file:
        port_path = Path(port_file).expanduser().resolve()
        port_path.parent.mkdir(parents=True, exist_ok=True)
        port_path.write_text(str(server.server_address[1]), encoding="ascii")
    print(f"Research Companion OS running at http://{args.host}:{server.server_address[1]}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        if scheduler: scheduler.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
