"""启动脚本。

  python run.py            启动 API 服务（默认含进程内任务 worker）
  python run.py worker     仅启动独立任务 worker 进程（多副本部署时，API 进程设 JOB_WORKER_ENABLED=false）
"""
import sys

from backend.config import settings

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        from backend.worker import run_standalone
        run_standalone()
    else:
        import uvicorn
        uvicorn.run("backend.main:app", host=settings.HOST, port=settings.PORT, reload=False)
