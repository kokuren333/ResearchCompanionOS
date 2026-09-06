import tempfile
import unittest
from pathlib import Path

from app import ResearchStore


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
        self.assertTrue((vault / "Projects" / "test-research" / "Project State.md").exists())


if __name__ == "__main__":
    unittest.main()
