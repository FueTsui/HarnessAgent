"""PDF 来源解析/渲染的可终止工作进程；stdin/stdout 仅供本机父进程使用。"""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys

# 直接加载纯本地PDF模块，避免导入backend.runtime.__init__时初始化模型/读取应用凭据。
spec = importlib.util.spec_from_file_location("harness_pdf_source", Path(__file__).with_name("presentation_requirements.py"))
source = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = source
spec.loader.exec_module(source)


def main():
    try:
        payload = json.loads(sys.stdin.read(256000))
        if payload.get("action") == "build":
            result = source.build_presentation_requirements(
                payload["objective"], payload["documents"], payload.get("display_names"),
                _trusted_recreation_goal=True,
            )
            response = {"ok": True, "requirements": asdict(result) if result is not None else None}
        elif payload.get("action") == "render":
            source._render_source_page(source.SourcePage(
                1, "", int(payload["source_page"]), 1, "", source_path=Path(payload["source_path"]),
            ), Path(payload["output"]))
            response = {"ok": True}
        else:
            response = {"ok": False}
    except Exception as exc:  # noqa: BLE001 - 不将解析器的原始错误/文档正文暴露到上层
        response = {"ok": False, "error_class": type(exc).__name__}
    print(json.dumps(response, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
