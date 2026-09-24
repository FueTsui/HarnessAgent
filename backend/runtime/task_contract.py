"""Private, serializable task intent shared by routing and verification.

Guidance changes the current objective without deleting the original request.
This object records intent only; it cannot grant tools or change permissions.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskContract:
    objective: str
    original_objective: str = ""
    revision: int = 1
    applied_guidance_ids: list[str] = field(default_factory=list)
    guidance: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.objective = str(self.objective or "").strip()
        self.original_objective = str(self.original_objective or self.objective).strip()
        self.revision = max(1, int(self.revision))

    def apply_guidance(self, content: str, mode: str = "guide",
                       guidance_id: str | int | None = None) -> bool:
        content = str(content or "").strip()
        if not content:
            return False
        if mode not in {"guide", "redirect"}:
            raise ValueError(f"Unsupported guidance mode: {mode}")
        identity = str(guidance_id) if guidance_id is not None else ""
        if identity and identity in self.applied_guidance_ids:
            return False
        self.objective = content if mode == "redirect" else f"{self.objective}\n\n补充要求：{content}"
        self.guidance.append({"mode": mode, "content": content, "id": identity})
        if identity:
            self.applied_guidance_ids.append(identity)
        self.revision += 1
        return True

    def snapshot(self) -> dict:
        """Private checkpoint payload; never pass this to a public event."""
        return {
            "version": 1, "original_objective": self.original_objective,
            "objective": self.objective, "revision": self.revision,
            "applied_guidance_ids": list(self.applied_guidance_ids),
            "guidance": [dict(item) for item in self.guidance],
        }

    @classmethod
    def from_snapshot(cls, payload: dict | None,
                      fallback_objective: str = "") -> TaskContract:
        if not isinstance(payload, dict):
            return cls(fallback_objective)
        if payload.get("version", 1) != 1:
            raise ValueError("Unsupported task contract version")
        return cls(
            objective=str(payload.get("objective") or fallback_objective),
            original_objective=str(payload.get("original_objective") or fallback_objective),
            revision=int(payload.get("revision") or 1),
            applied_guidance_ids=[str(value) for value in payload.get("applied_guidance_ids", [])],
            guidance=[{"mode": str(item.get("mode", "guide")),
                       "content": str(item.get("content", "")),
                       "id": str(item.get("id", ""))}
                      for item in payload.get("guidance", []) if isinstance(item, dict)],
        )

    def public_snapshot(self) -> dict:
        """Intent metadata only; objectives and steering can contain secrets."""
        return {
            "version": 1, "revision": self.revision,
            "guidance_count": len(self.guidance),
            "redirected": any(item["mode"] == "redirect" for item in self.guidance),
        }
