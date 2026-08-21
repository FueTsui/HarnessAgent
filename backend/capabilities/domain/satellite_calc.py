"""卫星图估算结果的确定性计算工具。"""

import json
import math
import re

CAPACITY_W_PER_M2 = 150.0
DEFAULT_USABLE_RATIO = 0.62
DEFAULT_ANNUAL_HOURS = 1100.0

CITY_HOURS = {
    '上海': 1050, '南京': 1100, '苏州': 1080, '杭州': 1050, '合肥': 1100,
    '北京': 1250, '天津': 1250, '济南': 1250, '青岛': 1200,
    '广州': 1050, '深圳': 1050, '成都': 950, '重庆': 900,
    '西安': 1200, '武汉': 1050, '长沙': 1050, '郑州': 1200,
}


def _to_float(value, default=None):
    try:
        if value is None or value == '':
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _clamp(value, low, high, default):
    value = _to_float(value, default)
    return max(low, min(high, value))


def _extract_json(text):
    text = '' if text is None else str(text).strip()
    # 移除 <think>/<thinking> XML 标签
    text = re.sub(
        r'<(?:think|thinking)[^>]*>.*?</(?:think|thinking)\s*>',
        '', text, flags=re.DOTALL
    )
    # 移除 DeepSeek R1 的 ... 标记
    text = re.sub(r'', '', text)
    text = re.sub(r'```$\s*', '', text, flags=re.MULTILINE)

    # 提取 markdown 代码块内的 JSON
    fence = re.search(r'```(?:json)?\s*(.*?)```', text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 清理残留 ... 和多余空白
    text = re.sub(r'', '', text)
    text = text.strip()

    start = text.find('{')
    if start < 0:
        # LLM 可能输出不带开头 { 的 headless JSON
        if re.match(r'\s*"[^"]+"\s*:', text):
            text = '{' + text
            start = 0
        else:
            return {}

    for end in range(len(text), start, -1):
        try:
            data = json.loads(text[start:end])
            return data if isinstance(data, dict) else {}
        except Exception:
            continue
    return {}


def _annual_hours(city_name):
    city = '' if city_name is None else str(city_name)
    for key, hours in CITY_HOURS.items():
        if key in city:
            return float(hours)
    return DEFAULT_ANNUAL_HOURS


def _parse_scale_text(image_scale_text):
    text = '' if image_scale_text is None else str(image_scale_text)
    # 支持"1像素=0.3米""0.3m/px""比例尺100米=250像素"等常见人工输入
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(?:米|m)\s*/\s*(?:px|像素)', text, re.I)
    if m:
        return _to_float(m.group(1))
    m = re.search(r'(?:1\s*(?:px|像素)\s*[=:：]\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:米|m)', text, re.I)
    if m and ('像素' in text or 'px' in text.lower()):
        return _to_float(m.group(1))
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(?:米|m).*?([0-9]+(?:\.[0-9]+)?)\s*(?:px|像素)', text, re.I)
    if m:
        meters = _to_float(m.group(1))
        pixels = _to_float(m.group(2))
        if meters and pixels and pixels > 0:
            return meters / pixels
    return None


def main(satellite_json_text: str = '', city_name: str = '', image_scale_text: str = '', project_info: str = '') -> dict:
    data = _extract_json(satellite_json_text)
    warnings = []

    confidence = _clamp(data.get('confidence'), 0.0, 1.0, 0.35)
    annual_hours = _annual_hours(city_name)
    usable_ratio = _clamp(data.get('usable_roof_ratio_estimate'), 0.35, 0.85, DEFAULT_USABLE_RATIO)

    meters_per_pixel = _to_float(data.get('meters_per_pixel_estimate'))
    manual_mpp = _parse_scale_text(image_scale_text)
    if manual_mpp:
        meters_per_pixel = manual_mpp

    scale_bar_meters = _to_float(data.get('scale_bar_meters'))
    scale_bar_pixels = _to_float(data.get('scale_bar_pixels'))
    if not meters_per_pixel and scale_bar_meters and scale_bar_pixels and scale_bar_pixels > 0:
        meters_per_pixel = scale_bar_meters / scale_bar_pixels

    roof_area = _to_float(data.get('roof_area_m2_estimate'))
    usable_area = _to_float(data.get('usable_roof_area_m2_estimate'))

    has_scale_basis = bool(meters_per_pixel or manual_mpp or (scale_bar_meters and scale_bar_pixels))
    if (roof_area is not None and roof_area > 0) and not has_scale_basis:
        warnings.append('面积来自视觉模型估算但缺少比例尺/米像素依据，只能作为前期粗估。')
        confidence = min(confidence, 0.55)

    if roof_area is not None and roof_area > 0 and usable_area is None:
        usable_area = roof_area * usable_ratio
    elif usable_area is not None and usable_area > 0 and roof_area is None:
        roof_area = usable_area / usable_ratio if usable_ratio > 0 else usable_area

    if roof_area is None or roof_area <= 0:
        roof_area = 0.0
        warnings.append('未获得可靠比例尺或屋面面积，屋面面积按0输出，报告中必须提示需补充比例尺或CAD图纸。')
    if usable_area is None or usable_area <= 0:
        usable_area = 0.0

    if not meters_per_pixel and roof_area == 0:
        warnings.append('卫星图缺少可复核米/像素比例，不能做精确平方米换算。')
    if confidence < 0.65:
        warnings.append('卫星视觉识别置信度低于0.65，测算仅可用于前期粗估。')

    pv_capacity_kwp = usable_area * CAPACITY_W_PER_M2 / 1000.0
    annual_generation_kwh = pv_capacity_kwp * annual_hours

    risk_flags = data.get('risk_flags') if isinstance(data.get('risk_flags'), list) else []
    for flag in risk_flags:
        flag = str(flag).strip()
        if flag and flag not in warnings:
            warnings.append(flag)

    basis = (
        f'单位面积装机按{CAPACITY_W_PER_M2:.0f}W/m2；'
        f'可铺设面积={usable_area:.2f}m2；'
        f'装机容量=可铺设面积*{CAPACITY_W_PER_M2:.0f}/1000；'
        f'年发电量=装机容量*年有效利用小时数{annual_hours:.0f}h。'
    )

    result = (
        '## 卫星图参数计算结果\n'
        f'- 屋面面积：{roof_area:.2f} m2\n'
        f'- 可铺设面积：{usable_area:.2f} m2\n'
        f'- 推荐光伏装机：{pv_capacity_kwp:.2f} kWp（{pv_capacity_kwp/1000:.3f} MWp）\n'
        f'- 年有效利用小时数：{annual_hours:.0f} h\n'
        f'- 年发电量：{annual_generation_kwh:.0f} kWh（{annual_generation_kwh/10000:.2f} 万kWh）\n'
        f'- 视觉置信度：{confidence:.2f}\n'
        f'- 计算依据：{basis}\n'
        f'- 风险提示：{";".join(warnings) if warnings else "无明显风险，但仍需现场复核屋面承重、遮挡和接入条件。"}\n'
    )

    return {
        'result': result,
        'roof_area_m2': round(roof_area, 2),
        'usable_roof_area_m2': round(usable_area, 2),
        'pv_capacity_kwp': round(pv_capacity_kwp, 2),
        'pv_capacity_mwp': round(pv_capacity_kwp / 1000.0, 4),
        'annual_hours': round(annual_hours, 2),
        'annual_generation_kwh': round(annual_generation_kwh, 2),
        'annual_generation_10k_kwh': round(annual_generation_kwh / 10000.0, 2),
        'confidence': round(confidence, 3),
        'calculation_basis': basis,
        'warnings': '；'.join(warnings) if warnings else '无明显风险，但仍需现场复核屋面承重、遮挡和接入条件。',
    }
