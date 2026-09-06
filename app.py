"""Research Companion OS - local-first research memory server.

The implementation deliberately uses only the Python standard library. SQLite is
the machine source of truth; Obsidian is a generated, human-readable projection.
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
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_DB = ROOT / "research_companion.db"
DEFAULT_VAULT = ROOT / "vault"
NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

MEMORY_TYPES = {
    "observation", "decision", "decision_rationale", "experiment", "finding",
    "failure", "root_cause", "hypothesis", "evidence", "question", "idea",
    "procedure", "project_narrative", "skill", "researcher_model", "note",
}
MEMORY_STATES = {"HOT", "WARM", "COLD", "ARCHIVED"}


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
                "workspace_dir": str(ROOT),
                "agent_command": "",
                "agent_timeout": "180",
            }
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (key, value, NOW()))
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
            rows = db.execute(f"SELECT * FROM memories{where} ORDER BY last_accessed DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
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
        return {"project": project, "counts": counts, "type_counts": type_counts, "memories": memories[:20], "edges": self.edges()}

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

    def sync_obsidian(self, vault: str, project_id: str | None = None) -> dict[str, Any]:
        vault_path = Path(vault).expanduser().resolve()
        vault_path.mkdir(parents=True, exist_ok=True)
        projects = [self.project(project_id)] if project_id else self.projects()
        written: list[str] = []
        for project in filter(None, projects):
            pdir = vault_path / "Projects" / slug(project["name"])
            (pdir / "Memory").mkdir(parents=True, exist_ok=True)
            state = ["---", f"id: {project['id']}", f"status: {project['status']}", f"updated_at: {project['updated_at']}", "tags: [research-companion, project]", "---", f"# {project['name']}", "", project.get("description", ""), "", "## Current objective", project.get("current_objective", "(not set)"), "", "## Active questions", *(f"- {x}" for x in project.get("active_questions", [])), "", "## Active hypotheses", *(f"- {x}" for x in project.get("active_hypotheses", [])), "", "## Blockers", *(f"- {x}" for x in project.get("current_blockers", [])), "", "## Next actions", *(f"- {x}" for x in project.get("next_actions", [])), ""]
            path = pdir / "Project State.md"; path.write_text("\n".join(state), encoding="utf-8"); written.append(str(path))
            for memory in self.memories(project["id"], limit=500):
                mdir = pdir / "Memory" / memory["type"]; mdir.mkdir(parents=True, exist_ok=True)
                content = ["---", f"id: {memory['id']}", f"type: {memory['type']}", f"state: {memory['state']}", f"source_type: {memory['source_type']}", f"confidence: {memory['confidence']:.2f}", f"importance: {memory['importance']:.2f}", f"created_at: {memory['created_at']}", "tags: [research-companion]", "---", f"# {memory['title']}", "", memory["content"], "", f"## Provenance\n- type: {memory['source_type']}\n- id: {memory['source_id'] or '(none)'}"]
                mp = mdir / f"{memory['id']}-{slug(memory['title'])}.md"; mp.write_text("\n".join(content), encoding="utf-8"); written.append(str(mp))
        return {"vault": str(vault_path), "written": written, "count": len(written)}

    def settings(self) -> dict[str, str]:
        with self.connect() as db:
            return {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}

    def update_settings(self, data: dict[str, Any]) -> dict[str, str]:
        allowed = {"vault_path", "workspace_dir", "agent_command", "agent_timeout"}
        with self.lock, self.connect() as db:
            for key, value in data.items():
                if key in allowed and value is not None:
                    if key == "vault_path" and not str(value).strip(): value = str(DEFAULT_VAULT)
                    if key == "workspace_dir" and not str(value).strip(): value = str(ROOT)
                    db.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), NOW()))
        current = self.settings()
        Path(current["vault_path"]).expanduser().resolve().mkdir(parents=True, exist_ok=True)
        return current

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
            return [dict(row) for row in db.execute("SELECT c.*, (SELECT content FROM chat_messages m WHERE m.conversation_id=c.id ORDER BY m.created_at DESC LIMIT 1) AS last_message FROM conversations c ORDER BY c.updated_at DESC LIMIT ?", (max(1, min(limit, 200)),))]

    def _save_chat_message(self, db: sqlite3.Connection, conversation_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        db.execute("INSERT INTO chat_messages(id,conversation_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?,?)", (uid("msg"), conversation_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), NOW()))

    def _agent_command(self, command: str, prompt: str, cwd: str, timeout: int) -> tuple[str, int]:
        workdir = Path(cwd).expanduser().resolve()
        if not workdir.exists() or not workdir.is_dir():
            return f"作業ディレクトリが存在しません: {workdir}", 2
        command = command.strip()
        if not command:
            return "エージェントコマンドが未設定です。右上の設定から、例: `codex exec` や `claude -p` を指定してください。", 0
        # {prompt} is replaced with one safely quoted argument. Without it, the
        # prompt is sent on stdin, which works with most CLI agents.
        rendered = command
        stdin = prompt
        if "{prompt}" in command:
            rendered = command.replace("{prompt}", subprocess.list2cmdline([prompt]))
            stdin = ""
        try:
            proc = subprocess.run(rendered, cwd=str(workdir), input=stdin, text=True, capture_output=True, timeout=max(10, min(timeout, 900)), shell=True)
        except subprocess.TimeoutExpired:
            return f"エージェントが{timeout}秒以内に終了しませんでした。", 124
        output = (proc.stdout or "").strip()
        if proc.stderr:
            output = (output + "\n\n[stderr]\n" + proc.stderr.strip()).strip()
        return output or "エージェントから出力がありませんでした。", proc.returncode

    def _chat_memory(self, project_id: str | None, user_message: str, assistant_message: str, conversation_id: str) -> dict[str, Any] | None:
        text = f"{user_message}\n{assistant_message}"
        explicit = user_message.strip().lower().startswith(("/remember", "/decision", "/failure", "/question", "/evidence", "/finding"))
        signals = ("決定", "決め", "失敗", "原因", "結論", "知見", "覚えて", "重要", "次回", "decision", "failure", "lesson", "remember")
        if not explicit and not any(signal in text.lower() for signal in signals):
            return None
        first = user_message.strip().splitlines()[0] if user_message.strip() else "Chat insight"
        memory_type = "note"
        for prefix, kind in (("/decision", "decision"), ("/failure", "failure"), ("/question", "question"), ("/evidence", "evidence"), ("/finding", "finding")):
            if user_message.strip().lower().startswith(prefix):
                memory_type = kind; first = user_message.strip()[len(prefix):].strip() or kind.title(); break
        if user_message.strip().lower().startswith("/remember"):
            first = user_message.strip()[9:].strip() or first
        return self.create_memory({
            "project_id": project_id, "type": memory_type, "title": first[:120],
            "content": f"User: {user_message.strip()}\n\nAgent: {assistant_message.strip()}",
            "source_type": "chat_message", "source_id": conversation_id,
            "importance": .7 if explicit else .55, "confidence": .65,
        })

    def _write_conversation_projection(self, conversation_id: str) -> str:
        settings = self.settings(); vault = Path(settings["vault_path"]).expanduser().resolve(); vault.mkdir(parents=True, exist_ok=True)
        conversation = self.conversation(conversation_id)
        if not conversation: raise ValueError("conversation not found")
        directory = vault / "Conversations"; directory.mkdir(parents=True, exist_ok=True)
        filename = f"{conversation_id}-{slug(conversation['title'])}.md"
        lines = ["---", f"id: {conversation_id}", f"project_id: {conversation.get('project_id') or ''}", "tags: [research-companion, conversation]", "---", f"# {conversation['title']}", ""]
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
        context = self.compile_context(project_id, message, 8)["packet"] if project_id else ""
        history = self.conversation(conversation_id)["messages"][-10:]
        prompt = "You are the user's research/coding agent. Work in the supplied directory. Preserve provenance and do not invent facts.\n\nRESEARCH CONTEXT:\n" + context + "\n\nRECENT CHAT:\n" + "\n".join(f"{m['role']}: {m['content']}" for m in history) + f"\n\nUSER:\n{message}"
        with self.lock, self.connect() as db:
            self._save_chat_message(db, conversation_id, "user", message, {"workspace_dir": workspace})
            title = message[:60] if conversation.get("title") == "New research chat" else conversation["title"]
            db.execute("UPDATE conversations SET title=?,project_id=?,workspace_dir=?,agent_command=?,updated_at=? WHERE id=?", (title, project_id, workspace, command, NOW(), conversation_id))
        assistant, exit_code = self._agent_command(command, prompt, workspace, timeout)
        if exit_code != 0: assistant = f"Agent error (exit {exit_code})\n\n{assistant}"
        with self.lock, self.connect() as db:
            self._save_chat_message(db, conversation_id, "assistant", assistant, {"exit_code": exit_code, "workspace_dir": workspace, "agent_command": command})
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (NOW(), conversation_id))
        memory = self._chat_memory(project_id, message, assistant, conversation_id)
        transcript = self._write_conversation_projection(conversation_id)
        if project_id:
            self.sync_obsidian(settings["vault_path"], project_id)
        return {"conversation_id": conversation_id, "message": {"role": "assistant", "content": assistant, "exit_code": exit_code}, "memory": memory, "transcript_path": transcript, "context_used": bool(context), "settings": self.settings()}

    def jobs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY name")]

    def run_job(self, job_name: str) -> dict[str, Any]:
        if job_name == "hourly_maintenance": result = {"forgetting": self.forget()}
        elif job_name == "daily_research_review": result = {"forgetting": self.forget(), "review": "project state remains user-controlled; no unverified facts were invented"}
        elif job_name == "weekly_cross_project_review": result = {"consolidation": self.consolidate()}
        else: raise ValueError("unknown job")
        with self.lock, self.connect() as db:
            db.execute("UPDATE jobs SET last_run=?,next_run=? WHERE name=?", (NOW(), datetime.fromtimestamp(time.time()+3600, timezone.utc).isoformat(timespec="seconds"), job_name))
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
    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") if content_type == "application/json" else payload.encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", f"{content_type}; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(body)
    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")
    def do_OPTIONS(self) -> None:
        self.send_response(204); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Access-Control-Allow-Headers", "Content-Type"); self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS"); self.end_headers()
    def do_GET(self) -> None:
        parsed = urlparse(self.path); path = parsed.path; q = parse_qs(parsed.query)
        try:
            if path == "/api/health": return self._send(200, {"ok": True, "service": "Research Companion OS", "time": NOW()})
            if path == "/api/settings": return self._send(200, self.store.settings())
            if path == "/api/projects": return self._send(200, self.store.projects())
            if path == "/api/conversations": return self._send(200, self.store.conversations())
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
        path = urlparse(self.path).path; data = self._json()
        try:
            if path == "/api/projects": return self._send(201, self.store.create_project(data))
            if path == "/api/conversations": return self._send(201, self.store.create_conversation(data))
            if path == "/api/chat": return self._send(200, self.store.chat(data))
            if path == "/api/memories": return self._send(201, self.store.create_memory(data))
            if path == "/api/search": return self._send(200, {"results": self.store.search(data.get("query", ""), data.get("project_id"), data.get("intent", "recall"), int(data.get("limit", 12)))})
            if path == "/api/context/compile": return self._send(200, self.store.compile_context(data["project_id"], data.get("query", ""), int(data.get("limit", 12))))
            if path == "/api/maintenance/forget": return self._send(200, self.store.forget())
            if path == "/api/maintenance/consolidate": return self._send(200, self.store.consolidate(data.get("project_id")))
            if path == "/api/obsidian/sync": return self._send(200, self.store.sync_obsidian(data["vault"], data.get("project_id")))
            if path == "/api/jobs/run": return self._send(200, self.store.run_job(data["name"]))
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})
    def do_PUT(self) -> None:
        path = urlparse(self.path).path; data = self._json()
        try:
            if path == "/api/settings": return self._send(200, self.store.update_settings(data))
            if path.startswith("/api/projects/"): return self._send(200, self.store.update_project(path.rsplit("/", 1)[1], data) or {"error": "not found"})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Companion OS")
    parser.add_argument("--host", default=os.getenv("RESEARCH_COMPANION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RESEARCH_COMPANION_PORT", "8765")))
    parser.add_argument("--db", default=os.getenv("RESEARCH_COMPANION_DB", str(DEFAULT_DB)))
    args = parser.parse_args()
    Handler.store = ResearchStore(args.db)
    scheduler = Scheduler(Handler.store); scheduler.start()
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Research Companion OS running at http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: scheduler.stop_event.set(); server.server_close()


if __name__ == "__main__":
    main()
