"""Legacy removal must preserve custom knowledge and survive restart safely."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.capabilities import knowledge
from backend.knowledge_retirement import MARKER_NAME, _guard, retire_legacy_datasets


class KnowledgeRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "knowledge"
        self.root.mkdir()
        for name, value in {
            "KNOWLEDGE_DIR": self.root, "DATASETS_META": self.root / "_datasets.json",
            "META_LOCK_FILE": self.root / ".datasets.lock",
        }.items():
            item = patch.object(knowledge, name, value)
            item.start()
            self.addCleanup(item.stop)

    def seed(self, value):
        knowledge.DATASETS_META.write_text(json.dumps(value), encoding="utf-8")
        for key in value:
            folder = self.root / key
            folder.mkdir(exist_ok=True)
            (folder / "document.txt").write_text(f"{key} original content", encoding="utf-8")

    def test_removes_documents_registry_cache_and_project_references_preserves_other_data(self):
        law = {"name": "法律", "created_by": 11, "builtin": False, "is_public": True}
        self.seed({"green": {"name": "old green", "builtin": True},
                   "vpp": {"name": "old vpp", "builtin": True}, "law": law})
        before = (self.root / "law/document.txt").read_bytes()
        (self.root / "_deleted_builtins.json").write_text('["green", "vpp"]')
        knowledge._chunk_cache[str(self.root / "green/document.txt")] = (0, 0, [])
        project = SimpleNamespace(dataset_ids='["green", "law", "vpp"]')
        class Database:
            committed = False
            def query(self, _model): return self
            def all(self): return [project]
            def commit(self): self.committed = True
        db = Database()
        result = retire_legacy_datasets(db)
        self.assertEqual(result["removed"], ["green", "vpp"])
        self.assertEqual(result["projects_updated"], 1)
        self.assertTrue(db.committed)
        self.assertEqual(json.loads(project.dataset_ids), ["law"])
        self.assertEqual(json.loads(knowledge.DATASETS_META.read_text()), {"law": law})
        self.assertEqual((self.root / "law/document.txt").read_bytes(), before)
        self.assertFalse((self.root / "green").exists())
        self.assertFalse((self.root / "vpp").exists())
        self.assertFalse((self.root / "_deleted_builtins.json").exists())
        self.assertEqual(knowledge._chunk_cache, {})
        self.assertFalse(retire_legacy_datasets()["changed"])
        self.assertEqual(set(knowledge._load_datasets()), {"law"})
        self.assertFalse((self.root / "green").exists())

    def test_new_install_has_no_predefined_knowledge(self):
        retire_legacy_datasets()
        self.assertEqual(knowledge._load_datasets(), {})
        self.assertFalse((self.root / "green").exists())
        self.assertFalse((self.root / "vpp").exists())

    def test_custom_owner_can_reuse_historical_key_without_deletion(self):
        self.seed({"green": {"name": "用户知识", "created_by": 3, "builtin": True}})
        retire_legacy_datasets()
        self.assertTrue((self.root / "green/document.txt").exists())
        self.assertIn("green", knowledge._load_datasets())

    def test_unregistered_legacy_folder_is_removed(self):
        folder = self.root / "vpp"
        folder.mkdir()
        (folder / "old.txt").write_text("old document")
        retire_legacy_datasets()
        self.assertFalse(folder.exists())

    def test_bad_registry_aborts_without_touching_documents(self):
        self.seed({"green": {"builtin": True}})
        knowledge.DATASETS_META.write_text('{bad json')
        with self.assertRaises(ValueError): retire_legacy_datasets()
        self.assertTrue((self.root / "green/document.txt").exists())
        self.assertFalse((self.root / MARKER_NAME).exists())

    def test_linked_directory_aborts_before_any_delete(self):
        self.seed({"green": {"builtin": True}, "vpp": {"builtin": True}})
        with patch.object(Path, "is_junction", lambda p: p.name == "vpp"):
            with self.assertRaises(RuntimeError): retire_legacy_datasets()
        self.assertTrue((self.root / "green/document.txt").exists())
        self.assertFalse((self.root / MARKER_NAME).exists())

    def test_outside_root_and_root_itself_are_rejected(self):
        for path in (self.root.parent, self.root, self.root.parent / "other"):
            with self.assertRaises(RuntimeError): _guard(path, self.root)
