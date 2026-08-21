"""Knowledge-base ownership and sharing permission regressions."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.capabilities import knowledge


class KnowledgePermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(knowledge, "KNOWLEDGE_DIR", root),
            patch.object(knowledge, "DATASETS_META", root / "_datasets.json"),
            patch.object(knowledge, "DELETED_BUILTINS_META", root / "_deleted_builtins.json"),
            patch.object(knowledge, "META_LOCK_FILE", root / ".datasets.lock"),
            patch.object(knowledge, "DEFAULT_DATASETS", {}),
        ]
        for item in self.patches:
            item.start()
        self.creator = SimpleNamespace(id=11, role="admin")
        self.other = SimpleNamespace(id=12, role="admin")
        self.root = SimpleNamespace(id=1, role="root")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_only_creator_can_mutate_shared_dataset(self):
        dataset = knowledge.create_dataset("共享制度库", self.creator)
        key = dataset["key"]
        knowledge.save_document(key, "rules.txt", b"shared rules", self.creator)
        knowledge.set_dataset_public(key, True, self.creator)

        other_view = knowledge.list_datasets(self.other)
        self.assertEqual([item["key"] for item in other_view], [key])
        self.assertFalse(other_view[0]["can_manage"])

        root_view = knowledge.list_datasets(self.root)
        self.assertEqual([item["key"] for item in root_view], [key])
        self.assertFalse(root_view[0]["can_manage"])

        for user in (self.other, self.root):
            with self.assertRaises(PermissionError):
                knowledge.save_document(key, "overwrite.txt", b"denied", user)
            with self.assertRaises(PermissionError):
                knowledge.delete_document(key, "rules.txt", user)
            with self.assertRaises(PermissionError):
                knowledge.set_dataset_public(key, False, user)
            with self.assertRaises(PermissionError):
                knowledge.delete_dataset(key, user)

        self.assertTrue(knowledge.delete_document(key, "rules.txt", self.creator))
        self.assertTrue(knowledge.delete_dataset(key, self.creator))

    def test_private_dataset_is_hidden_from_regular_users_but_root_is_read_only(self):
        dataset = knowledge.create_dataset("创建者私有库", self.creator)
        self.assertEqual(knowledge.list_datasets(self.other), [])

        root_view = knowledge.list_datasets(self.root)
        self.assertEqual(len(root_view), 1)
        self.assertEqual(root_view[0]["key"], dataset["key"])
        self.assertFalse(root_view[0]["can_manage"])


if __name__ == "__main__":
    unittest.main()
