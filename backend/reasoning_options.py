"""Capability-bound model reasoning controls; see docs/system-session-20260921/reasoning-adapters.md."""
from __future__ import annotations

import json

EFFORT_LEVELS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
TOGGLE_VALUES = ('disabled', 'enabled')
EFFORT_ORDER = (*EFFORT_LEVELS, *TOGGLE_VALUES)
_LABELS = dict(zip(EFFORT_ORDER, ('不推理', '最少', '轻度', '标准', '深度', '扩展', '最高', '关闭思考', '开启思考')))
_FIVE_SIX = ('none', 'low', 'medium', 'high', 'xhigh', 'max')
_OPENAI_EFFORTS = {
    'gpt-6-astra': ('low', 'medium', 'high', 'xhigh', 'max'),
    **{name: _FIVE_SIX for name in ('gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')},
    'gpt-5.5': ('none', 'low', 'medium', 'high', 'xhigh'),
    'gpt-5.4': ('none', 'low', 'medium', 'high', 'xhigh'),
}
_CLAUDE_THREE = ('low', 'medium', 'high')
_CLAUDE_EFFORTS = {
    'claude-opus-4-5': _CLAUDE_THREE,
    **{name: (*_CLAUDE_THREE, 'max') for name in ('claude-opus-4-6', 'claude-sonnet-4-6', 'claude-mythos-preview')},
    **{name: (*_CLAUDE_THREE, 'xhigh', 'max') for name in (
        'claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5', 'claude-sonnet-5',
        'claude-fable-5', 'claude-fable-5-1', 'claude-mythos-5', 'claude-mythos-5-1')},
}
_LEGACY_CLAUDE_TOGGLE = {'claude-sonnet-4-5', 'claude-haiku-4-5', 'claude-opus-4-1', 'claude-opus-4', 'claude-sonnet-4'}
_DEEPSEEK_EFFORTS = {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}
_MINIMAX_FIXED_THINKING = {'minimax-m2', 'minimax-m2.1', 'minimax-m2.1-highspeed',
                          'minimax-m2.5', 'minimax-m2.5-highspeed', 'minimax-m2.7', 'minimax-m2.7-highspeed'}
_HY_EFFORTS = {'hy3': ('none', 'low', 'high'), 'hy4-preview': ('none', 'high')}
_GLM_MODELS = {'glm-5', 'glm-5.1', 'glm-5.2', 'glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx'}
_KIMI_MODELS = {'kimi-k3', 'kimi-k2.6', 'kimi-k2.7-code', 'kimi-k2.7-code-highspeed'}
_PARAMS = {'auto', 'reasoning_effort', 'reasoning.effort', 'output_config.effort'}


def canonical_reasoning_model_id(value) -> str:
    """Resolve only known vendor namespaces for capabilities, never rewrite the wire ID."""
    model = str(value or '').strip().lower()
    namespace, separator, candidate = model.partition('/')
    if not separator:
        return model
    families = {
        'openai': set(_OPENAI_EFFORTS),
        'anthropic': set(_CLAUDE_EFFORTS) | _LEGACY_CLAUDE_TOGGLE,
        'z-ai': _GLM_MODELS, 'zhipuai': _GLM_MODELS,
        'moonshotai': _KIMI_MODELS,
        'minimax': _MINIMAX_FIXED_THINKING | {'minimax-m3'},
        'minimaxai': _MINIMAX_FIXED_THINKING | {'minimax-m3'},
        'deepseek': _DEEPSEEK_EFFORTS, 'deepseek-ai': _DEEPSEEK_EFFORTS,
        'tencent': set(_HY_EFFORTS), 'tencent-hunyuan': set(_HY_EFFORTS),
        'hunyuan': set(_HY_EFFORTS),
    }
    return candidate if candidate in families.get(namespace, ()) else model


def _field(provider, name, default=None):
    return provider.get(name, default) if isinstance(provider, dict) else getattr(provider, name, default)


def reasoning_profile_is_fixed(provider) -> bool:
    config = normalize_reasoning_config(_field(provider, 'reasoning_config', None))
    return (config.get('mode', 'auto') == 'auto'
            and canonical_reasoning_model_id(_field(provider, 'model_id', '')) in _MINIMAX_FIXED_THINKING)


def normalize_reasoning_config(value) -> dict:
    """Accept API dictionaries and ORM JSON, never arbitrary parameter paths."""
    if value is None or value == '':
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError('推理配置必须是合法 JSON 对象') from exc
    if not isinstance(value, dict):
        raise ValueError('推理配置必须是对象')
    if not value:
        return {}
    if set(value) - {'mode', 'control', 'effort_param', 'supported_efforts', 'budget_tokens'}:
        raise ValueError('推理配置包含不支持的字段')
    mode, control, param = value.get('mode', 'auto'), value.get('control', 'effort'), value.get('effort_param', 'auto')
    if (not isinstance(mode, str) or not isinstance(control, str)
            or mode not in {'auto', 'custom', 'off'} or control not in {'effort', 'thinking_toggle'}):
        raise ValueError('推理配置模式或控制方式无效')
    if not isinstance(param, str) or param not in _PARAMS:
        raise ValueError('推理参数位置无效')
    efforts = value.get('supported_efforts', [])
    valid = EFFORT_LEVELS if control == 'effort' else TOGGLE_VALUES
    if not isinstance(efforts, list) or any(not isinstance(item, str) or item not in valid for item in efforts):
        raise ValueError('推理档位必须匹配控制方式；思考开关只接受 disabled/enabled')
    if mode == 'custom' and not efforts:
        raise ValueError('自定义推理配置必须明确声明至少一个可用值')
    if control == 'thinking_toggle' and param != 'auto':
        raise ValueError('思考开关使用 thinking.type，不接受 effort 参数位置')
    result = {'mode': mode, 'control': control, 'effort_param': param,
              'supported_efforts': [item for item in valid if item in efforts]}
    if 'budget_tokens' in value:
        budget = value['budget_tokens']
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1024:
            raise ValueError('思考预算必须是至少 1024 的整数')
        if control != 'thinking_toggle':
            raise ValueError('思考 token 预算只适用于 Messages 思考开关')
        result['budget_tokens'] = budget
    return result


def _auto_profile(provider):
    model = canonical_reasoning_model_id(_field(provider, 'model_id', ''))
    wire = _field(provider, 'wire_api', 'chat_completions')
    if model in _DEEPSEEK_EFFORTS:
        return 'effort', ('none', 'low', 'high', 'max'), ''
    if model in _HY_EFFORTS:
        return 'effort', _HY_EFFORTS[model], ''
    if model == 'minimax-m3':
        return 'thinking_toggle', TOGGLE_VALUES, ''
    if model in _MINIMAX_FIXED_THINKING:
        return 'thinking_toggle', (), '已识别 MiniMax M2 系列：固定开启思考，不提供强度档位或关闭开关'
    if model in {'deepseek-chat', 'deepseek-reasoner'}:
        return 'effort', (), '该旧版 DeepSeek 官方别名已停用；请选择现行型号，或为第三方兼容接口声明能力'
    if model == 'hy3-preview':
        return 'effort', (), 'Hy3 preview 的路由取决于服务商；请选择 hy3 / hy4-preview 或声明兼容接口能力'
    if model in _OPENAI_EFFORTS:
        if wire == 'messages':
            return 'effort', (), '该 OpenAI 型号未声明 Messages 协议支持，请配置兼容网关能力'
        if model == 'gpt-6-astra' and wire != 'responses':
            return 'effort', (), 'GPT-6 Astra 的工具调用需使用 Responses 协议，请继承连接设置或调整连接'
        return 'effort', _OPENAI_EFFORTS[model], ''
    if model in {'glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx', 'kimi-k3'}:
        return 'effort', ('low', 'high', 'max'), ''
    if model == 'glm-5.2':
        # Official compatibility aliases are not independent strengths.
        return 'effort', ('none', 'high', 'max'), ''
    if model in _CLAUDE_EFFORTS:
        return 'effort', _CLAUDE_EFFORTS[model], ''
    if model in {'glm-5', 'glm-5.1', 'kimi-k2.6'}:
        if wire == 'chat_completions':
            return 'thinking_toggle', TOGGLE_VALUES, ''
        return 'thinking_toggle', (), '此型号仅确认 Chat Completions 的思考开关；兼容网关可手动声明'
    if model in _LEGACY_CLAUDE_TOGGLE:
        if wire == 'messages':
            return 'thinking_toggle', TOGGLE_VALUES, ''
        return 'thinking_toggle', (), '此 Claude 型号仅确认 Messages 的预算思考开关；兼容网关可手动声明'
    if model in {'kimi-k2.7-code', 'kimi-k2.7-code-highspeed'}:
        return 'thinking_toggle', (), '该型号始终思考，不提供强度档位或关闭开关'
    return 'effort', (), '尚未确认该型号的可用推理档位，请继承设置或声明兼容接口能力'


def _resolved_profile(provider):
    config = normalize_reasoning_config(_field(provider, 'reasoning_config', None))
    if config.get('mode') == 'custom':
        return config, config['control'], tuple(config['supported_efforts']), ''
    control, efforts, reason = _auto_profile(provider)
    return config, control, efforts, reason


def _effective_max_tokens(provider):
    extra = _field(provider, 'extra_body', {}) or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    raw = extra.get('max_tokens') if isinstance(extra, dict) else None
    if raw is None:
        if _field(provider, 'max_tokens_param', 'auto') == 'none':
            raise ValueError('Messages 思考开关必须明确设置有效 max_tokens')
        raw = _field(provider, 'max_tokens', 8192) or 8192
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError('Messages 的最大输出 token 必须是正整数')
    return raw


def validate_reasoning_settings(provider) -> None:
    """Strict declarations; unknown auto defaults retain legacy compatibility."""
    config, control, efforts, _ = _resolved_profile(provider)
    mode = config.get('mode', 'auto')
    if mode == 'off':
        return
    if mode != 'custom' and not _field(provider, 'model_reasoning', False):
        return
    effort = _field(provider, 'reasoning_effort', '') or ''
    if mode == 'custom':
        if (_field(provider, 'provider_type') == 'chatgpt'
                or str(_field(provider, 'base_url', '') or '').rstrip('/').lower() == 'https://chatgpt.com/backend-api/codex'):
            raise ValueError('ChatGPT 订阅客户端不支持自定义推理覆盖')
        if not _field(provider, 'model_reasoning', False):
            raise ValueError('自定义推理控制需要启用连接的推理能力')
        if effort and effort not in efforts:
            raise ValueError('默认推理值不在声明的可用值中')
    elif efforts and effort and effort not in efforts:
        # GLM-5.2 officially accepts these aliases, although the UI deliberately
        # displays only its three distinct behaviors.
        if not (canonical_reasoning_model_id(_field(provider, 'model_id', '')) == 'glm-5.2' and effort in EFFORT_LEVELS):
            raise ValueError('默认推理值不在该型号的可用值中')
    wire = _field(provider, 'wire_api', 'chat_completions')
    if 'budget_tokens' in config and (control != 'thinking_toggle' or wire != 'messages'):
        raise ValueError('思考 token 预算只适用于 Messages 思考开关')
    # MiniMax M3 adaptive thinking has no budget parameter. Custom gateway
    # declarations retain the explicit generic Messages budget contract.
    model = canonical_reasoning_model_id(_field(provider, 'model_id', ''))
    uses_budget = not (mode == 'auto' and model == 'minimax-m3')
    if control == 'thinking_toggle' and wire == 'messages' and 'enabled' in efforts and uses_budget:
        budget = config.get('budget_tokens', 2048)
        if not 1024 <= budget < _effective_max_tokens(provider):
            raise ValueError('思考预算必须至少 1024 且小于 Messages 的有效 max_tokens')
        if model in set(_CLAUDE_EFFORTS) - {'claude-opus-4-5', 'claude-opus-4-6', 'claude-sonnet-4-6', 'claude-mythos-preview'}:
            raise ValueError('该 Claude 型号不支持 enabled + budget_tokens，请使用 effort 和 adaptive thinking')


def reasoning_capabilities(provider) -> dict:
    result = {'reasoning_efforts': [], 'reasoning_effort': '', 'reasoning_supported': False,
              'reasoning_unavailable_reason': '', 'reasoning_control': 'effort', 'reasoning_effort_labels': {}}
    if provider is None or _field(provider, 'is_environment_default', False):
        result['reasoning_unavailable_reason'] = '默认环境模型未声明可验证的推理档位，请继承连接设置'
        return result
    if (_field(provider, 'provider_type') == 'chatgpt'
            or str(_field(provider, 'base_url', '') or '').rstrip('/').lower() == 'https://chatgpt.com/backend-api/codex'):
        result['reasoning_unavailable_reason'] = '当前 ChatGPT 订阅客户端不支持每轮推理覆盖，请继承连接设置'
        return result
    if _field(provider, 'wire_api', 'chat_completions') not in {'chat_completions', 'responses', 'messages'}:
        result['reasoning_unavailable_reason'] = '当前协议未实现每轮推理覆盖，请继承连接设置'
        return result
    if not bool(_field(provider, 'model_reasoning', False)):
        result['reasoning_unavailable_reason'] = '该连接未启用推理能力，请继承连接设置'
        return result
    configured = _field(provider, 'reasoning_effort', '') or ''
    result['reasoning_effort'] = configured if configured in EFFORT_ORDER else ''
    try:
        config, control, efforts, reason = _resolved_profile(provider)
        result['reasoning_control'] = control
        if reasoning_profile_is_fixed(provider):
            result['reasoning_effort'] = ''
        if config.get('mode') == 'off':
            result.update(reasoning_effort='', reasoning_unavailable_reason='该连接已关闭推理控制')
            return result
        validate_reasoning_settings(provider)
    except ValueError as exc:
        result['reasoning_unavailable_reason'] = str(exc)
        return result
    result.update(reasoning_efforts=list(efforts), reasoning_supported=bool(efforts),
                  reasoning_unavailable_reason=reason,
                  reasoning_effort_labels={key: _LABELS[key] for key in efforts})
    return result


def common_reasoning_capabilities(providers) -> dict:
    values = [reasoning_capabilities(provider) for provider in providers] or [reasoning_capabilities(None)]
    supported = set(values[0]['reasoning_efforts'])
    for value in values[1:]:
        supported.intersection_update(value['reasoning_efforts'])
    defaults = {value['reasoning_effort'] for value in values}
    controls = {value['reasoning_control'] for value in values}
    return {
        'reasoning_efforts': [value for value in EFFORT_ORDER if value in supported],
        'reasoning_effort': next(iter(defaults)) if len(defaults) == 1 else '',
        'reasoning_supported': bool(supported),
        'reasoning_control': next(iter(controls)) if len(controls) == 1 else 'mixed',
        'reasoning_effort_labels': {key: _LABELS[key] for key in EFFORT_ORDER if key in supported},
        'reasoning_unavailable_reason': '' if supported else (
            values[0]['reasoning_unavailable_reason'] if len(values) == 1
            else '智能体自动路由的候选模型没有共同可用的推理控制；请选择具体模型或继承设置'),
    }


def normalize_turn_reasoning(value) -> str:
    if value is None or value == '':
        return ''
    if not isinstance(value, str) or value not in EFFORT_ORDER:
        raise ValueError('推理强度无效，请使用模型目录返回的可用值；不支持 Ultra 或自动映射')
    return value


def apply_turn_reasoning(snapshot: dict, effort: str) -> dict:
    """Copy, validate and apply; unsupported fallbacks never drop an override."""
    result = dict(snapshot)
    if not effort:
        validate_reasoning_settings(result)
        return result
    capability = reasoning_capabilities(result)
    if effort not in capability['reasoning_efforts']:
        raise ValueError(capability['reasoning_unavailable_reason'] or f'该模型不支持推理值 {effort}')
    result['reasoning_effort'] = effort
    result['model_reasoning'] = True
    validate_reasoning_settings(result)
    return result


def reasoning_wire_settings(provider) -> tuple[str, str, int]:
    """Internal wire contract with only allowlisted parameter locations."""
    config, control, _, _ = _resolved_profile(provider)
    if config.get('mode') == 'off':
        return 'off', '', 0
    param = config.get('effort_param', 'auto')
    if param == 'auto':
        param = {'responses': 'reasoning.effort', 'messages': 'output_config.effort'}.get(
            _field(provider, 'wire_api', 'chat_completions'), 'reasoning_effort')
    return control, param, config.get('budget_tokens', 2048)
