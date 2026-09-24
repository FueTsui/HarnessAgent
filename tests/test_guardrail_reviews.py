import unittest, asyncio, json
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.database import Base, get_db
from backend.models import User
from backend.security import get_current_user
from backend.api.guardrails import router
from backend import guardrail_reviews as reviews
from backend.guardrail_models import GuardrailReview

class GuardrailReviewTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            db.add_all([User(id=i, username=str(i), password_hash='x', role=role, permissions=permissions, is_active=active) for i,role,permissions,active in [(1,'root','',True),(2,'admin','["guardrails"]',True),(3,'admin','[]',True),(4,'user','["guardrails"]',True),(5,'admin','["guardrails"]',False)]])
            db.commit()
            self.users = {u.id:u for u in db.query(User).all()}
        self.user = self.users[2]
        app = FastAPI(); app.include_router(router)
        def session():
            with self.sessions() as db: yield db
        app.dependency_overrides[get_db] = session
        app.dependency_overrides[get_current_user] = lambda:self.user
        self.client = TestClient(app)
        self.patch = patch.object(reviews,'SessionLocal',self.sessions); self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.client.close(); self.engine.dispose()

    def test_permissions_and_atomic_decision(self):
        rid = reviews.create_review(4, None, {'point':'user_input','matches':[]})
        for uid in [3,4,5]:
            self.user=self.users[uid]
            self.assertEqual(self.client.get('/api/v1/guardrails/reviews').status_code,403)
            self.assertEqual(self.client.post(f'/api/v1/guardrails/reviews/{rid}/decision',json={'decision':'approved'}).status_code,403)
        for uid in [1,2]:
            self.user=self.users[uid]
            self.assertEqual(len(self.client.get('/api/v1/guardrails/reviews').json()['items']),1)
        self.assertEqual(self.client.post(f'/api/v1/guardrails/reviews/{rid}/decision',json={'decision':'approved'}).status_code,200)
        self.assertEqual(self.client.post(f'/api/v1/guardrails/reviews/{rid}/decision',json={'decision':'rejected'}).status_code,409)
        with self.sessions() as db:
            self.assertEqual(db.get(GuardrailReview,rid).reviewed_by,2)

    def test_live_approval_rejection_timeout_and_cancel(self):
        async def scenario(decision):
            token=reviews.review_context.set(True)
            try:
                async def event(name,data):
                    if name != 'guardrail.awaiting_review': return
                    response=self.client.post(f'/api/v1/guardrails/reviews/{data["review_id"]}/decision',json={'decision':decision})
                    self.assertEqual(response.status_code,200)
                return await reviews.request_review(4,None,{'point':'user_input'},event)
            finally: reviews.review_context.reset(token)
        self.assertTrue(asyncio.run(scenario('approved')))
        self.assertFalse(asyncio.run(scenario('rejected')))
        async def timeout_cancel():
            token=reviews.review_context.set(True)
            try:
                with patch.object(reviews,'WAIT_SECONDS',0):
                    self.assertFalse(await reviews.request_review(4,None,{}))
                async def cancel(name,data): raise asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await reviews.request_review(4,None,{},cancel)
            finally: reviews.review_context.reset(token)
        asyncio.run(timeout_cancel())
        with self.sessions() as db:
            self.assertEqual(db.query(GuardrailReview).filter_by(status='pending').count(),0)

    def test_content_waits_without_leaking_and_approval_is_not_reused(self):
        from backend.guardrail_policies import _enforce_snapshot, ContentBlocked
        policy={'name':'Privacy','rules':[{'enabled':True,'points':['user_input'],'detector':'pii','pii_types':['email'],'action':'block'}]}
        async def run():
            token=reviews.review_context.set(True)
            try:
                async def event(name,data):
                    if name!='guardrail.awaiting_review': return
                    payload=self.client.get('/api/v1/guardrails/reviews').json()
                    self.assertNotIn('private@example.com',json.dumps(payload))
                    self.client.post(f'/api/v1/guardrails/reviews/{data["review_id"]}/decision',json={'decision':'approved'})
                result=await _enforce_snapshot([policy],{},'user_input','private@example.com',user_id=4,runtime_event=event)
                self.assertEqual(result['decision'],'approved')
                with patch.object(reviews,'WAIT_SECONDS',0):
                    with self.assertRaises(ContentBlocked):
                        await _enforce_snapshot([policy],{},'user_input','private@example.com',user_id=4)
            finally: reviews.review_context.reset(token)
        asyncio.run(run())

    def test_tool_gate_preserves_baseline_permissions_and_rechecks_reviewer(self):
        from backend import guardrails
        async def run():
            token=reviews.review_context.set(True)
            try:
                async def event(name,data):
                    if name != 'guardrail.awaiting_review': return
                    self.client.post(f'/api/v1/guardrails/reviews/{data["review_id"]}/decision',json={'decision':'approved'})
                decision=guardrails.evaluate_tool(guardrails.GuardrailConfig(blocked_tools=['write']),kind='builtin',tool_name='write',arguments={},mutating=True,policy='ask')
                result=await reviews.review_tool(decision,4,None,event)
                self.assertTrue(result['allowed'])
                self.assertTrue(result['requires_approval'])
                self.assertFalse(result['guardrail_requires_approval'])
                rid=reviews.create_review(4,None,{})
                self.client.post(f'/api/v1/guardrails/reviews/{rid}/decision',json={'decision':'approved'})
                with self.sessions() as db:
                    db.get(User,2).permissions='[]'; db.commit()
                self.assertEqual(reviews.state(rid),'rejected')
            finally: reviews.review_context.reset(token)
        asyncio.run(run())

if __name__=='__main__': unittest.main()
