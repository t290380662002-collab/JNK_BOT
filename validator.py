# -*- coding: utf-8 -*-
"""
護照資料自動核對：台灣姓名拼音 + 年齡是否滿 21 歲。

用途：掃描證件或 /book 手打資料後，自動判斷
  1) 英文姓名拼音（台灣護照拼法：威妥瑪/通用拼音，如 CHIA）與中文姓名是否吻合；
     注意：此為台灣護照拼音，與大陸漢語拼音（如 jia）不同，請勿混淆。
  2) 出生日期推算是否滿 21 歲。
若有疑慮（拼音可能拼錯 / 未滿 21 歲）回傳提示清單，供 bot 顯示警告。

設計原則（避免誤報）：
  · 字典只收錄「常見字 -> 正確台灣拼音」；
  · 某字不在字典時「不驗證、不報錯」（最多漏檢，不會誤報）；
  · 僅當字典有該字、且其拼音清單與使用者輸入明顯不符時才提示。
"""
import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# 台灣姓名拼音表（威妥瑪 / 國語羅馬字第二式，即台灣護照常見拼法）
#   每個字對應「可能的正確拼音」清單（同字多音則全部列出）。
#   繁簡同形字直接收錄；常見簡繁對應另以 SIMP_TO_TRAD 補齊查表。
# ---------------------------------------------------------------------------
_CHAR_PINYIN = {
    # ===== 常見姓氏 =====
    "王": ["WANG"], "李": ["LI"], "張": ["CHANG"], "劉": ["LIU"], "陈": ["CHEN"],
    "陳": ["CHEN"], "杨": ["YANG"], "楊": ["YANG"], "黃": ["HUANG"], "黄": ["HUANG"],
    "趙": ["CHAO"], "赵": ["CHAO"], "吳": ["WU"], "周": ["CHOU"], "徐": ["HSU"],
    "孫": ["SUN"], "胡": ["HU"], "朱": ["CHU"], "高": ["KAO"], "林": ["LIN"],
    "何": ["HO"], "郭": ["KUO"], "馬": ["MA"], "马": ["MA"], "羅": ["LO"], "罗": ["LO"],
    "梁": ["LIANG"], "宋": ["SUNG"], "鄭": ["CHENG"], "郑": ["CHENG"], "謝": ["HSIEH"],
    "谢": ["HSIEH"], "韓": ["HAN"], "韩": ["HAN"], "唐": ["TANG"], "馮": ["FENG"],
    "冯": ["FENG"], "于": ["YU"], "董": ["TUNG"], "蕭": ["HSIAO"], "萧": ["HSIAO"],
    "程": ["CHENG"], "曹": ["TSAO"], "袁": ["YUAN"], "鄧": ["TENG"], "邓": ["TENG"],
    "許": ["HSU"], "许": ["HSU"], "傅": ["FU"], "沈": ["SHEN"], "曾": ["TZENG"],
    "彭": ["PENG"], "呂": ["LU"], "蘇": ["SU"], "苏": ["SU"], "盧": ["LU"], "卢": ["LU"],
    "蔣": ["CHIANG"], "蒋": ["CHIANG"], "蔡": ["TSAI"], "廖": ["LIAO"], "余": ["YU"],
    "賴": ["LAI"], "赖": ["LAI"], "賈": ["CHIA"], "潘": ["PAN"], "葉": ["YEH"],
    "叶": ["YEH"], "鐘": ["CHUNG"], "钟": ["CHUNG"], "莊": ["CHUANG"], "庄": ["CHUANG"],
    "江": ["CHIANG"], "杜": ["TU"], "阮": ["JUAN"], "藍": ["LAN"], "蓝": ["LAN"],
    "簡": ["CHIEN"], "简": ["CHIEN"], "卓": ["CHO"], "詹": ["CHAN"], "游": ["YU"],
    "古": ["KU"], "夏": ["HSIA"], "方": ["FANG"], "洪": ["HUNG"], "邱": ["CHIU"],
    "汪": ["WANG"], "田": ["TIEN"], "毛": ["MAO"], "顧": ["KU"], "顾": ["KU"],
    "孟": ["MENG"], "史": ["SHIH"], "范": ["FAN"], "倪": ["NI"], "湯": ["TANG"],
    "溫": ["WEN"], "温": ["WEN"], "莫": ["MO"], "易": ["YI"], "戴": ["TAI"],
    "紀": ["CHI"], "纪": ["CHI"], "歐": ["OU"], "歐陽": ["OUYANG"], "司馬": ["SZE-MA", "SMA"],
    "石": ["SHIH"], "熊": ["HSIUNG"], "龔": ["KUNG"], "龚": ["KUNG"], "嚴": ["YEN"],
    "严": ["YEN"], "秦": ["CHIN"], "侯": ["HOU"], "邵": ["SHAO"],
    "尹": ["YIN"], "黎": ["LI"], "錢": ["CHIEN"], "钱": ["CHIEN"], "譚": ["TAN"],
    "谭": ["TAN"], "鄒": ["TSOU"], "邹": ["TSOU"], "白": ["PAI"],
    "項": ["HSIANG"], "柳": ["LIU"], "章": ["CHANG"], "俞": ["YU"],
    "岳": ["YUEH"], "伍": ["WU"], "金": ["CHIN"], "元": ["YUAN"],
    # ===== 常見名字用字 =====
    "家": ["CHIA"], "妏": ["WEN", "YUN"], "婷": ["TING"], "怡": ["YI"], "君": ["CHUN"],
    "慧": ["HUI"], "雅": ["YA"], "琪": ["CHI"], "偉": ["WEI"], "明": ["MING"],
    "俊": ["CHUN"], "軒": ["HSUAN"], "涵": ["HAN"], "宇": ["YU"], "萱": ["HSUAN"],
    "惠": ["HUI"], "淑": ["SHU"], "芬": ["FEN"], "芳": ["FANG"], "莉": ["LI"],
    "娟": ["CHUAN"], "玲": ["LING"], "珍": ["CHEN"], "珠": ["CHU"],
    "嘉": ["CHIA"], "欣": ["HSUN"], "安": ["AN"], "雯": ["WEN"],
    "怡": ["YI"], "妤": ["YU"], "盈": ["YING"], "恩": ["EN"], "宣": ["HSUAN"],
    "庭": ["TING"], "柏": ["PO"], "翰": ["HAN"], "維": ["WEI"], "哲": ["CHE"],
    "儒": ["JU"], "均": ["CHUN"], "宜": ["YI"], "靜": ["CHING"], "穎": ["YING"],
    "蓉": ["JUNG"], "琳": ["LIN"], "琪": ["CHI"], "薇": ["WEI"], "萍": ["PING"],
    "珊": ["SHAN"], "倩": ["CHIEN"], "佩": ["PEI"], "潔": ["CHIEH"], "翊": ["YI"],
    "淳": ["CHUN"], "希": ["HSI"], "岑": ["TSEN"], "筑": ["CHU"], "沛": ["PEI"],
    "凱": ["KAI"], "翔": ["HSIANG"], "傑": ["CHIEH"], "廷": ["TING"], "佑": ["YU"],
    "承": ["CHENG"], "冠": ["KUAN"], "弘": ["HUNG"], "志": ["CHIH"], "宗": ["TSUNG"],
    "奕": ["YI"], "妍": ["YEN"], "璇": ["HSUAN"], "筠": ["YUN"], "綺": ["CHI"],
    "妘": ["YUN"], "芯": ["HSIN"], "慈": ["TZU"], "嫚": ["MAN"], "露": ["LU"],
    "曉": ["HSIAO"], "晓": ["HSIAO"], "傑": ["CHIEH"], "傑": ["CHIEH"],
}

# 簡體字 -> 繁體字（僅收錄與拼音有關、且繁簡不同形的常見字）
_SIMP_TO_TRAD = {
    "陈": "陳", "杨": "楊", "黄": "黃", "赵": "趙", "吴": "吳", "刘": "劉",
    "罗": "羅", "萧": "蕭", "郑": "鄭", "谢": "謝", "韩": "韓", "冯": "馮",
    "苏": "蘇", "卢": "盧", "蒋": "蔣", "赖": "賴", "蓝": "藍",
    "简": "簡", "叶": "葉", "钟": "鐘", "庄": "莊", "邓": "鄧", "许": "許",
    "汤": "湯", "温": "溫", "严": "嚴", "钱": "錢", "谭": "譚", "邹": "鄒",
    "顾": "顧", "马": "馬", "伟": "偉", "龙": "龍", "国": "國",
}

# 複姓（取前兩字為姓）
_DOUBLE_SURNAMES = {"歐陽", "司馬", "上官", "諸葛", "東方", "獨孤", "慕容", "長孫", "宇文", "尉遲"}


def _char_pinyin(ch: str):
    """回傳該字可能的正確台灣拼音清單（大寫）；無資料回 None。"""
    if not ch:
        return None
    if ch in _CHAR_PINYIN:
        return [p.upper() for p in _CHAR_PINYIN[ch]]
    trad = _SIMP_TO_TRAD.get(ch)
    if trad and trad in _CHAR_PINYIN:
        return [p.upper() for p in _CHAR_PINYIN[trad]]
    return None


def _split_zh(zh: str):
    """拆中文姓名為 (姓, 名)。預設首字為姓（複姓例外）。"""
    zh = (zh or "").strip()
    if len(zh) <= 1:
        return zh, ""
    if zh[:2] in _DOUBLE_SURNAMES:
        return zh[:2], zh[2:]
    return zh[0], zh[1:]


def _split_en(en: str):
    """英文姓名 -> (姓段, [名各段])。支援 'LIN,CHTA-WEN' / 'LIN CHTA WEN'。"""
    en = (en or "").replace("，", ",").replace("、", ",").strip()
    if not en:
        return "", []
    if "," in en:
        sur, given = en.split(",", 1)
    else:
        parts = [p for p in re.split(r"[\s\-]+", en) if p]
        sur = parts[0] if parts else ""
        given = " ".join(parts[1:]) if len(parts) > 1 else ""
    sur = sur.strip().upper()
    given_segs = [s.strip().upper() for s in re.split(r"[\s\-]+", given) if s.strip()]
    return sur, given_segs


def validate_name(zh: str, en: str):
    """比對中文姓名與英文拼音，回傳拼音可能有誤的提示清單。"""
    warns = []
    if not zh or not en:
        return warns
    z_sur, z_given = _split_zh(zh)
    e_sur, e_given = _split_en(en)

    # 姓
    sp = _char_pinyin(z_sur)
    if sp and e_sur and e_sur not in sp:
        warns.append(
            f"姓「{z_sur}」的拼音可能不是 {e_sur}（台灣護照拼法為 {'/'.join(sp)}，與大陸漢語拼音不同）"
        )

    # 名：逐字對逐段
    chars = list(z_given)
    for i, ch in enumerate(chars):
        seg = e_given[i] if i < len(e_given) else None
        gp = _char_pinyin(ch)
        if gp and seg and seg not in gp:
            warns.append(
                f"名字「{ch}」的拼音可能不是 {seg}（台灣護照拼法為 {'/'.join(gp)}，與大陸漢語拼音不同）"
            )
    return warns


def _parse_date(d):
    """'YYYY-MM-DD' / 'YYYY/MM/DD' / datetime -> date；失敗回 None。"""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if not isinstance(d, str):
        return None
    s = d.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def validate_age(dob):
    """推算年齡，未滿 21 歲回傳提示清單（含一筆警告）。"""
    warns = []
    d = _parse_date(dob)
    if d:
        today = date.today()
        age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        if age < 21:
            warns.append(
                f"⚠️ 入住者未滿 21 歲（出生 {d.strftime('%Y/%m/%d')}，現齡約 {age} 歲）"
            )
    return warns


def validate_guest(zh=None, en=None, dob=None):
    """統一入口：回傳該入住者的所有核對提示（無錯誤回傳空清單）。"""
    return validate_name(zh or "", en or "") + validate_age(dob)


if __name__ == "__main__":
    # 自測
    cases = [
        ("林家妏", "LIN,CHTA-WEN", "2000-05-18"),  # 名字「家」拼錯 CHTA vs CHIA
        ("林家妏", "LIN,CHIA-WEN", "2000-05-18"),  # 全對
        ("林家妏", "LIN,CHIA-WEN", "2010-05-18"),  # 未滿 21
        ("吳俊", "WU,JUN", "1981-07-08"),            # 全對
    ]
    for zh, en, dob in cases:
        print(f"[{zh} | {en} | {dob}] -> {validate_guest(zh, en, dob)}")
