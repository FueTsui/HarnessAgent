import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.api.providers import (
    _to_out,
    delete_provider,
    discover_models_for_provider,
)
from backend.database import Base
from backend.llm.client import LLMClient
from backend.models import Agent, ModelProvider, ROLE_ROOT, TokenUsage, User
from backend.schemas import ProviderCreate, ProviderProbe, ProviderUpdate
from pydantic import ValidationError


class ProviderDeletionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.root = User(username="root", password_hash="x", role=ROLE_ROOT, is_active=True)
        self.db.add(self.root)
        self.db.commit()
        self.db.refresh(self.root)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _provider(self, name="test-provider"):
        provider = ModelProvider(name=name, created_by=self.root.id)
        self.db.add(provider)
        self.db.commit()
        self.db.refresh(provider)
        return provider

    def test_delete_preserves_usage_and_clears_provider_reference(self):
        provider = self._provider()
        provider_id = provider.id
        usage = TokenUsage(
            user_id=self.root.id,
            provider_id=provider_id,
            model="historical-model",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        )
        self.db.add(usage)
        self.db.commit()
        usage_id = usage.id

        delete_provider(provider_id, self.root, self.db)

        self.assertIsNone(self.db.get(ModelProvider, provider_id))
        retained_usage = self.db.get(TokenUsage, usage_id)
        self.assertIsNotNone(retained_usage)
        self.assertIsNone(retained_usage.provider_id)
        self.assertEqual(retained_usage.model, "historical-model")

    def test_delete_rejects_provider_bound_to_agent(self):
        provider = self._provider()
        agent = Agent(name="bound-agent", provider_id=provider.id, created_by=self.root.id)
        self.db.add(agent)
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            delete_provider(provider.id, self.root, self.db)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIsNotNone(self.db.get(ModelProvider, provider.id))

    def test_saved_provider_discovery_keeps_provider_id(self):
        provider = self._provider()
        provider.base_url = "https://api.example.com/v1"
        provider.api_key = "stored-secret"
        self.db.commit()
        body = ProviderProbe(
            provider_type="nvidia",
            base_url=provider.base_url,
            api_key="",
            model_id="nvidia/test-model",
        )

        async def fake_list_models(client):
            self.assertEqual(client.provider_id, provider.id)
            return ["nvidia/test-model"]

        async def run():
            with patch.object(LLMClient, "list_models", fake_list_models):
                return await discover_models_for_provider(
                    provider.id, body, self.root, self.db
                )

        self.assertEqual(
            asyncio.run(run()),
            {"models": ["nvidia/test-model"], "source": "live"},
        )

    def test_provider_output_exposes_general_model_metadata_without_secret(self):
        provider = self._provider("general-model-provider")
        provider.api_key = "stored-secret"
        provider.model_id = "vendor/model-v2"
        provider.model_name = "Model V2"
        provider.model_reasoning = True
        provider.model_input = '["text", "image"]'
        provider.context_window = 200000
        provider.max_tokens = 8192
        self.db.commit()

        output = _to_out(provider, self.root)

        self.assertEqual(output.model_id, "vendor/model-v2")
        self.assertEqual(output.model_name, "Model V2")
        self.assertTrue(output.model_reasoning)
        self.assertEqual(output.model_input, ["text", "image"])
        self.assertEqual(output.context_window, 200000)
        self.assertEqual(output.max_tokens, 8192)
        self.assertTrue(output.has_key)
        self.assertFalse(hasattr(output, "api_key"))

    def test_legacy_model_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderCreate(
                name="legacy",
                base_url="https://api.example.com/v1",
                model_id="new-model",
                text_model="old-text-model",
            )
        with self.assertRaises(ValidationError):
            ProviderUpdate(vision_model="old-vision-model")

    def test_model_input_uses_explicit_universal_modalities(self):
        body = ProviderCreate(
            name="multimodal",
            base_url="https://api.example.com/v1",
            model_id="model-v2",
            model_input=["text", "image"],
        )
        self.assertEqual(body.model_input, ["text", "image"])

        with self.assertRaises(ValidationError):
            ProviderCreate(
                name="image-only",
                base_url="https://api.example.com/v1",
                model_id="model-v2",
                model_input=["image"],
            )
        with self.assertRaises(ValidationError):
            ProviderUpdate(model_input=["text", "text"])


if __name__ == "__main__":
    unittest.main()
