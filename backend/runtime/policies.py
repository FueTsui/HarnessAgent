"""Harness 版本策略的运行时契约。

数据库中的四类策略是面向管理平面的松散 JSON；本模块把它们收敛为有边界、
有默认值的不可变运行参数。所有数值都在这里限幅，避免错误配置绕过运行时预算。
"""
from dataclasses import dataclass
from typing import Any


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _terms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip()[:100] for item in value if str(item).strip())[:20]


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip()[:64] for item in value if str(item).strip())[:100]


@dataclass(frozen=True)
class RuntimePolicies:
    """一次 Turn 固定使用的规范化策略快照。"""

    profile: str
    tool_mode: str
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    max_iterations: int
    max_same_tool_calls: int
    max_parallel_calls: int
    max_successful_calls: int
    tool_timeout_seconds: float
    max_tool_output_chars: int
    tool_context_budget_chars: int
    tool_compact_chars: int
    argument_repair: bool
    router_enabled: bool
    router_activation_threshold: int
    router_max_candidates: int
    memory_enabled: bool
    history_recent_messages: int
    history_budget_ratio: float
    history_summary_chars: int
    memory_scope: str
    memory_top_k: int
    memory_min_relevance: float
    memory_influence: float
    memory_relevance_weight: float
    memory_recency_weight: float
    memory_max_chars: int
    memory_exclude_current_session: bool
    verification_required: bool
    verification_strict: bool
    verification_max_revisions: int
    min_answer_chars: int
    required_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    require_successful_tool: bool
    output_language: str
    output_max_chars: int
    output_concise: bool

    @classmethod
    def from_dicts(
        cls,
        tool_policy: dict | None = None,
        memory_policy: dict | None = None,
        verification_policy: dict | None = None,
        output_policy: dict | None = None,
    ) -> "RuntimePolicies":
        tool = _dict(tool_policy)
        memory = _dict(memory_policy)
        verification = _dict(verification_policy)
        output = _dict(output_policy)
        router = _dict(tool.get("router"))
        memory_scope = str(memory.get("scope") or "agent").strip().lower()
        if memory_scope not in {"agent", "user"}:
            memory_scope = "agent"

        # 本项目默认服务本地中小模型。standard 可显式放宽并行度，但仍保留确定性
        # 参数校验、失败解析和预算限幅。
        profile = str(tool.get("profile") or "small_model").strip().lower()
        small = profile in {"small", "small_model", "weak_model", "local"}
        mode = str(tool.get("mode") or "allow_bound").strip().lower()
        if mode not in {"allow_bound", "allowlist", "disabled"}:
            mode = "allow_bound"
        return cls(
            profile="small_model" if small else "standard",
            tool_mode=mode,
            allowed_tools=_names(tool.get("allowed_tools")),
            denied_tools=_names(tool.get("denied_tools")),
            max_iterations=_int(tool.get("max_iterations"), 8 if small else 6, 1, 20),
            # 小模型即使配置过宽也最多连续尝试同类工具 3 次，避免用不同参数反复 ls/grep
            # 绕过“相同签名”检测并耗尽全部成功预算。
            max_same_tool_calls=_int(
                tool.get("max_same_tool_calls"), 2 if small else 3, 1, 3 if small else 8
            ),
            max_parallel_calls=_int(tool.get("max_parallel_calls"), 1 if small else 4, 1, 12),
            max_successful_calls=_int(
                tool.get("max_successful_calls"), 4 if small else 10, 1, 30
            ),
            tool_timeout_seconds=_float(tool.get("timeout_seconds"), 45.0, 1.0, 300.0),
            max_tool_output_chars=_int(
                tool.get("max_output_chars"), 4000 if small else 8000, 500, 30000
            ),
            tool_context_budget_chars=_int(
                tool.get("context_budget_chars"), 24000, 4000, 100000
            ),
            tool_compact_chars=_int(tool.get("compact_chars"), 1200, 300, 5000),
            argument_repair=_bool(tool.get("argument_repair"), True),
            router_enabled=_bool(router.get("enabled", tool.get("router_enabled")), True),
            router_activation_threshold=_int(
                router.get("activation_threshold"), 10 if small else 16, 2, 200
            ),
            router_max_candidates=_int(
                router.get("max_candidates"), 6 if small else 10, 2, 30
            ),
            memory_enabled=_bool(memory.get("enabled"), True),
            history_recent_messages=_int(memory.get("recent_messages"), 6, 2, 20),
            history_budget_ratio=_float(memory.get("budget_ratio"), .70, .30, .90),
            history_summary_chars=_int(memory.get("summary_chars"), 5000, 500, 12000),
            memory_scope=memory_scope,
            memory_top_k=_int(memory.get("top_k"), 3, 1, 8),
            memory_min_relevance=_float(
                memory.get("min_relevance"), .12, .01, .95
            ),
            memory_influence=_float(memory.get("influence"), .35, .05, .80),
            memory_relevance_weight=_float(
                memory.get("relevance_weight"), .85, 0.0, 1.0
            ),
            memory_recency_weight=_float(
                memory.get("recency_weight"), .15, 0.0, 1.0
            ),
            memory_max_chars=_int(memory.get("max_chars"), 1800, 200, 8000),
            memory_exclude_current_session=_bool(
                memory.get("exclude_current_session"), True
            ),
            verification_required=_bool(verification.get("required"), True),
            verification_strict=_bool(verification.get("strict"), False),
            verification_max_revisions=_int(verification.get("max_revisions"), 1, 0, 2),
            min_answer_chars=_int(verification.get("min_answer_chars"), 1, 1, 2000),
            required_terms=_terms(verification.get("required_terms")),
            forbidden_terms=_terms(verification.get("forbidden_terms")),
            require_successful_tool=_bool(
                verification.get("require_successful_tool"), False
            ),
            output_language=str(output.get("language") or "follow_user").strip()[:32],
            output_max_chars=_int(output.get("max_chars"), 30000, 500, 100000),
            output_concise=_bool(output.get("concise"), False),
        )

    def public_snapshot(self) -> dict:
        """可安全写入 Item 的生效策略摘要。"""
        return {
            "profile": self.profile,
            "tool_mode": self.tool_mode,
            "max_iterations": self.max_iterations,
            "max_parallel_calls": self.max_parallel_calls,
            "max_successful_calls": self.max_successful_calls,
            "router_enabled": self.router_enabled,
            "router_max_candidates": self.router_max_candidates,
            "argument_repair": self.argument_repair,
            "memory_enabled": self.memory_enabled,
            "memory_scope": self.memory_scope,
            "memory_influence": self.memory_influence,
            "verification_required": self.verification_required,
            "verification_strict": self.verification_strict,
        }
