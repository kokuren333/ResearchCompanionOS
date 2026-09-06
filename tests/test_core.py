import base64
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import ResearchStore
from pypdf import PdfWriter


class ResearchCompanionCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.temp.name) / "test.db")
        self.project = self.store.create_project({
            "name": "Test Research",
            "current_objective": "hybrid retrieval architecture",
            "current_blockers": ["embedding model undecided"],
            "active_questions": ["Can BM25 and cosine work together?"],
            "next_actions": ["run retrieval benchmark"],
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_memory_persists_with_provenance_and_searches(self):
        memory = self.store.create_memory({
            "project_id": self.project["id"], "type": "decision", "title": "SQLite is source of truth",
            "content": "Obsidian remains a human-readable projection; SQLite stores machine cognitive state.",
            "source_type": "direct_user_statement", "importance": .9, "rediscovery_cost": .9,
        })
        result = self.store.search("SQLite source of truth", self.project["id"], "decision")
        self.assertEqual(result[0]["id"], memory["id"])
        self.assertEqual(memory["source_type"], "direct_user_statement")
        self.assertIn("semantic", result[0]["score_breakdown"])

    def test_context_packet_contains_state_and_memory(self):
        self.store.create_memory({"project_id": self.project["id"], "type": "failure", "title": "Full context injection failed", "content": "Token volume grew without bound.", "rediscovery_cost": .9})
        packet = self.store.compile_context(self.project["id"], "context retrieval")
        self.assertIn("CURRENT OBJECTIVE", packet["packet"])
        self.assertIn("CURRENT BLOCKERS", packet["packet"])
        self.assertIn("Full context injection failed", packet["packet"])

    def test_forgetting_is_reversible_state_change(self):
        memory = self.store.create_memory({"project_id": self.project["id"], "type": "note", "title": "old note", "content": "low value", "importance": .05, "reuse_probability": .05, "rediscovery_cost": .05, "activation": .05})
        with self.store.connect() as db:
            db.execute("UPDATE memories SET last_accessed='2020-01-01T00:00:00+00:00' WHERE id=?", (memory["id"],))
        result = self.store.forget()
        self.assertTrue(any(x["id"] == memory["id"] for x in result["changed"]))
        self.assertEqual(self.store.memory(memory["id"])["state"], "ARCHIVED")

    def test_obsidian_projection(self):
        self.store.create_memory({"project_id": self.project["id"], "type": "question", "title": "Open question", "content": "What should be benchmarked?"})
        vault = Path(self.temp.name) / "vault"
        result = self.store.sync_obsidian(str(vault), self.project["id"])
        self.assertGreaterEqual(result["count"], 2)
        self.assertTrue((vault / "Research Companion" / "Projects" / "test-research" / "Project State.md").exists())

    def test_chat_saves_transcript_and_explicit_insight(self):
        vault = Path(self.temp.name) / "shared-vault"
        self.store.update_settings({"vault_path": str(vault), "workspace_dir": self.temp.name, "agent_command": 'python -c "print(\'test-agent\')"'})
        result = self.store.chat({"project_id": self.project["id"], "message": "/remember SQLite should remain the machine source of truth"})
        self.assertEqual(result["memory"]["type"], "note")
        self.assertTrue(Path(result["transcript_path"]).exists())
        self.assertTrue((vault / "Research Companion" / "Projects" / "test-research" / "Project State.md").exists())
        conversation = self.store.conversation(result["conversation_id"])
        self.assertEqual([m["role"] for m in conversation["messages"]], ["user", "assistant"])

    def test_configured_agent_runs_in_selected_directory(self):
        output, code = self.store._agent_command('python -c "print(\'agent-ok\')"', 'test prompt', self.temp.name, 30)
        self.assertEqual(code, 0)
        self.assertIn("agent-ok", output)

    def test_agent_stdin_is_utf8(self):
        command = 'python -c "import sys; print(sys.stdin.buffer.read().hex())"'
        output, code = self.store._agent_command(command, "こんにちは、研究 companion", self.temp.name, 30)
        self.assertEqual(code, 0)
        self.assertIn("e38193e38293e381abe381a1e381afe38081e7a094e7a9b620636f6d70616e696f6e", output)

    def test_default_agent_and_conversation_deletion(self):
        self.assertEqual(self.store.settings()["agent_command"], "codex exec --skip-git-repo-check --model gpt-5.6-luna -c model_reasoning_effort=low")
        vault = Path(self.temp.name) / "vault"
        self.store.update_settings({"vault_path": str(vault), "workspace_dir": self.temp.name, "agent_command": 'python -c "print(\'test-agent\')"'})
        result = self.store.chat({"project_id": self.project["id"], "message": "/remember delete me"})
        conversation_id = result["conversation_id"]
        transcript = Path(result["transcript_path"])
        self.assertTrue(transcript.exists())
        self.assertTrue(self.store.delete_conversation(conversation_id))
        self.assertIsNone(self.store.conversation(conversation_id))
        self.assertFalse(transcript.exists())
        self.assertFalse(self.store.delete_conversation(conversation_id))

    def test_full_vault_sync_removes_unchanged_stale_projection(self):
        vault = Path(self.temp.name) / "manifest-vault"
        memory = self.store.create_memory({"project_id": self.project["id"], "type": "note", "title": "Old title", "content": "old"})
        self.store.sync_obsidian(str(vault))
        old_file = next((vault / "Research Companion" / "Projects" / "test-research" / "Memory" / "note").glob(f"{memory['id']}-*.md"))
        with self.store.connect() as db:
            db.execute("UPDATE memories SET title=? WHERE id=?", ("New title", memory["id"]))
        self.store.sync_obsidian(str(vault))
        self.assertFalse(old_file.exists())
        self.assertTrue(any(path.name.startswith(memory["id"] + "-") for path in (vault / "Research Companion" / "Projects" / "test-research" / "Memory" / "note").glob("*.md")))

    def test_vault_sync_preserves_hand_edited_projection(self):
        vault = Path(self.temp.name) / "edited-vault"
        memory = self.store.create_memory({"project_id": self.project["id"], "type": "note", "title": "Stable title", "content": "original"})
        self.store.sync_obsidian(str(vault))
        generated = next((vault / "Research Companion" / "Projects" / "test-research" / "Memory" / "note").glob(f"{memory['id']}-*.md"))
        generated.write_text("# My hand-edited note\n", encoding="utf-8")
        with self.store.connect() as db:
            db.execute("UPDATE memories SET content=? WHERE id=?", ("new source content", memory["id"]))
        result = self.store.sync_obsidian(str(vault))
        self.assertIn(str(generated), result["conflicts"])
        self.assertEqual(generated.read_text(encoding="utf-8"), "# My hand-edited note\n")
        self.assertTrue((generated.parent / f"{generated.stem} (Research Companion update).md").exists())

    def test_imports_only_tagged_user_note(self):
        vault = Path(self.temp.name) / "import-vault"
        notes = vault / "Literature"
        notes.mkdir(parents=True)
        tagged = notes / "paper.md"
        tagged.write_text("---\ntags: [research-companion, evidence]\ntype: evidence\n---\n# A tagged paper\n\nObserved result.", encoding="utf-8")
        (notes / "private.md").write_text("# Private note\n\nDo not import", encoding="utf-8")
        result = self.store.import_obsidian(str(vault), self.project["id"])
        self.assertEqual(result["count"], 1)
        imported = self.store.memory(result["imported"][0])
        self.assertEqual(imported["type"], "evidence")
        self.assertEqual(imported["source_type"], "obsidian_note")
        self.assertEqual(self.store.import_obsidian(str(vault), self.project["id"])["count"], 0)

    def test_daily_job_uses_daily_interval(self):
        self.store.run_job("daily_research_review")
        job = next(job for job in self.store.jobs() if job["name"] == "daily_research_review")
        seconds = datetime.fromisoformat(job["next_run"]).replace(tzinfo=timezone.utc).timestamp() - time.time()
        self.assertGreater(seconds, 23 * 60 * 60)

    def test_agent_can_be_cancelled(self):
        result = {}
        run_id = "run_test_cancel"

        def run_agent():
            result["value"] = self.store._agent_command_with_id(
                'python -c "import time; time.sleep(30)"', "test prompt", self.temp.name, 60, run_id
            )

        worker = threading.Thread(target=run_agent)
        worker.start()
        for _ in range(30):
            if self.store.cancel_agent(run_id):
                break
            time.sleep(.1)
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertIn(result["value"][1], (1, 130, -9))

    def test_agent_run_status_keeps_output(self):
        output, code = self.store._agent_command_with_id(
            'python -c "print(\'stream-ok\')"', "test prompt", self.temp.name, 30, "run_stream"
        )
        status = self.store.run_status("run_stream")
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "finished")
        self.assertIn("stream-ok", status["output"])

    def test_successful_agent_stderr_is_not_chat_content(self):
        output, code = self.store._agent_command_with_id(
            'python -c "import sys; print(\'reply\'); print(\'internal diagnostic\', file=sys.stderr)"',
            "test prompt", self.temp.name, 30, "run_diagnostics"
        )
        self.assertEqual(code, 0)
        self.assertEqual(output, "reply")

    def test_agent_decides_when_to_save_durable_memory(self):
        vault = Path(self.temp.name) / "agent-memory-vault"
        marker = '[[memory]]{"type":"finding","title":"Encoding boundary","content":"All imported notes must be UTF-8.","importance":0.92,"confidence":0.88,"reuse_probability":0.9,"rediscovery_cost":0.8,"reason":"Prevents a recurring import failure."}[[/memory]]'
        encoded = base64.b64encode(f"Durable answer\n{marker}".encode()).decode()
        command = f'python -c "import sys,base64; print(base64.b64decode(sys.argv[1]).decode())" {encoded}'
        self.store.update_settings({"vault_path": str(vault), "workspace_dir": self.temp.name, "agent_command": command})
        result = self.store.chat({"project_id": self.project["id"], "message": "We found a rule that should survive this session."})
        self.assertEqual(result["message"]["exit_code"], 0)
        self.assertEqual(result["memory"]["source_type"], "agent_decided")
        self.assertEqual(result["memory"]["type"], "finding")
        self.assertAlmostEqual(result["memory"]["importance"], 0.92)
        self.assertNotIn("[[memory]]", result["message"]["content"])
        conversation = self.store.conversation(result["conversation_id"])
        self.assertNotIn("[[memory]]", conversation["messages"][-1]["content"])

    def test_legacy_success_message_stderr_is_migrated_away(self):
        conversation = self.store.create_conversation({"project_id": self.project["id"], "title": "legacy"})
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO chat_messages(id,conversation_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                ("legacy_message", conversation["id"], "assistant", "reply\n\n[stderr]\nworkdir: private\ntokens used\n123", '{"exit_code": 0}', "2026-01-01T00:00:00+00:00"),
            )
        migrated = ResearchStore(self.store.db_path).conversation(conversation["id"])
        self.assertEqual(migrated["messages"][0]["content"], "reply")

    def test_slash_commands_update_state_without_running_agent(self):
        vault = Path(self.temp.name) / "command-vault"
        self.store.update_settings({"vault_path": str(vault), "workspace_dir": self.temp.name, "agent_command": 'python -c "raise SystemExit(99)"'})
        result = self.store.chat({"project_id": self.project["id"], "message": "/decision Use a small benchmark | Compare recall@k before changing the embedding model"})
        self.assertEqual(result["message"]["exit_code"], 0)
        self.assertEqual(result["memory"]["type"], "decision")
        self.assertIn("Decisionとして知識ベースに保存しました", result["message"]["content"])
        self.assertIn("Research State", (vault / "Research Companion" / "Projects" / "test-research" / "Project Overview.md").read_text(encoding="utf-8"))

        objective = self.store.chat({"project_id": self.project["id"], "message": "/objective Validate the retrieval loop"})
        self.assertEqual(objective["message"]["exit_code"], 0)
        self.assertEqual(self.store.project(self.project["id"])["current_objective"], "Validate the retrieval loop")

    def test_obsidian_projection_is_navigable_knowledge_base(self):
        vault = Path(self.temp.name) / "knowledge-vault"
        first = self.store.create_memory({"project_id": self.project["id"], "type": "decision", "title": "Choose BM25", "content": "Use BM25 as the lexical baseline."})
        second = self.store.create_memory({"project_id": self.project["id"], "type": "finding", "title": "Baseline is fast", "content": "The baseline is fast enough for local use.", "related_memory_ids": [first["id"]], "relation": "SUPPORTS"})
        result = self.store.sync_obsidian(str(vault))
        home = vault / "Research Companion" / "Home.md"
        overview = vault / "Research Companion" / "Projects" / "test-research" / "Project Overview.md"
        index = vault / "Research Companion" / "Projects" / "test-research" / "Memory Index.md"
        second_note = next((vault / "Research Companion" / "Projects" / "test-research" / "Memory" / "finding").glob(f"{second['id']}-*.md"))
        self.assertGreater(result["count"], 5)
        self.assertIn("Projects", home.read_text(encoding="utf-8"))
        self.assertIn("Memory Index", overview.read_text(encoding="utf-8"))
        self.assertIn("Choose BM25", index.read_text(encoding="utf-8"))
        self.assertIn("SUPPORTS", second_note.read_text(encoding="utf-8"))

    def test_memory_can_be_updated_and_deleted(self):
        memory = self.store.create_memory({"project_id": self.project["id"], "type": "note", "title": "Editable", "content": "old"})
        updated = self.store.update_memory(memory["id"], {"title": "Updated", "content": "new", "state": "HOT"})
        self.assertEqual(updated["title"], "Updated")
        self.assertEqual(updated["state"], "HOT")
        self.assertTrue(self.store.delete_memory(memory["id"]))
        self.assertIsNone(self.store.memory(memory["id"]))

    def test_project_delete_removes_children_and_keeps_last_project(self):
        other = self.store.create_project({"name": "Temporary"})
        self.store.create_memory({"project_id": other["id"], "type": "note", "title": "child", "content": "remove"})
        conversation = self.store.create_conversation({"project_id": other["id"], "title": "child chat"})
        result = self.store.delete_project(other["id"])
        self.assertEqual(result["memories"], 1)
        self.assertEqual(result["conversations"], 1)
        self.assertIsNone(self.store.project(other["id"]))
        self.assertIsNone(self.store.conversation(conversation["id"]))
        self.assertIsNotNone(self.store.delete_project(self.project["id"]))
        remaining = self.store.projects()[0]
        with self.assertRaises(ValueError):
            self.store.delete_project(remaining["id"])

    def test_pdf_library_extracts_renders_and_deduplicates(self):
        source = Path(self.temp.name) / "paper.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        with source.open("wb") as stream:
            writer.write(stream)
        vault = Path(self.temp.name) / "paper-vault"
        self.store.update_settings({"vault_path": str(vault)})
        imported = self.store.import_pdf({"project_id": self.project["id"], "path": str(source), "discipline": "Test field", "ocr": False})
        self.assertFalse(imported["duplicate"])
        document = imported["document"]
        self.assertEqual(document["page_count"], 1)
        self.assertTrue(Path(document["stored_path"]).exists())
        self.assertEqual(len(self.store.pdfs(self.project["id"])), 1)
        self.assertGreater(len(__import__("pdf_library").render_page(Path(document["stored_path"]), 1)), 100)
        duplicate = self.store.import_pdf({"project_id": self.project["id"], "path": str(source), "ocr": False})
        self.assertTrue(duplicate["duplicate"])
        old_path = Path(document["stored_path"])
        new_vault = Path(self.temp.name) / "migrated-paper-vault"
        self.store.update_settings({"vault_path": str(new_vault)})
        migrated = self.store.pdf(document["id"])
        self.assertNotEqual(migrated["stored_path"], str(old_path))
        self.assertTrue(Path(migrated["stored_path"]).exists())
        self.assertFalse(old_path.exists())
        self.assertTrue(self.store.delete_pdf(document["id"]))
        self.assertFalse(Path(migrated["stored_path"]).exists())


if __name__ == "__main__":
    unittest.main()
