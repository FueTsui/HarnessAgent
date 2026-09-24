"""Visible model names stay consistent without changing routing or wire IDs."""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import chat as chat_api
from backend.database import Base
from backend.guest_access import PERSONAL_MODEL_PREFIX, guest_agent
from backend.models import Agent, ModelProvider, User


class ModelDisplayNameTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.owner = User(username="owner", password_hash="x", role="root", is_active=True)
        self.user = User(username="user", password_hash="x", role="user", is_active=True)
        self.guest = User(username="visitor", password_hash="x", role="guest", is_active=True)
        self.db.add_all([self.owner, self.user, self.guest])
        self.db.flush()
        self.provider = ModelProvider(
            name="Original connection", model_name="GPT-6 Astra", model_id="gpt-6-astra",
            base_url="https://example.invalid/v1", wire_api="responses", model_reasoning=True,
            enabled=True, is_public=True, created_by=self.owner.id,
        )
        self.db.add(self.provider)
        self.db.flush()
        self.agent = Agent(name="Assistant", provider_id=self.provider.id,
                           created_by=self.owner.id, enabled=True, is_public=True)
        self.db.add(self.agent)
        guest_agent(self.db, self.guest, create=True)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def catalog(self, user=None):
        user = user or self.user
        return chat_api.chat_models(None if user is self.guest else self.agent.id, user, self.db)

    def test_registered_default_and_explicit_use_model_name(self):
        catalog = self.catalog()
        self.assertEqual(catalog["default"]["name"], "智能体默认")
        self.assertIsNone(catalog["default"]["provider_id"])
        for row in [catalog["default"], *catalog["items"]]:
            self.assertEqual(row["model_name"], "GPT-6 Astra")
            self.assertEqual(row["model"], "gpt-6-astra")
        self.assertEqual(catalog["items"][0]["provider_id"], self.provider.id)
        self.assertEqual(self.provider.name, "Original connection")

    def test_guest_default_and_explicit_use_model_name_and_concrete_id(self):
        catalog = self.catalog(self.guest)
        for row in [catalog["default"], *catalog["items"]]:
            self.assertEqual(row["name"], "GPT-6 Astra")
            self.assertEqual(row["model_name"], "GPT-6 Astra")
            self.assertEqual(row["model"], "gpt-6-astra")
            self.assertEqual(row["provider_id"], self.provider.id)

    def test_legacy_names_and_whitespace_fall_back_consistently(self):
        for model_name, name, expected in [
            ("  Friendly model  ", "Legacy label", "Friendly model"),
            ("", "  Legacy label  ", "Legacy label"),
            (" \t", "Legacy label", "Legacy label"),
            ("", "  ", "gpt-6-astra"),
        ]:
            with self.subTest(model_name=model_name, name=name):
                self.provider.model_name = model_name
                self.provider.name = name
                self.db.commit()
                for user in (self.user, self.guest):
                    catalog = self.catalog(user)
                    for row in [catalog["default"], *catalog["items"]]:
                        self.assertEqual(row["model_name"], expected)
                        self.assertEqual(row["model"], "gpt-6-astra")

    def test_personal_internal_name_is_never_a_display_fallback(self):
        self.provider.name = PERSONAL_MODEL_PREFIX + "private-identifier"
        self.provider.model_name = "  "
        self.db.commit()
        for user in (self.user, self.guest):
            catalog = self.catalog(user)
            self.assertNotIn(PERSONAL_MODEL_PREFIX, json.dumps(catalog))
            for row in [catalog["default"], *catalog["items"]]:
                self.assertEqual(row["model_name"], "gpt-6-astra")

    def test_environment_default_uses_real_id_only_for_registered_user(self):
        self.agent.provider_id = None
        self.provider.is_public = False
        self.db.commit()
        with patch.object(chat_api.settings, "LLM_TEXT_MODEL", "environment-model-id"):
            registered = self.catalog()
            self.assertEqual(registered["default"]["model_name"], "environment-model-id")
            self.assertEqual(registered["default"]["model"], "environment-model-id")
            self.assertIsNone(registered["default"]["provider_id"])
            guest = self.catalog(self.guest)
        self.assertFalse(guest["available"])
        self.assertEqual(guest["default"]["model_name"], "")
        self.assertEqual(guest["default"]["model"], "")
        self.assertEqual(guest["default"]["name"], "暂无可用模型")
        self.assertEqual(guest["items"], [])
        self.assertNotIn("environment-model-id", json.dumps(guest))

    def test_name_change_preserves_permissions_reasoning_and_wire_model(self):
        before = self.catalog()
        self.provider.model_name = "自定义友好名称"
        private = ModelProvider(name="Secret", model_name="Private label", model_id="private-id",
                                enabled=True, is_public=False, created_by=self.owner.id)
        self.db.add(private)
        self.db.commit()
        after = self.catalog()
        self.assertEqual([row["provider_id"] for row in after["items"]], [self.provider.id])
        for old, new in zip([before["default"], *before["items"]],
                            [after["default"], *after["items"]]):
            self.assertEqual(new["model_name"], "自定义友好名称")
            self.assertEqual(new["model"], old["model"])
            self.assertEqual(new["reasoning_efforts"], old["reasoning_efforts"])
            self.assertEqual(new["reasoning_supported"], old["reasoning_supported"])
        with self.assertRaises(ValueError):
            chat_api.select_chat_provider(self.db, self.user, self.agent, private.id)
        selected = chat_api.select_chat_provider(self.db, self.user, self.agent, self.provider.id)
        snapshot = chat_api._provider_snapshot(selected)
        client = chat_api._client_from_provider_snapshot(snapshot)
        payload = client._responses_payload([{"role": "user", "content": "hello"}])
        self.assertEqual(snapshot["model_id"], "gpt-6-astra")
        self.assertEqual(payload["model"], "gpt-6-astra")


if __name__ == "__main__":
    unittest.main()
