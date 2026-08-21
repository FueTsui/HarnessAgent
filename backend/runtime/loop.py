"""动态 Agent Loop 状态。

这里刻意不定义预制节点、边或阶段顺序。循环只记录已经发生的事实，下一步始终由
模型结合最新上下文和工具观察动态决定；外围策略只负责预算、权限和完成门禁。
"""
from dataclasses import dataclass, field


AGENT_LOOP_VERSION = "2.0"


@dataclass
class AgentLoopState:
    """一次 Turn 的最小、可持久化执行状态。"""

    version: str = AGENT_LOOP_VERSION
    status: str = "running"
    iteration: int = 0
    model_calls: int = 0
    successful_tools: int = 0
    successful_tool_names: set[str] = field(default_factory=set)
    required_evidence_tools: tuple[str, ...] = ()
    verification_issues: list[str] = field(default_factory=list)
    revision_count: int = 0
    memory_candidates: int = 0
    memory_selected: int = 0
    memory_max_score: float = 0.0
    memory_influence: float = 0.0
    stop_reason: str = ""

    def checkpoint(self) -> dict:
        return {
            "loop_version": self.version,
            "status": self.status,
            "iteration": self.iteration,
            "model_calls": self.model_calls,
            "successful_tools": self.successful_tools,
            "successful_tool_names": sorted(self.successful_tool_names),
            "required_evidence_tools": list(self.required_evidence_tools),
            "verification_issues": list(self.verification_issues),
            "revision_count": self.revision_count,
            "memory_candidates": self.memory_candidates,
            "memory_selected": self.memory_selected,
            "memory_max_score": self.memory_max_score,
            "memory_influence": self.memory_influence,
            "stop_reason": self.stop_reason,
        }
