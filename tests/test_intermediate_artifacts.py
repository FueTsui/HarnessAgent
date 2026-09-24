import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import artifacts
from backend.database import Base
from backend.models import Artifact, Job, Thread, Turn, User
from backend.runtime import builtin_tools


class IntermediateArtifactTests(unittest.TestCase):
    def test_browser_links_normalize_only_owned_outputs(self):
        from backend.api.chat import _normalize_export_links
        answer = '[下载](sandbox:/api/v1/exports/%E4%B8%AD%E6%96%87.pptx) '
        answer += '[其他](sandbox:/api/v1/exports/other.pptx) [外站](sandbox:https://example.com/a)'
        result = _normalize_export_links(answer, ['中文.pptx'])
        self.assertIn('[下载](/api/v1/exports/%E4%B8%AD%E6%96%87.pptx)', result)
        self.assertIn('sandbox:/api/v1/exports/other.pptx', result)
        self.assertIn('sandbox:https://example.com/a', result)

    def test_failed_turn_keeps_valid_owned_output_without_becoming_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = create_engine('sqlite:///' + (root / 'test.db').as_posix())
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine)
            with sessions() as db:
                db.add(User(id=1, username='owner', password_hash='x', role='root'))
                db.add(Thread(id='thread', owner_id=1, title='test'))
                db.add(Turn(id='run', thread_id='thread', owner_id=1, sequence=1, status='running'))
                db.add(Job(id='run', owner_id=1, kind='chat', status='running'))
                db.commit()

            async def persist(filename):
                artifacts.register_generated(owner_id=1, run_id='run', filename=filename)

            ctx = builtin_tools.BuiltinToolContext(
                root=root, enabled_tools={'presentation_create'}, artifact_callback=persist,
            )
            with (
                patch.object(artifacts, 'SessionLocal', sessions),
                patch.object(artifacts, 'EXPORT_DIR', root),
                patch.object(builtin_tools, 'EXPORT_DIR', root),
                patch.object(builtin_tools, 'enforce_content', new=AsyncMock()),
            ):
                asyncio.run(builtin_tools.execute('presentation_create', {
                    'confirm': True, 'slides': [{'title': '中间成果', 'body': '任务尚未完成。'}],
                }, ctx))
                self.assertEqual(len(ctx.artifacts), 1)
                filename = ctx.artifacts[0]
                with sessions() as db:
                    row = db.get(Turn, 'run')
                    row.status = 'failed'
                    row.error = '计划未完成'
                    db.commit()
                artifacts.register_generated(owner_id=1, run_id='run', filename=filename)
                with self.assertRaisesRegex(ValueError, '归属'):
                    artifacts.register_generated(owner_id=2, run_id='run', filename=filename)
                with self.assertRaisesRegex(ValueError, '不存在'):
                    artifacts.register_generated(owner_id=1, run_id='run', filename='missing.pptx')
                with sessions() as db:
                    output = db.query(Artifact).one()
                    self.assertEqual((output.owner_id, output.run_id, output.turn_id), (1, 'run', 'run'))
                    self.assertEqual(db.get(Turn, 'run').status, 'failed')
                    self.assertTrue(artifacts.valid_presentation_artifact(output.filename))
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
