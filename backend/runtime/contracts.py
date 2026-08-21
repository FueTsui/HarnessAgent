"""业务输入契约。

TaskInput 只描述用户输入；Thread、Turn、Item 与 Agent Loop 状态拥有各自契约，
避免把业务字段、执行状态和界面表示耦合在一起。
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TaskInput:
    query: str = ""
    project_name: str = ""
    city_name: str = ""
    project_address: str = ""
    project_info: str = ""
    industry_structure: str = ""
    electricity_trading: str = ""
    image_scale: str = ""
    satellite_images: list[Path] = field(default_factory=list)
    drawing_images: list[Path] = field(default_factory=list)
    bill_files: list[Path] = field(default_factory=list)
    documents: list[Path] = field(default_factory=list)
    custom_vars: dict = field(default_factory=dict)
    custom_files: dict[str, list[Path]] = field(default_factory=dict)
    custom_var_labels: dict = field(default_factory=dict)
