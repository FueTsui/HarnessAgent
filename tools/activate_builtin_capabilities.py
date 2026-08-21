"""一次性启用完整内置 Harness 能力。

用法：
    .\\.venv\\Scripts\\python.exe tools\\activate_builtin_capabilities.py

脚本幂等：重复执行不会重复创建 Skill、审查子智能体或 Harness 版本。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import harness
from backend.database import SessionLocal
from backend.models import Agent, HarnessVersion, McpServer, Skill, User
from backend.runtime import builtin_tools


CHANGE_SUMMARY = "启用完整内置 Harness 能力"
SKILL_NAME = "workspace-engineering"
REVIEWER_NAME = "代码审查子智能体"


def _ids(value: str) -> list[int]:
    try:
        rows = json.loads(value or "[]")
        return [int(row) for row in rows if str(row).isdigit()]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def activate() -> dict:
    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.role == "root").order_by(User.id).first()
        tavily_ids = {
            item.id
            for item in db.query(McpServer).filter(
                McpServer.name == "Tavily 联网搜索"
            ).all()
        }
        for item in db.query(McpServer).filter(McpServer.id.in_(tavily_ids)).all():
            item.enabled = False
        if tavily_ids:
            for configured_agent in db.query(Agent).all():
                configured_agent.mcp_ids = json.dumps([
                    value for value in _ids(configured_agent.mcp_ids)
                    if value not in tavily_ids
                ], ensure_ascii=False)
        agent = (
            db.query(Agent)
            .filter(Agent.is_default.is_(True))
            .order_by(Agent.id)
            .first()
            or db.query(Agent).filter(Agent.enabled.is_(True)).order_by(Agent.id).first()
        )
        if agent is None:
            raise RuntimeError("没有可配置的智能体")

        skill = db.query(Skill).filter(Skill.name == SKILL_NAME).first()
        if skill is None:
            skill = Skill(
                name=SKILL_NAME,
                description=(
                    "处理工作区代码、文件修改、命令验证、Git 检查、浏览器测试、"
                    "HTML/图片产物、Cron 或子智能体任务时使用。"
                ),
                instructions=(
                    "每次只执行当前最有价值的一步。先用 ls/glob/grep/read 获取证据；"
                    "修改优先用 edit/multi_edit/apply_patch，并保持精确匹配和原子性；"
                    "修改后必须运行 lsp、测试或目标命令验证，再检查 git_diff。"
                    "不得读取工作区外路径，不得绕过 Shell 白名单。"
                    "只有用户明确要求时才执行 git_commit、Cron 删除或浏览器提交；"
                    "打开的浏览器会话完成后调用 browser_close。"
                    "子任务可用 spawn_agent，随后用 wait_agent/list_agents 检查真实终态，"
                    "不得把 pending 当作完成。"
                ),
                resources="[]",
                enabled=True,
                is_public=True,
                created_by=owner.id if owner else None,
            )
            db.add(skill)
            db.flush()

        reviewer = db.query(Agent).filter(Agent.name == REVIEWER_NAME).first()
        if reviewer is None:
            review_skill = db.query(Skill).filter(Skill.name == "review-agent").first()
            reviewer_skills = [skill.id] + ([review_skill.id] if review_skill else [])
            reviewer = Agent(
                name=REVIEWER_NAME,
                description="只读检查代码变更、测试风险与安全边界，返回可操作问题。",
                opening_statement="请给出需要审查的变更或目标。",
                enabled=True,
                provider_id=agent.provider_id,
                mcp_ids="[]",
                skill_ids=json.dumps(reviewer_skills),
                agent_ids="[]",
                builtin_tools=json.dumps([
                    name for name, tool in builtin_tools.TOOLS.items()
                    if not tool.mutating
                ], ensure_ascii=False),
                memory_enabled=False,
                routing="",
                is_public=False,
                is_default=False,
                created_by=owner.id if owner else None,
            )
            db.add(reviewer)
            db.flush()

        if not db.query(HarnessVersion).filter(
            HarnessVersion.agent_id == reviewer.id
        ).first():
            harness.create_version(
                db,
                reviewer,
                system_prompt=(
                    "你是只读代码审查子智能体。先检查目标文件、差异和验证结果；"
                    "只报告可复现、可定位、会影响正确性/安全性/维护性的缺陷。"
                    "不得修改文件、提交 Git、创建计划任务或触发外部副作用。"
                ),
                tool_policy={
                    "profile": "small_model",
                    "mode": "allow_bound",
                    "max_iterations": 8,
                    "max_parallel_calls": 1,
                    "max_successful_calls": 6,
                    "timeout_seconds": 60,
                    "denied_tools": [
                        "write", "edit", "multi_edit", "apply_patch", "shell",
                        "git_commit", "html_generate", "image_generate", "browser_type",
                        "CronCreate", "CronDelete", "CronSetEnabled", "spawn_agent",
                        "resume_agent", "interrupt_agent", "close_agent",
                    ],
                    "router": {"enabled": True, "activation_threshold": 8, "max_candidates": 6},
                },
                memory_policy={"enabled": False},
                verification_policy={"required": True, "max_revisions": 1},
                output_policy={"language": "follow_user", "max_chars": 30000},
                change_summary="创建只读代码审查子智能体",
                created_by=owner.id if owner else None,
                publish=True,
            )
        reviewer.builtin_tools = json.dumps([
            name for name, tool in builtin_tools.TOOLS.items()
            if not tool.mutating
        ], ensure_ascii=False)

        skill_ids = _ids(agent.skill_ids)
        if skill.id not in skill_ids:
            skill_ids.append(skill.id)
            agent.skill_ids = json.dumps(skill_ids)
        agent_ids = _ids(agent.agent_ids)
        if reviewer.id not in agent_ids:
            agent_ids.append(reviewer.id)
            agent.agent_ids = json.dumps(agent_ids)
        agent.builtin_tools = json.dumps(
            sorted(builtin_tools.TOOLS), ensure_ascii=False
        )
        # 升级已有 Agent：若此前已经具备内置能力，则补上免费 web_search/web_fetch。
        for configured_agent in db.query(Agent).all():
            assigned = {
                str(value)
                for value in json.loads(configured_agent.builtin_tools or "[]")
            }
            if assigned:
                assigned.update({"web_search", "web_fetch"})
                configured_agent.builtin_tools = json.dumps(
                    sorted(assigned), ensure_ascii=False
                )

        current = harness.active_version(db, agent)
        already = current is not None and current.change_summary == CHANGE_SUMMARY
        if not already:
            config = harness.as_dict(current) if current else {}
            system_prompt = config.get("system_prompt", "")
            addition = (
                "\n\n[内置能力执行规范]\n"
                "优先用文件搜索和读取建立证据，再做最小修改；修改后运行确定性诊断或测试，"
                "并用 git_diff 核对。大量工具由 Router 渐进披露，不要猜测未读取的文件内容。"
                "异步子智能体必须等待真实终态；高风险或不可逆操作需要用户明确要求。"
            )
            harness.create_version(
                db,
                agent,
                system_prompt=system_prompt + addition,
                tool_policy={
                    "profile": "small_model",
                    "mode": "allow_bound",
                    "max_iterations": 12,
                    "max_same_tool_calls": 2,
                    "max_parallel_calls": 1,
                    "max_successful_calls": 8,
                    "timeout_seconds": 60,
                    "max_output_chars": 12000,
                    "context_budget_chars": 30000,
                    "argument_repair": True,
                    "router": {
                        "enabled": True,
                        "activation_threshold": 8,
                        "max_candidates": 7,
                    },
                },
                memory_policy=config.get("memory_policy") or {"enabled": True},
                verification_policy=config.get("verification_policy") or {
                    "required": True, "max_revisions": 1
                },
                output_policy=config.get("output_policy") or {
                    "language": "follow_user", "max_chars": 30000
                },
                change_summary=CHANGE_SUMMARY,
                created_by=owner.id if owner else None,
                publish=True,
            )
        db.commit()
        return {
            "agent_id": agent.id,
            "active_version": agent.active_version,
            "skill_id": skill.id,
            "reviewer_id": reviewer.id,
            "created_version": not already,
        }
    finally:
        db.close()


if __name__ == "__main__":
    print(json.dumps(activate(), ensure_ascii=False))
