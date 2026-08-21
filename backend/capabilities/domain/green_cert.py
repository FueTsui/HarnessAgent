"""绿证收益确定性计算工具。"""

CERT_PER_KWH = 1000
CERT_UNIT_PRICE = 6.0


def _num(value, default=0.0):
    try:
        if value is None or value == '':
            return default
        return float(value)
    except Exception:
        return default


def main(annual_pv_power: float = 0.0, surplus_ratio: float = 0.2) -> dict:
    annual_pv_power = max(_num(annual_pv_power, 0.0), 0.0)
    surplus_ratio = _num(surplus_ratio, 0.2)
    if surplus_ratio < 0 or surplus_ratio > 1:
        surplus_ratio = 0.2

    tradable_power = annual_pv_power * surplus_ratio
    cert_count = tradable_power / CERT_PER_KWH
    total_revenue = cert_count * CERT_UNIT_PRICE
    return {
        'total_revenue': round(total_revenue, 2),
        'cert_count': round(cert_count, 2),
        'tradable_power': round(tradable_power, 2),
    }
