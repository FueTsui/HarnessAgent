"""日志安全：关闭会打印完整 URL 的网络请求日志，并脱敏常见密钥参数。"""
import logging
import re


_LOG_SECRET_RE = re.compile(
    r"(?i)([?&](?:[a-z0-9_-]*api[_-]?key|token|access[_-]?token|secret)=)[^&\s\"']+"
)


def redact_log_value(value):
    return _LOG_SECRET_RE.sub(r"\1<redacted>", value) if isinstance(value, str) else value


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_log_value(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_log_value(value) for key, value in record.args.items()
            }
        return True


def configure_secure_logging() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())
