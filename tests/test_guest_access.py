"""Retired visitor endpoints and login-required chat contracts."""
import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend import jobs, main, worker
from backend.api import chat
from backend.config import settings
from backend.database import Base, get_db
from backend.models import Agent, AuthSession, Job, ModelProvider, User
from backend.security import create_browser_session, create_token, hash_password

class LoginRequiredTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('fixture-password')

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory.begin() as db:
            member = User(username='member', password_hash=self.password_hash, role='user')
            legacy = User(username='legacy', password_hash=self.password_hash, role='user')
            db.add_all([member, legacy]); db.flush()
            self.member_id, self.guest_id = member.id, legacy.id
            self.member_cookie = create_browser_session(db, member)
            self.guest_cookie = create_browser_session(db, legacy)
            legacy.role = 'guest'
            self.guest_jwt = create_token(legacy)
            provider = ModelProvider(name='platform', model_id='fixture-model', enabled=True, is_public=True)
            db.add(provider); db.flush()
            agent = Agent(name='default', enabled=True, is_default=True, is_public=True, provider_id=provider.id)
            db.add(agent); db.flush()
            self.agent_id = agent.id
        def database():
            with self.factory() as db:
                yield db
        main.app.dependency_overrides[get_db] = database
        self.addCleanup(main.app.dependency_overrides.clear)
        for item in [patch.object(main, 'SessionLocal', self.factory), patch.object(chat, 'SessionLocal', self.factory),
                     patch.object(main, '_write_audit_log_safely'), patch.object(main, '_docs_branding', return_value=('Harness', ''))]:
            item.start(); self.addCleanup(item.stop)
        self.client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.engine.dispose)

    def test_removed_routes_and_assets_are_unavailable_even_when_logged_in(self):
        for cookie in (None, self.member_cookie):
            self.client.cookies.clear()
            if cookie:
                self.client.cookies.set(settings.AUTH_COOKIE_NAME, cookie)
            for method, path in [('POST', '/api/v1/auth/guest'), ('GET', '/models'),
                                 ('GET', '/static/personal-models.js'), ('GET', '/api/v1/personal-models'),
                                 ('POST', '/api/v1/personal-models'), ('GET', '/api/v1/personal-models/1'),
                                 ('PATCH', '/api/v1/personal-models/1'), ('DELETE', '/api/v1/personal-models/1')]:
                with self.subTest(cookie=bool(cookie), path=path, method=method):
                    self.assertEqual(self.client.request(method, path, json={}).status_code, 404)
        with self.factory() as db:
            self.assertEqual(db.query(User).count(), 2)

    def test_no_identity_and_old_guest_credentials_cannot_chat_or_stream(self):
        for kind in ('anonymous', 'guest-cookie', 'guest-bearer', 'invalid'):
            self.client.cookies.clear()
            headers = {}
            if kind == 'guest-cookie':
                self.client.cookies.set(settings.AUTH_COOKIE_NAME, self.guest_cookie)
            if kind in ('guest-bearer', 'invalid'):
                headers = {'Authorization': 'Bearer ' + (self.guest_jwt if kind == 'guest-bearer' else 'invalid')}
            for method, path in [('GET', '/api/v1/auth/me'), ('POST', '/api/v1/chat'),
                                 ('GET', '/api/v1/chat/models'), ('GET', '/api/v1/chat/turns'),
                                 ('GET', '/api/v1/chat/turns/old/stream'), ('POST', '/api/v1/chat/turns/old/guidance'),
                                 ('POST', '/api/v1/reasoning-capabilities')]:
                with self.subTest(kind=kind, path=path):
                    self.assertEqual(self.client.request(method, path, headers=headers, json={}).status_code, 401)
            response = self.client.get('/', headers=headers)
            self.assertEqual((response.status_code, response.headers['location']), (303, '/login'))
        with self.factory() as db:
            self.assertEqual(db.query(Job).count(), 0)

    def test_member_session_preserves_workspace_catalog_and_logout(self):
        response = self.client.post('/api/v1/auth/login', json={'username': 'member', 'password': 'fixture-password'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/').status_code, 200)
        response = self.client.get('/api/v1/chat/models')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['default']['model'], 'fixture-model')
        self.assertEqual(self.client.post('/api/v1/reasoning-capabilities', json={}).status_code, 200)
        self.assertEqual(self.client.post('/api/v1/auth/logout').status_code, 204)
        self.assertEqual(self.client.post('/api/v1/chat', data={'query': 'hello'}).status_code, 401)

    def test_guest_issuance_queue_and_legacy_worker_are_blocked(self):
        with self.factory() as db:
            guest = db.get(User, self.guest_id)
            with self.assertRaises(HTTPException):
                create_browser_session(db, guest)
            with self.assertRaises(jobs.JobOwnerUnavailable):
                jobs.enqueue_in_session(db, guest.id, self.agent_id, 'chat', {})
        view = SimpleNamespace(id='legacy-job', owner_id=self.guest_id, parent_job_id=None,
            payload={'user_id': self.guest_id, 'agent_id': self.agent_id, 'inputs': {'query': 'hello'}})
        with patch('backend.database.SessionLocal', self.factory), patch.object(worker, '_delegated_agent_access', return_value=True), patch.object(chat, 'execute_chat', new_callable=AsyncMock) as execute:
            with self.assertRaises(RuntimeError):
                asyncio.run(worker._chat_handler(view))
            execute.assert_not_called()

    def test_migration_retires_only_guests_and_personal_connections(self):
        with self.factory.begin() as db:
            db.add_all([ModelProvider(name='__personal_model_fixture', created_by=self.member_id, enabled=True, is_public=True),
                        ModelProvider(name='guest-model', created_by=self.guest_id, enabled=True, is_public=True),
                        Agent(name='legacy-agent', created_by=self.guest_id, enabled=True)])
        migration = importlib.import_module('migrations.versions.0029_retire_guest_access')
        with self.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.downgrade()
        with self.factory() as db:
            self.assertEqual(db.query(User).count(), 2)
            self.assertFalse(db.get(User, self.guest_id).is_active)
            self.assertTrue(db.get(User, self.member_id).is_active)
            self.assertEqual(db.query(AuthSession).filter_by(user_id=self.guest_id).count(), 0)
            self.assertEqual(db.query(AuthSession).filter_by(user_id=self.member_id).count(), 1)
            self.assertTrue(db.query(ModelProvider).filter_by(name='platform').one().enabled)
            for name in ('__personal_model_fixture', 'guest-model'):
                row = db.query(ModelProvider).filter_by(name=name).one()
                self.assertFalse(row.enabled)
                self.assertFalse(row.is_public)
            self.assertFalse(db.query(Agent).filter_by(name='legacy-agent').one().enabled)
