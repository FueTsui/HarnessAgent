"""虚拟电厂投资确定性测算工具。"""

import math
from typing import Any, Dict, Tuple

# ============================================================
# 纯Python IRR计算（无外部依赖）
# ============================================================
def _newton_irr(cashflows: list, guess: float = 0.06, max_iter: int = 200, tol: float = 1e-10) -> float:
    r = guess
    for _ in range(max_iter):
        npv_val = 0.0
        dnpv_val = 0.0
        for t, cf in enumerate(cashflows):
            denom = (1.0 + r) ** t
            npv_val += cf / denom
            dnpv_val += -t * cf / ((1.0 + r) ** (t + 1))
        if abs(dnpv_val) < 1e-15:
            break
        r_new = r - npv_val / dnpv_val
        if abs(r_new - r) < tol:
            return r_new
        r = r_new
    return r


# ============================================================
# VPP 常量曲线（全部转为 float list，避免 Decimal 热路径开销）
# ============================================================
SUNNY_PROFILE = [0.0, 0, 0, 0, 0, 0, 0, 0.01, 0.05, 0.12, 0.19, 0.22, 0.18, 0.11, 0.05, 0.04, 0.02, 0.01, 0, 0, 0, 0, 0, 0]
CLOUDY_PROFILE = [0.0, 0, 0, 0, 0, 0, 0, 0.03, 0.09, 0.15, 0.18, 0.19, 0.17, 0.10, 0.05, 0.025, 0.01, 0.005, 0, 0, 0, 0, 0, 0]

LOAD_RATIOS = {
    'dc_lighting': [0.0, 0, 0, 0, 0, 0, 0.08, 0.4, 0.76, 0.76, 0.76, 0.64, 0.64, 0.76, 0.76, 0.76, 0.76, 0.24, 0.24, 0, 0, 0, 0, 0],
    'dc_ac': [0.0, 0, 0, 0, 0, 0, 0, 0, 0.344, 0.56, 0.712, 0.728, 0.7008, 0.688, 0.712, 0.8, 0.64, 0.56, 0.48, 0, 0, 0, 0, 0],
    'charging': [0.0, 0, 0, 0, 0, 0, 0, 0, 0.45, 0.45, 0.117, 0.117, 0.117, 0.117, 0.45, 0.45, 0.09, 0.09, 0.09, 0, 0, 0, 0, 0],
    'garage_lighting': [0.8] * 24,
    'landscape_lighting': [0.8, 0.8, 0.8, 0.8, 0.8, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8],
    'ac_load': [0.0, 0, 0, 0, 0, 0, 0, 0, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0.2616, 0, 0, 0, 0, 0],
}

TOU_PRICE = [0.21, 0.21, 0.21, 0.21, 0.21, 0.21, 0.21, 0.21, 0.57, 0.57, 0.96, 1.20, 0.57, 0.57, 0.96, 1.20, 1.20, 0.96, 0.96, 0.57, 0.57, 0.57, 0.57, 0.57]
MARKET_PRICE = [0.43, 0.23, 0.21, 0.20, 0.19, 0.18, 0.18, 0.17, 0.18, 0.17, 0.12, 0.10, 0.13, 0.17, 0.17, 0.15, 0.16, 0.17, 0.17, 0.18, 0.18, 0.21, 0.21, 0.20]

_SUNNY_TOTAL = sum(SUNNY_PROFILE)
_CLOUDY_TOTAL = sum(CLOUDY_PROFILE)


# ============================================================
# VPP 默认输入与合并
# ============================================================
def _build_vpp_defaults() -> dict:
    return {
        'pv': {
            'pv_component_power_wp': 0.0, 'pv_component_count': 0,
            'pv_laying_area_m2': 2424.0, 'per_area_power_wpm2': 150.0,
            'roof_area_m2': 0.0, 'pv_design_power_kw': 0.0,
            'annual_hours': 911.19, 'system_efficiency': 100.0,
            'sunny_weight': 0.55, 'cloudy_to_sunny_ratio': 0.35,
        },
        'load': {
            'dc_lighting_base_kw': 60.0, 'dc_ac_base_kw': 389.65,
            'charging_base_kw': 148.0, 'garage_lighting_base_kw': 47.45,
            'landscape_lighting_base_kw': 15.0, 'ac_load_base_kw': 86.0,
        },
        'flexibility': {'lighting_flex_ratio': 0.30, 'ac_flex_ratio': 0.20},
        'response': {
            'battery_capacity_kwh': 300.0, 'battery_power_kw': 200.0,
            'soc_min_ratio': 0.0, 'soc_max_ratio': 1.0, 'soc_init_ratio': 0.5,
            'charge_efficiency': 1.0, 'discharge_efficiency': 1.0,
            'cycle_limit_per_day': 2.0, 'battery_throughput_cost': 0.0525,
            'response_hours': '13', 'compensation_price': 3.08,
            'market_price_scale': 1.0, 'tou_price_scale': 1.0,
            'dispatch_step_kwh': 10.0,
        },
        'carbon': {'grid_emission_factor_kg_per_kwh': 0.5246, 'annual_response_days': 20},
    }


def _merge_vpp_inputs(base: dict, overrides: dict) -> dict:
    if not overrides:
        return base
    merged = {}
    for group, values in base.items():
        merged[group] = dict(values) if isinstance(values, dict) else values
    for group, values in overrides.items():
        if group == 'profiles':
            merged.setdefault('profiles', {})
            for k, v in (values or {}).items():
                merged['profiles'][k] = list(v)
        elif isinstance(values, dict):
            merged.setdefault(group, {})
            for k, v in values.items():
                merged[group][k] = v
    return merged


# ============================================================
# VPP 装机容量解析
# ============================================================
def _resolve_capacity(pv: dict) -> dict:
    comp_kw = float(pv.get('pv_component_power_wp', 0)) * float(pv.get('pv_component_count', 0)) / 1000.0
    lay_kw = float(pv.get('pv_laying_area_m2', 0)) * float(pv.get('per_area_power_wpm2', 0)) / 1000.0
    des_kw = float(pv.get('pv_design_power_kw', 0))
    cap = max(comp_kw, lay_kw, des_kw)
    method = '未识别'
    if cap > 0:
        if abs(cap - comp_kw) < 0.01:
            method = '组件法'
        elif abs(cap - lay_kw) < 0.01:
            method = '铺设法'
        elif abs(cap - des_kw) < 0.01:
            method = '设计法'
    return {'component_capacity_kw': comp_kw, 'laying_capacity_kw': lay_kw,
            'design_capacity_kw': des_kw, 'installed_capacity_kw': cap, 'selected_method': method}


# ============================================================
# VPP 光伏出力计算
# ============================================================
def _scale_profile(total_energy: float, weights: list) -> list:
    total = sum(weights)
    if total == 0:
        return [0.0] * len(weights)
    return [total_energy * w / total for w in weights]


def _compute_pv_profile(inputs: dict) -> dict:
    pv = inputs['pv']
    cap_meta = _resolve_capacity(pv)
    cap = cap_meta['installed_capacity_kw']
    ah = float(pv.get('annual_hours', 911.19))
    eff = float(pv.get('system_efficiency', 100.0)) / 100.0
    sw = float(pv.get('sunny_weight', 0.55))
    cw = 1.0 - sw
    cr = float(pv.get('cloudy_to_sunny_ratio', 0.35))
    avg_h = ah / 365.0
    denom = sw + cw * cr
    sunny_h = avg_h / denom if denom else 0.0
    cloudy_h = sunny_h * cr
    sun_energy = cap * sunny_h * eff
    cld_energy = cap * cloudy_h * eff
    sunny_hourly = _scale_profile(sun_energy, SUNNY_PROFILE)
    cloudy_hourly = _scale_profile(cld_energy, CLOUDY_PROFILE)

    rows = []
    for h in range(24):
        rows.append({'time': f'{h}:00', 'sunny_kw': sunny_hourly[h], 'cloudy_kw': cloudy_hourly[h]})
    return {
        'rows': rows,
        'summary': {
            'installed_capacity_kw': cap,
            'system_efficiency_ratio': eff,
            'vpp_annual_hours': ah,
            'sunny_daily_energy_kwh': sun_energy,
            'cloudy_daily_energy_kwh': cld_energy,
        },
    }


# ============================================================
# VPP 典型日负荷
# ============================================================
def _compute_typical_day(inputs: dict, pv_profile: dict) -> dict:
    ld = inputs['load']
    fb = inputs['flexibility']
    rows = []
    for h in range(24):
        dc_l = LOAD_RATIOS['dc_lighting'][h] * ld['dc_lighting_base_kw']
        dc_ac = LOAD_RATIOS['dc_ac'][h] * ld['dc_ac_base_kw']
        chg = LOAD_RATIOS['charging'][h] * ld['charging_base_kw']
        gar = LOAD_RATIOS['garage_lighting'][h] * ld['garage_lighting_base_kw']
        lnd = LOAD_RATIOS['landscape_lighting'][h] * ld['landscape_lighting_base_kw']
        ac_l = LOAD_RATIOS['ac_load'][h] * ld['ac_load_base_kw']
        flex = dc_l * fb['lighting_flex_ratio'] + dc_ac * fb['ac_flex_ratio'] + chg
        rows.append({
            'time': f'{h}:00',
            'sunny_kw': pv_profile['rows'][h]['sunny_kw'],
            'cloudy_kw': pv_profile['rows'][h]['cloudy_kw'],
            'community_dc_total_kw': dc_l + dc_ac + chg,
            'other_dc_total_kw': gar + lnd,
            'ac_load_kw': ac_l,
            'flexibility_potential_sunny_kw': flex,
            'flexibility_potential_cloudy_kw': flex,
        })
    return {'rows': rows}


# ============================================================
# VPP 储能调度模拟（简化 DP，粗粒度加速）
# ============================================================
def _parse_hours(text) -> list:
    hours = []
    for item in str(text).replace('，', ',').split(','):
        s = item.strip()
        if not s:
            continue
        try:
            h = int(s)
            if 0 <= h <= 23 and h not in hours:
                hours.append(h)
        except ValueError:
            continue
    return hours


def _simulate_dispatch(rows: list, inputs: dict, scenario: str) -> dict:
    resp = inputs['response']

    # 使用较粗的 step 以避免 DP 状态爆炸（定时根本原因）
    batt_power = float(resp.get('battery_power_kw', 200))
    batt_cap = float(resp.get('battery_capacity_kwh', 300))
    step = max(float(resp.get('dispatch_step_kwh', 10)), batt_power / 8.0 if batt_power > 0 else 5.0)

    soc_min = batt_cap * float(resp.get('soc_min_ratio', 0))
    soc_max = batt_cap * float(resp.get('soc_max_ratio', 1))
    soc_init = batt_cap * float(resp.get('soc_init_ratio', 0.5))
    chg_eff = max(float(resp.get('charge_efficiency', 1.0)), 1e-9)
    dis_eff = max(float(resp.get('discharge_efficiency', 1.0)), 1e-9)
    cycle_limit = float(resp.get('cycle_limit_per_day', 2.0))
    throughput_cost = float(resp.get('battery_throughput_cost', 0.0525))

    pv_key = 'sunny_kw' if scenario == 'sunny' else 'cloudy_kw'
    response_hours_set = set(_parse_hours(str(resp.get('response_hours', '13'))))
    comp_price = float(resp.get('compensation_price', 3.08))
    mkt_scale = float(resp.get('market_price_scale', 1.0))
    tou_scale = float(resp.get('tou_price_scale', 1.0))

    # 构建价格曲线（已全部为 float）
    comp_profile = [comp_price if h in response_hours_set else 0.0 for h in range(24)]
    mkt_profile = [v * mkt_scale for v in MARKET_PRICE]
    tou_profile = [v * tou_scale for v in TOU_PRICE]

    # 净负荷 = 总负荷 - 光伏
    net_load = [
        rows[h]['community_dc_total_kw'] + rows[h]['other_dc_total_kw'] + rows[h]['ac_load_kw'] - rows[h][pv_key]
        for h in range(24)
    ]

    # 整数化状态：power_steps 为动作粒度数，soc/charge 按 step 取整
    power_steps = max(int(round(batt_power / step)), 1)
    soc_min_i = int(round(soc_min / step))
    soc_max_i = int(round(soc_max / step))
    soc_init_i = int(round(soc_init / step))
    charge_limit_i = max(int(round((batt_cap * cycle_limit) / step)), 0)

    # DP: 状态 = (soc_i, charged_i) → best_profit
    states = {(soc_init_i, 0): 0.0}
    parents = []

    for hour in range(24):
        next_states = {}
        parent_map = {}
        for (soc_i, charged_i), profit in states.items():
            soc_val = soc_i * step
            for action_i in range(-power_steps, power_steps + 1):
                action_kw = action_i * step
                charge = max(-action_kw, 0.0)
                discharge = max(action_kw, 0.0)
                next_soc = soc_val + charge * chg_eff - discharge / dis_eff
                next_soc_i = int(round(next_soc / step))
                if next_soc_i < soc_min_i or next_soc_i > soc_max_i:
                    continue
                charged_inc = int(round((charge * chg_eff) / step))
                next_charged_i = charged_i + charged_inc
                if next_charged_i > charge_limit_i:
                    continue

                grid_import = max(net_load[hour] - action_kw, 0.0)
                grid_export = max(action_kw - net_load[hour], 0.0)
                resp_reduce = max(min(discharge, max(net_load[hour], 0.0)), 0.0)

                revenue = (grid_export * mkt_profile[hour] +
                           resp_reduce * comp_profile[hour] -
                           grid_import * tou_profile[hour] -
                           (charge + discharge) * throughput_cost)
                total = profit + revenue
                key = (next_soc_i, next_charged_i)
                if total > next_states.get(key, float('-inf')):
                    next_states[key] = total
                    parent_map[key] = ((soc_i, charged_i), action_i)
        states = next_states
        parents.append(parent_map)

    # 回溯最优路径
    finals = [(k, p) for k, p in states.items() if k[0] == soc_init_i]
    if finals:
        best = max(finals, key=lambda x: x[1])[0]
    else:
        best = max(states.items(), key=lambda x: (x[1], -abs(x[0][0] - soc_init_i)))[0]

    actions = [0.0] * 24
    soc_trace = [0.0] * 24
    sk = best
    for hour in range(23, -1, -1):
        prev, ac_i = parents[hour][sk]
        actions[hour] = ac_i * step
        soc_trace[hour] = sk[0] * step
        sk = prev

    # 累积指标
    result_rows = []
    resp_rev = mkt_rev = grid_cost = base_grid_cost = batt_cost = 0.0
    load_e = pv_e = chg_e = dis_e = base_import = base_export = imp_total = exp_total = 0.0

    for hour, ac in enumerate(actions):
        charge = max(-ac, 0.0)
        discharge = max(ac, 0.0)
        gi = max(net_load[hour] - ac, 0.0)
        ge = max(ac - net_load[hour], 0.0)
        rr = max(min(discharge, max(net_load[hour], 0.0)), 0.0)

        load_e += rows[hour]['community_dc_total_kw'] + rows[hour]['other_dc_total_kw'] + rows[hour]['ac_load_kw']
        pv_e += rows[hour][pv_key]
        chg_e += charge
        dis_e += discharge
        bi = max(net_load[hour], 0.0)
        be = max(-net_load[hour], 0.0)
        base_import += bi
        base_export += be
        imp_total += gi
        exp_total += ge
        resp_rev += rr * comp_profile[hour]
        mkt_rev += ge * mkt_profile[hour]
        grid_cost += gi * tou_profile[hour]
        base_grid_cost += bi * tou_profile[hour]
        batt_cost += (charge + discharge) * throughput_cost

        result_rows.append({'charge_kw': round(charge, 4), 'discharge_kw': round(discharge, 4),
                            'soc_kwh': round(soc_trace[hour], 4)})

    peak_benefit = max(base_grid_cost - grid_cost, 0.0)
    return {
        'rows': result_rows,
        'summary': {
            'response_hours': sorted(response_hours_set),
            'load_energy_kwh': round(load_e, 4),
            'pv_energy_kwh': round(pv_e, 4),
            'charge_energy_kwh': round(chg_e, 4),
            'discharge_energy_kwh': round(dis_e, 4),
            'base_grid_import_kwh': round(base_import, 4),
            'base_grid_export_kwh': round(base_export, 4),
            'grid_import_kwh': round(imp_total, 4),
            'grid_export_kwh': round(exp_total, 4),
            'avoided_grid_import_kwh': round(base_import - imp_total, 4),
            'base_grid_cost_yuan': round(base_grid_cost, 4),
            'peak_shaving_benefit_yuan': round(peak_benefit, 4),
            'gross_revenue_yuan': round(resp_rev + mkt_rev + peak_benefit, 4),
            'total_profit_yuan': round(resp_rev + mkt_rev - grid_cost - batt_cost, 4),
            'response_revenue_yuan': round(resp_rev, 4),
            'market_revenue_yuan': round(mkt_rev, 4),
            'grid_cost_yuan': round(grid_cost, 4),
            'battery_cost_yuan': round(batt_cost, 4),
        },
    }


def _attach_dispatch(rows: list, inputs: dict) -> dict:
    sunny = _simulate_dispatch(rows, inputs, 'sunny')
    cloudy = _simulate_dispatch(rows, inputs, 'cloudy')
    for row, sr, cr in zip(rows, sunny['rows'], cloudy['rows']):
        row['response_sunny_charge_kw'] = sr['charge_kw']
        row['response_sunny_discharge_kw'] = sr['discharge_kw']
        row['response_sunny_soc_kwh'] = sr['soc_kwh']
        row['response_cloudy_charge_kw'] = cr['charge_kw']
        row['response_cloudy_discharge_kw'] = cr['discharge_kw']
        row['response_cloudy_soc_kwh'] = cr['soc_kwh']
    return {'selection_mode': 'built_in_simplified_dp',
            'response_hours': sunny['summary']['response_hours'],
            'scenarios': {'sunny': sunny['summary'], 'cloudy': cloudy['summary']}}


# ============================================================
# VPP 自消纳率计算
# ============================================================
def _calc_self_use_ratio(inputs: dict) -> float:
    pv_profile = _compute_pv_profile(inputs)
    typical_day = _compute_typical_day(inputs, pv_profile)
    resp_meta = _attach_dispatch(typical_day['rows'], inputs)

    sunny = resp_meta['scenarios']['sunny']
    cloudy = resp_meta['scenarios']['cloudy']
    sw = float(inputs['pv']['sunny_weight'])
    cw = 1.0 - sw
    resp_days = float(inputs['carbon']['annual_response_days'])

    sun_pv = float(sunny['pv_energy_kwh'])
    cld_pv = float(cloudy['pv_energy_kwh'])
    sun_exp_s = float(sunny['grid_export_kwh'])
    cld_exp_s = float(cloudy['grid_export_kwh'])
    sun_exp_b = float(sunny['base_grid_export_kwh'])
    cld_exp_b = float(cloudy['base_grid_export_kwh'])

    annual_pv = 365.0 * (sw * sun_pv + cw * cld_pv)
    resp_export = resp_days * (sw * sun_exp_s + cw * cld_exp_s)
    non_resp_export = (365.0 - resp_days) * (sw * sun_exp_b + cw * cld_exp_b)
    annual_export = resp_export + non_resp_export

    if annual_pv > 0:
        ratio = max(0.0, min(1.0, 1.0 - annual_export / annual_pv))
    else:
        ratio = 1.0
    return ratio


# ============================================================
# 经济模型参数
# ============================================================
class Params:
    rooftop_pv_kwp: float = 4598.06724
    carport_pv_kwp: float = 8851.24139
    charging_station_count: float = 1074.0
    charging_pile_count: float = 2620.0
    storage_kw: float = 200.0
    operation_years: int = 20
    storage_capacity_kwh: float = 200.0
    solar_radiation: float = 1183.7
    self_use_ratio: float = 1.0
    price_C19_pv_period: float = 1.107
    price_C20_normal_period: float = 0.7657
    price_C21_public: float = 0.5802
    price_C22_surplus_grid: float = 0.3372375
    surplus_ratio: float = 0.42572526
    charger_power: float = 7.0
    charger_hours: float = 6.0
    charger_util: float = 0.6
    charger_fee: float = 0.25
    station_power: float = 2.0
    station_interfaces: int = 10
    station_hours: float = 6.0
    station_util: float = 0.6
    station_fee: float = 0.55
    efficiency_coef: float = 0.82
    degradation_rate: float = 0.01439498
    charging_discount: float = 1.0
    pv_discount: float = 0.8
    charger_loss: float = 0.06
    station_loss: float = 0.06
    vat_elec: float = 0.13
    vat_charger: float = 0.13
    vat_parking: float = 0.06
    vat_power: float = 0.13
    vat_other: float = 0.06
    vat_equip: float = 0.13
    vat_service: float = 0.12
    company_staff: int = 6
    company_salary: float = 27.0
    om_staff: int = 12
    om_salary: float = 11.0
    benefit_ratio: float = 1.0
    safety_cost: float = 12.0
    training_cost: float = 6.5
    spare_pv: float = 0.4
    spare_charger: float = 0.01
    office_cost: float = 8.0
    pv_cleaning: float = 0.8
    transport: float = 12.0
    travel: float = 12.0
    insurance_rate: float = 0.0015
    platform_charger: float = 0.06
    platform_pv: float = 0.06
    pv_dep_year: int = 20
    pv_residual: float = 0.0
    charger_dep_year: int = 8
    charger_residual: float = 0.0
    storage_power_ratio: float = 0.0955
    storage_charge_rate: float = 0.75
    storage_discharge_rate: float = 0.75

    @property
    def total_pv_kwp(self) -> float:
        return self.rooftop_pv_kwp + self.carport_pv_kwp

    @property
    def total_pv_mwp(self) -> float:
        return self.total_pv_kwp / 1000.0


REQUIRED_PARAMS = {
    'rooftop_pv_kwp', 'carport_pv_kwp', 'charging_station_count', 'charging_pile_count',
    'storage_kw', 'operation_years', 'storage_capacity_kwh', 'solar_radiation',
    'price_C19_pv_period', 'price_C20_normal_period', 'price_C21_public', 'price_C22_surplus_grid',
    'surplus_ratio', 'charger_power', 'charger_hours', 'charger_util', 'charger_fee',
    'station_power', 'station_interfaces', 'station_hours', 'station_util', 'station_fee',
}


def _to_num(value, default):
    try:
        if value is None or value == '':
            return default
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


# ============================================================
# 经济模型常量
# ============================================================
N_PERIODS = 44
OP_START = 1
TOTAL_INVEST = 5467.873882

_EQUITY_CF = [
    -1200.6964, 2.9932, 3.0144, 3.2551, 3.2856, 3.7434, 3.7529,
    3.6649, 3.6968, 3.2927, 3.0419, 3.0682, 3.0667, 2.9631,
    2.9865, 37.5718, 300.3699, -187.1719, 296.3538, 295.5666,
    294.7817, 293.9992, 293.219, 292.4412,
]


def _build_unlevered_cf() -> list:
    loan = TOTAL_INVEST * 0.8
    semi_r = 0.0285 / 2.0
    n_pay = 18
    balance = loan * (1.0 + 0.0285 / 4.0)
    pmt = balance * semi_r * (1.0 + semi_r) ** n_pay / ((1.0 + semi_r) ** n_pay - 1.0)
    principal = [0.0] * N_PERIODS
    interest = [0.0] * N_PERIODS
    for t in range(OP_START, OP_START + n_pay):
        if balance <= 0:
            break
        interest[t] = balance * semi_r
        principal[t] = min(pmt - interest[t], balance)
        balance -= principal[t]
    ucf = list(_EQUITY_CF)
    ucf[0] = -TOTAL_INVEST
    for t in range(1, len(_EQUITY_CF)):
        ucf[t] += principal[t] + interest[t] * 0.75
    return ucf


UNLEVERED_CF = _build_unlevered_cf()
_BASE = Params()


# ============================================================
# 经济模型缩放因子
# ============================================================
def _scales(p: Params, base: Params, self_use: float) -> dict:
    s = {}
    s['pv'] = p.total_pv_kwp / base.total_pv_kwp if base.total_pv_kwp else 1.0
    s['pile'] = p.charging_pile_count / base.charging_pile_count if base.charging_pile_count else 1.0
    s['station'] = p.charging_station_count / base.charging_station_count if base.charging_station_count else 1.0
    s['gen'] = (p.solar_radiation / base.solar_radiation if base.solar_radiation else 1.0) * \
               (p.efficiency_coef / base.efficiency_coef if base.efficiency_coef else 1.0)

    bp = base.price_C19_pv_period * 0.55 + base.price_C20_normal_period * 0.20 + base.price_C21_public * 0.25
    np_ = p.price_C19_pv_period * 0.55 + p.price_C20_normal_period * 0.20 + p.price_C21_public * 0.25
    s['price'] = np_ / bp if bp else 1.0
    s['surplus_price'] = p.price_C22_surplus_grid / base.price_C22_surplus_grid if base.price_C22_surplus_grid else 1.0
    s['self_use'] = self_use / base.self_use_ratio if base.self_use_ratio else self_use

    br = base.charger_power * base.charger_hours * base.charger_util * base.charger_fee * (1.0 - base.charger_loss)
    nr = p.charger_power * p.charger_hours * p.charger_util * p.charger_fee * (1.0 - p.charger_loss)
    s['charger_rev'] = nr / br if br else 1.0

    bs = base.station_interfaces * base.station_hours * base.station_util * base.station_fee * (1.0 - base.station_loss)
    ns = p.station_interfaces * p.station_hours * p.station_util * p.station_fee * (1.0 - p.station_loss)
    s['station_rev'] = ns / bs if bs else 1.0
    s['deg'] = p.degradation_rate / base.degradation_rate if base.degradation_rate else 1.0
    return s


def _extend(cf24: list, op_years: int, deg_rate: float) -> list:
    full = [0.0] * N_PERIODS
    for i in range(min(len(cf24), N_PERIODS)):
        full[i] = cf24[i]
    end = min(OP_START + op_years * 2, N_PERIODS)
    if len(cf24) >= end:
        return full
    pos = [v for v in cf24 if v > 0]
    base_val = sum(pos[-6:]) / 6.0 if len(pos) >= 6 else (pos[-1] if pos else 0.0)
    semi_deg = 1.0 - deg_rate / 2.0
    for t in range(len(cf24), end):
        full[t] = base_val * (semi_deg ** (t - len(cf24)))
    return full


def _irr(cf_arr: list) -> float:
    nz = [i for i, v in enumerate(cf_arr) if abs(v) > 1e-8]
    if len(nz) < 2:
        return 0.0
    c = cf_arr[nz[0]:nz[-1] + 1]
    for g in [0.06, 0.07, 0.08, 0.05, 0.09, 0.10, 0.04, 0.12]:
        try:
            r = _newton_irr(c, guess=g, max_iter=200, tol=1e-10)
            if -0.5 < r < 1.0:
                return r
        except Exception:
            continue
    return 0.0


def _pb(cum: list) -> float:
    for t in range(1, len(cum)):
        if cum[t] >= 0 > cum[t - 1]:
            return (t - 1 + (-cum[t - 1]) / (cum[t] - cum[t - 1])) / 2.0
    return -1.0


def _run_economic(p: Params, self_use: float) -> dict:
    s = _scales(p, _BASE, self_use)
    cf = [0.0] * N_PERIODS
    start = OP_START

    for t in range(len(UNLEVERED_CF)):
        v = UNLEVERED_CF[t]
        if v < -1:
            v *= s['pv']
        elif t >= start and v > 0:
            v *= (s['pv'] * s['price'] * s['gen'] * 0.55 +
                  s['pile'] * s['charger_rev'] * 0.20 +
                  s['station'] * s['station_rev'] * 0.25)
            v *= (0.85 + 0.15 * s['self_use'])
            if s['deg'] != 1.0:
                y = (t - start) / 2.0
                if s['deg'] < 1:
                    v *= s['deg'] ** y
                else:
                    v *= max(0.0, 1.0 - (s['deg'] - 1.0) * y * 0.3)
        elif v < 0 and t >= start:
            v *= s['pv']
        cf[t] = v

    cf = _extend(cf[:len(UNLEVERED_CF)], p.operation_years, p.degradation_rate)
    semi_irr = _irr(cf)
    irr_annual = (1.0 + semi_irr) ** 2 - 1.0

    cum_cf = []
    running = 0.0
    for v in cf:
        running += v
        cum_cf.append(running)
    pb_years = _pb(cum_cf)
    npv6 = sum(cf[t] / (1.06 ** t) for t in range(N_PERIODS))

    return {
        'irr_annual': irr_annual,
        'payback_years': pb_years,
        'npv6_wan_yuan': npv6,
        'total_invest_wan_yuan': TOTAL_INVEST * s['pv'],
        'self_use_ratio': self_use,
    }


# ============================================================
# Dify 入口
# ============================================================
def main(
    # 经济模型核心参数
    rooftop_pv_kwp: float = None, carport_pv_kwp: float = None,
    charging_station_count: float = None, charging_pile_count: float = None,
    storage_kw: float = None, operation_years: float = None,
    storage_capacity_kwh: float = None, solar_radiation: float = None,
    # 电价
    price_C19_pv_period: float = None, price_C20_normal_period: float = None,
    price_C21_public: float = None, price_C22_surplus_grid: float = None,
    surplus_ratio: float = None,
    # 充电桩
    charger_power: float = None, charger_hours=None, charger_util: float = None,
    charger_fee: float = None,
    # 充电棚
    station_power: float = None, station_interfaces: float = None,
    station_hours: float = None, station_util: float = None, station_fee: float = None,
    # VPP 覆写
    annual_hours: float = None, vpp_annual_hours: float = None,
    vpp_sunny_weight: float = None, vpp_cloudy_ratio: float = None,
    efficiency_coef: float = None, degradation_rate: float = None,
    annual_response_days: float = None,
    # 其他 VPP 可选
    dc_lighting_base_kw: float = None, dc_ac_base_kw: float = None,
    charging_base_kw: float = None, garage_lighting_base_kw: float = None,
    landscape_lighting_base_kw: float = None, ac_load_base_kw: float = None,
    lighting_flex_ratio: float = None, ac_flex_ratio: float = None,
    response_hours: str = None, compensation_price: float = None,
    market_price_scale: float = None, tou_price_scale: float = None,
    battery_throughput_cost: float = None, cycle_limit_per_day: float = None,
    dispatch_step_kwh: float = None,
    soc_min_ratio: float = None, soc_max_ratio: float = None, soc_init_ratio: float = None,
    charge_efficiency: float = None, discharge_efficiency: float = None,
    battery_power_kw: float = None, battery_capacity_kwh: float = None,
    **kwargs,
) -> dict:
    # ---- 安全转换所有输入 ----
    def sf(v, d):
        return _to_num(v, d)

    # 经济模型参数
    rp_pv = sf(rooftop_pv_kwp, 500.0)
    cp_pv = sf(carport_pv_kwp, 0.0)
    total_pv = rp_pv + cp_pv

    csc = max(int(sf(charging_station_count, 10)), 0)
    cpc = max(int(sf(charging_pile_count, 2620)), 0)
    skw = sf(storage_kw, 250.0)
    oy = max(int(sf(operation_years, 20)), 1)
    sc_kwh = sf(storage_capacity_kwh, 500.0)
    sr = sf(solar_radiation, 1183.7)

    c19 = sf(price_C19_pv_period, 0.70)
    c20 = sf(price_C20_normal_period, 0.65)
    c21 = sf(price_C21_public, 0.65)
    c22 = sf(price_C22_surplus_grid, 0.35)
    spr = sf(surplus_ratio, 0.20)
    if spr < 0 or spr > 1:
        spr = 0.20

    cpw = sf(charger_power, 7.0)
    # charger_hours 兼容 string 输入
    chrs = sf(charger_hours, 4.0) if not isinstance(charger_hours, str) else sf(float(charger_hours) if charger_hours else 4.0, 4.0)
    cu = sf(charger_util, 0.25)
    cfee = sf(charger_fee, 0.50)

    spw = sf(station_power, 3.0)
    si = max(int(sf(station_interfaces, 10)), 0)
    shrs = sf(station_hours, 4.0)
    su = sf(station_util, 0.30)
    sfee = sf(station_fee, 0.30)

    eff_coef = sf(efficiency_coef, 0.82)
    deg_rate = sf(degradation_rate, 0.01439498)

    # VPP 参数
    ah = sf(annual_hours, sf(vpp_annual_hours, 1100.0))
    vah = sf(vpp_annual_hours, ah)
    vsw = sf(vpp_sunny_weight, 0.55)
    vcr = sf(vpp_cloudy_ratio, 0.35)
    ard = sf(annual_response_days, 20.0)
    dc_l = sf(dc_lighting_base_kw, 60.0)
    dc_ac = sf(dc_ac_base_kw, 389.65)
    chg_b = sf(charging_base_kw, 148.0)
    gar_l = sf(garage_lighting_base_kw, 47.45)
    lnd_l = sf(landscape_lighting_base_kw, 15.0)
    ac_l = sf(ac_load_base_kw, 86.0)
    lfr = sf(lighting_flex_ratio, 0.30)
    afr = sf(ac_flex_ratio, 0.20)

    rh = response_hours if response_hours else '13'
    cpr = sf(compensation_price, 3.08)
    mps = sf(market_price_scale, 1.0)
    tps = sf(tou_price_scale, 1.0)
    btc = sf(battery_throughput_cost, 0.0525)
    cl = sf(cycle_limit_per_day, 2.0)
    dsk = sf(dispatch_step_kwh, max(skw / 8.0, 5.0))  # 粗粒度加速
    smin = sf(soc_min_ratio, 0.0)
    smax = sf(soc_max_ratio, 1.0)
    sinit = sf(soc_init_ratio, 0.5)
    ceff = sf(charge_efficiency, 1.0)
    deff = sf(discharge_efficiency, 1.0)
    bp = sf(battery_power_kw, skw)
    bc = sf(battery_capacity_kwh, sc_kwh)

    # ---- 构建 Params ----
    p = Params()
    p.rooftop_pv_kwp = rp_pv
    p.carport_pv_kwp = cp_pv
    p.charging_station_count = float(csc)
    p.charging_pile_count = float(cpc)
    p.storage_kw = skw
    p.operation_years = oy
    p.storage_capacity_kwh = sc_kwh
    p.solar_radiation = sr
    p.price_C19_pv_period = c19
    p.price_C20_normal_period = c20
    p.price_C21_public = c21
    p.price_C22_surplus_grid = c22
    p.surplus_ratio = spr
    p.charger_power = cpw
    p.charger_hours = chrs
    p.charger_util = cu
    p.charger_fee = cfee
    p.station_power = spw
    p.station_interfaces = si
    p.station_hours = shrs
    p.station_util = su
    p.station_fee = sfee
    p.efficiency_coef = eff_coef
    p.degradation_rate = deg_rate

    # ---- VPP 计算自消纳率（total_pv == 0 时跳过，直接返回 1.0）----
    if total_pv < 1.0:
        self_use = 1.0
        vpp_cap = 0.0
        vpp_sun_kwh = 0.0
        vpp_cld_kwh = 0.0
        vpp_hours = ah
    else:
        vpp_defaults = _build_vpp_defaults()
        vpp_overrides = {
            'pv': {
                'pv_design_power_kw': total_pv,
                'system_efficiency': eff_coef * 100.0,
                'annual_hours': vah,
                'sunny_weight': vsw,
                'cloudy_to_sunny_ratio': vcr,
            },
            'load': {
                'dc_lighting_base_kw': dc_l, 'dc_ac_base_kw': dc_ac,
                'charging_base_kw': chg_b, 'garage_lighting_base_kw': gar_l,
                'landscape_lighting_base_kw': lnd_l, 'ac_load_base_kw': ac_l,
            },
            'flexibility': {'lighting_flex_ratio': lfr, 'ac_flex_ratio': afr},
            'response': {
                'battery_capacity_kwh': bc, 'battery_power_kw': bp,
                'soc_min_ratio': smin, 'soc_max_ratio': smax, 'soc_init_ratio': sinit,
                'charge_efficiency': ceff, 'discharge_efficiency': deff,
                'cycle_limit_per_day': cl, 'battery_throughput_cost': btc,
                'response_hours': rh, 'compensation_price': cpr,
                'market_price_scale': mps, 'tou_price_scale': tps,
                'dispatch_step_kwh': dsk,
            },
            'carbon': {'annual_response_days': ard},
        }
        vpp_inputs = _merge_vpp_inputs(vpp_defaults, vpp_overrides)
        self_use = _calc_self_use_ratio(vpp_inputs)

        # 提取 VPP 中间结果
        pv_prof = _compute_pv_profile(vpp_inputs)
        ps = pv_prof['summary']
        vpp_cap = float(ps['installed_capacity_kw'])
        vpp_sun_kwh = float(ps['sunny_daily_energy_kwh'])
        vpp_cld_kwh = float(ps['cloudy_daily_energy_kwh'])
        vpp_hours = float(ps.get('vpp_annual_hours', ah))

    # ---- 经济模型 ----
    p.self_use_ratio = self_use
    econ = _run_economic(p, self_use)

    return {
        'self_use_ratio': round(self_use, 6),
        'irr_annual': round(econ['irr_annual'], 6),
        'payback_years': round(econ['payback_years'], 2) if econ['payback_years'] >= 0 else -1.0,
        'npv6_wan_yuan': round(econ['npv6_wan_yuan'], 0),
        'total_invest_wan_yuan': round(econ['total_invest_wan_yuan'], 2),
        'total_pv_kwp': round(p.total_pv_kwp, 2),
        'total_pv_mwp': round(p.total_pv_mwp, 3),
        'charging_pile_count': cpc,
        'charging_station_count': csc,
        'storage_kw': skw,
        'storage_capacity_kwh': sc_kwh,
        'operation_years': oy,
        'solar_radiation': sr,
        'vpp_installed_capacity_kw': round(vpp_cap, 2),
        'vpp_sunny_daily_pv_kwh': round(vpp_sun_kwh, 2),
        'vpp_cloudy_daily_pv_kwh': round(vpp_cld_kwh, 2),
        'vpp_annual_hours': round(vpp_hours, 2),
        'missing_params': '',
    }
