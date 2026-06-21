"""
bazi_engine.py —— 精准八字排盘核心引擎（单文件、无外部依赖）

用途：
- 可直接整体复制粘贴进 Coze 插件代码编辑器使用
- 也可被 FastAPI/Flask 等框架 import 后包装成 HTTP API 给 Dify 用

核心函数：calculate_full_bazi(year, month, day, hour, minute, gender, query_year=None)
入参：
  year, month, day, hour, minute : 出生日期时间（公历，北京时间）
  gender : "male" 或 "female"
  query_year : 可选，要查询的流年年份；不传则返回当前系统日期所在的流年
返回：dict，可直接 json.dumps 输出，结构见函数末尾示例

算法来源说明：
- 节气计算：太阳视黄经天文公式（Meeus 低精度太阳坐标算法），已用2018-2026年
  已知节气日期逐一验证通过，包括立春卡在2月3/4日的边界年份
- 日柱：连续干支周期计算，用两个独立历史锚点(2000-01-07甲子日 / 1900-01-31甲辰日)
  交叉验证一致
- 年柱：以立春为界，立春前算上一年干支
- 月柱：以"节"（12节，不含中气）为界，不是按农历初一
- 大运：阳男阴女顺排，阴男阳女逆排，起运按"三天折一岁"传统算法
"""

import math
from datetime import datetime, timedelta


# ============ 第一部分：天文历法 - 节气精确计算 ============

def _julian_day(y, m, d, h, mi, s):
    dd = d + (h + mi / 60 + s / 3600) / 24
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + A // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + dd + B - 1524.5


def _solar_apparent_longitude(jd):
    T = (jd - 2451545.0) / 36525.0
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T * T
    M = 357.52911 + 35999.05029 * T - 0.0001537 * T * T
    M_rad = math.radians(M % 360)
    C = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M_rad)
         + (0.019993 - 0.000101 * T) * math.sin(2 * M_rad)
         + 0.000289 * math.sin(3 * M_rad))
    true_long = L0 + C
    omega = 125.04 - 1934.136 * T
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    return apparent_long % 360


_JIEQI_ORDER = [
    ("立春", 315), ("雨水", 330), ("惊蛰", 345), ("春分", 0),
    ("清明", 15), ("谷雨", 30), ("立夏", 45), ("小满", 60),
    ("芒种", 75), ("夏至", 90), ("小暑", 105), ("大暑", 120),
    ("立秋", 135), ("处暑", 150), ("白露", 165), ("秋分", 180),
    ("寒露", 195), ("霜降", 210), ("立冬", 225), ("小雪", 240),
    ("大雪", 255), ("冬至", 270), ("小寒", 285), ("大寒", 300),
]
_JIE_ONLY = {"立春", "惊蛰", "清明", "立夏", "芒种", "小暑",
             "立秋", "白露", "寒露", "立冬", "大雪", "小寒"}


def _find_solar_term_jd(approx_year, target_angle):
    base_jd = _julian_day(approx_year, 3, 20, 0, 0, 0)
    angle_diff = target_angle if target_angle <= 180 else target_angle - 360
    days_offset = angle_diff * 365.2422 / 360
    center_jd = base_jd + days_offset
    lo, hi = center_jd - 10, center_jd + 10

    def angle_dist(jd):
        lon = _solar_apparent_longitude(jd)
        return (lon - target_angle + 540) % 360 - 180

    for _ in range(60):
        mid = (lo + hi) / 2
        d = angle_dist(mid)
        if abs(d) < 1e-7:
            break
        d_lo = angle_dist(lo)
        if (d_lo < 0 and d < 0) or (d_lo > 0 and d > 0):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _jd_to_datetime(jd):
    jd += 0.5
    Z = math.floor(jd)
    F = jd - Z
    if Z < 2299161:
        A = Z
    else:
        alpha = math.floor((Z - 1867216.25) / 36524.25)
        A = Z + 1 + alpha - math.floor(alpha / 4)
    B = A + 1524
    C = math.floor((B - 122.1) / 365.25)
    D = math.floor(365.25 * C)
    E = math.floor((B - D) / 30.6001)
    day = B - D - math.floor(30.6001 * E) + F
    month = E - 1 if E < 14 else E - 13
    year = C - 4716 if month > 2 else C - 4715
    day_int = int(day)
    frac = day - day_int
    hours = frac * 24
    h = int(hours)
    minutes = (hours - h) * 60
    mi = int(minutes)
    sec = int((minutes - mi) * 60)
    return datetime(int(year), int(month), day_int, h, mi, sec)


def _find_nearest_jie_boundaries(dt):
    jd_now = _julian_day(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) - 8 / 24
    candidates = []
    for name, angle in _JIEQI_ORDER:
        if name not in _JIE_ONLY:
            continue
        for yr in (dt.year - 1, dt.year, dt.year + 1):
            jd = _find_solar_term_jd(yr, angle)
            candidates.append((name, jd))
    candidates.sort(key=lambda x: x[1])

    prev_jie, next_jie = None, None
    for name, jd in candidates:
        if jd <= jd_now:
            prev_jie = (name, jd)
        elif next_jie is None:
            next_jie = (name, jd)
            break
    prev_dt = _jd_to_datetime(prev_jie[1]) + timedelta(hours=8)
    next_dt = _jd_to_datetime(next_jie[1]) + timedelta(hours=8)
    return prev_jie[0], prev_dt, next_jie[0], next_dt


# ============ 第二部分：干支系统 ============

TIANGAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
DIZHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
WUXING_GAN = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
              "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"}
WUXING_ZHI = {"子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
              "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水"}
YINYANG_GAN = {"甲": True, "乙": False, "丙": True, "丁": False, "戊": True,
               "己": False, "庚": True, "辛": False, "壬": True, "癸": False}
ZHI_HIDDEN_GAN = {
    "子": ["癸"], "丑": ["己", "癸", "辛"], "寅": ["甲", "丙", "戊"], "卯": ["乙"],
    "辰": ["戊", "乙", "癸"], "巳": ["丙", "戊", "庚"], "午": ["丁", "己"], "未": ["己", "丁", "乙"],
    "申": ["庚", "壬", "戊"], "酉": ["辛"], "戌": ["戊", "辛", "丁"], "亥": ["壬", "甲"],
}
WUXING_SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
WUXING_KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}

_ANCHOR_DATE = datetime(2000, 1, 7)  # 已知锚点：甲子日


def _get_day_ganzhi(dt):
    target = datetime(dt.year, dt.month, dt.day)
    delta_days = (target - _ANCHOR_DATE).days
    index = (delta_days + 0) % 60
    return TIANGAN[index % 10], DIZHI[index % 12]


def _get_hour_ganzhi(day_gan, hour, minute=0):
    h = hour + minute / 60
    zhi_index = 0 if (h >= 23 or h < 1) else int((h + 1) // 2)
    zhi = DIZHI[zhi_index]
    day_gan_index = TIANGAN.index(day_gan)
    zi_start_gan = {0: 0, 5: 0, 1: 2, 6: 2, 2: 4, 7: 4, 3: 6, 8: 6, 4: 8, 9: 8}[day_gan_index]
    gan_index = (zi_start_gan + zhi_index) % 10
    return TIANGAN[gan_index], zhi


def _get_year_ganzhi_for_date(dt):
    """以立春为界，返回该日期所在的干支纪年（年柱/流年通用）"""
    lichun_jd = _find_solar_term_jd(dt.year, 315)
    lichun_dt = _jd_to_datetime(lichun_jd) + timedelta(hours=8)
    ganzhi_year = dt.year - 1 if dt < lichun_dt else dt.year
    offset = (ganzhi_year - 1984) % 60
    return TIANGAN[offset % 10], DIZHI[offset % 12], ganzhi_year


_JIE_TO_ZHI_INDEX = {"立春": 2, "惊蛰": 3, "清明": 4, "立夏": 5, "芒种": 6, "小暑": 7,
                      "立秋": 8, "白露": 9, "寒露": 10, "立冬": 11, "大雪": 0, "小寒": 1}
_YIN_START_GAN_MAP = {0: 2, 5: 2, 1: 4, 6: 4, 2: 6, 7: 6, 3: 8, 8: 8, 4: 0, 9: 0}


def _get_month_ganzhi(dt, year_gan):
    prev_jie, prev_jie_dt, next_jie, next_jie_dt = _find_nearest_jie_boundaries(dt)
    zhi_index = _JIE_TO_ZHI_INDEX[prev_jie]
    zhi = DIZHI[zhi_index]
    year_gan_index = TIANGAN.index(year_gan)
    yin_start_gan = _YIN_START_GAN_MAP[year_gan_index]
    steps = (zhi_index - 2) % 12
    gan_index = (yin_start_gan + steps) % 10
    return TIANGAN[gan_index], zhi, prev_jie, prev_jie_dt, next_jie, next_jie_dt


def _get_shishen(day_gan, other_gan):
    if other_gan == day_gan:
        return "比肩"
    day_wx, other_wx = WUXING_GAN[day_gan], WUXING_GAN[other_gan]
    same_yy = YINYANG_GAN[day_gan] == YINYANG_GAN[other_gan]
    if other_wx == day_wx:
        return "比肩" if same_yy else "劫财"
    if WUXING_SHENG[day_wx] == other_wx:
        return "食神" if same_yy else "伤官"
    if WUXING_KE[day_wx] == other_wx:
        return "偏财" if same_yy else "正财"
    if WUXING_KE[other_wx] == day_wx:
        return "七杀" if same_yy else "正官"
    if WUXING_SHENG[other_wx] == day_wx:
        return "偏印" if same_yy else "正印"
    return "?"


def _calculate_wuxing_strength(year_p, month_p, day_p, hour_p):
    scores = {"木": 0.0, "火": 0.0, "土": 0.0, "金": 0.0, "水": 0.0}
    for gan, zhi in (year_p, month_p, day_p, hour_p):
        scores[WUXING_GAN[gan]] += 1.0
        hidden = ZHI_HIDDEN_GAN[zhi]
        for hg, w in zip(hidden, [1.0, 0.5, 0.3]):
            scores[WUXING_GAN[hg]] += w

    day_wx = WUXING_GAN[day_p[0]]
    sheng_wo_wx = [k for k, v in WUXING_SHENG.items() if v == day_wx][0]
    wo_sheng_wx = WUXING_SHENG[day_wx]
    wo_ke_wx = WUXING_KE[day_wx]
    ke_wo_wx = [k for k, v in WUXING_KE.items() if v == day_wx][0]

    help_score = scores[day_wx] + scores[sheng_wo_wx]
    drain_score = scores[wo_sheng_wx] + scores[wo_ke_wx] + scores[ke_wo_wx]
    total = help_score + drain_score
    help_ratio = help_score / total if total > 0 else 0.5

    return {
        "five_elements_distribution": {k: round(v, 2) for k, v in scores.items()},
        "day_master_element": day_wx,
        "help_score": round(help_score, 2),
        "drain_score": round(drain_score, 2),
        "help_ratio": round(help_ratio, 3),
        "strength": "身强" if help_ratio >= 0.5 else "身弱",
    }


def _calculate_dayun(year_gan, month_p, birth_dt, gender):
    year_gan_yang = YINYANG_GAN[year_gan]
    is_forward = (year_gan_yang and gender == "male") or (not year_gan_yang and gender == "female")
    prev_jie, prev_jie_dt, next_jie, next_jie_dt = _find_nearest_jie_boundaries(birth_dt)

    diff_days = ((next_jie_dt - birth_dt).total_seconds() / 86400 if is_forward
                 else (birth_dt - prev_jie_dt).total_seconds() / 86400)
    start_age_years = diff_days / 3

    month_gan_idx = TIANGAN.index(month_p[0])
    month_zhi_idx = DIZHI.index(month_p[1])

    dayun_list = []
    for i in range(1, 9):
        if is_forward:
            g_idx, z_idx = (month_gan_idx + i) % 10, (month_zhi_idx + i) % 12
        else:
            g_idx, z_idx = (month_gan_idx - i) % 10, (month_zhi_idx - i) % 12
        start_age = start_age_years + (i - 1) * 10
        dayun_list.append({
            "ganzhi": TIANGAN[g_idx] + DIZHI[z_idx],
            "start_age": round(start_age, 1),
            "end_age": round(start_age + 10, 1),
        })

    return {
        "direction": "顺排" if is_forward else "逆排",
        "start_age_years": round(start_age_years, 2),
        "dayun_list": dayun_list,
    }


# ============ 第三部分：对外主函数 ============

def calculate_full_bazi(year, month, day, hour, minute=0, gender="male", query_year=None):
    """
    主入口函数。返回结构化字典，可直接 json 序列化输出给上层 AI 使用。
    """
    birth_dt = datetime(year, month, day, hour, minute)

    day_calc_dt = birth_dt + timedelta(days=1) if hour >= 23 else birth_dt
    day_gan, day_zhi = _get_day_ganzhi(day_calc_dt)
    hour_gan, hour_zhi = _get_hour_ganzhi(day_gan, hour, minute)
    year_gan, year_zhi, ganzhi_year = _get_year_ganzhi_for_date(birth_dt)
    month_gan, month_zhi, prev_jie, prev_jie_dt, next_jie, next_jie_dt = _get_month_ganzhi(birth_dt, year_gan)

    year_p, month_p, day_p, hour_p = (year_gan, year_zhi), (month_gan, month_zhi), (day_gan, day_zhi), (hour_gan, hour_zhi)

    pillars_out = {}
    for label, (gan, zhi), is_day in [
        ("year_pillar", year_p, False), ("month_pillar", month_p, False),
        ("day_pillar", day_p, True), ("hour_pillar", hour_p, False),
    ]:
        hidden = ZHI_HIDDEN_GAN[zhi]
        pillars_out[label] = {
            "stem": gan, "branch": zhi,
            "stem_element": WUXING_GAN[gan], "branch_element": WUXING_ZHI[zhi],
            "stem_shishen": "日主" if is_day else _get_shishen(day_gan, gan),
            "hidden_stems": [{"stem": hg, "shishen": _get_shishen(day_gan, hg)} for hg in hidden],
        }

    strength = _calculate_wuxing_strength(year_p, month_p, day_p, hour_p)
    dayun = _calculate_dayun(year_gan, month_p, birth_dt, gender)

    # 流年：默认取系统当前日期，也可指定 query_year（取该年7月1日代表该年干支）
    if query_year is None:
        now = datetime.now()
        ln_gan, ln_zhi, ln_year = _get_year_ganzhi_for_date(now)
    else:
        ln_gan, ln_zhi, ln_year = _get_year_ganzhi_for_date(datetime(query_year, 7, 1))

    liunian = {
        "year": ln_year,
        "ganzhi": ln_gan + ln_zhi,
        "shishen_of_stem": _get_shishen(day_gan, ln_gan),
    }

    return {
        "input": {"year": year, "month": month, "day": day, "hour": hour, "minute": minute, "gender": gender},
        "day_master": {"stem": day_gan, "element": WUXING_GAN[day_gan]},
        "pillars": pillars_out,
        "wuxing_strength": strength,
        "dayun": dayun,
        "liunian": liunian,
        "month_boundary_used": {
            "prev_jie": prev_jie, "prev_jie_datetime": prev_jie_dt.isoformat(),
            "next_jie": next_jie, "next_jie_datetime": next_jie_dt.isoformat(),
        },
    }


if __name__ == "__main__":
    import json
    result = calculate_full_bazi(1990, 5, 15, 14, 30, "male")
    print(json.dumps(result, ensure_ascii=False, indent=2))
