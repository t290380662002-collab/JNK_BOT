"""
MRZ 解析器單元測試（純 Python，不需 Tesseract）。
執行：python test_mrz.py
"""
from mrz_parser import parse

CASES = [
    {
        "name": "護照 TD3",
        "lines": [
            "P<GBRSTEPHENS<<JOHN<<MR<MICHAEL" + "<" * 12,
            "GBN123456<GBR8501018M2501018" + "<" * 15 + "0",
        ],
        "expect": {
            "doc_type_guess": "護照",
            "last_name": "STEPHENS",
            "first_name": "JOHN MR MICHAEL",
            "doc_number": "GBN123456",
            "nationality": "英國",
            "date_of_birth": "1985-01-01",
            "sex": "M",
            "expiry_date": "2025-01-01",
        },
    },
    {
        "name": "卡式 TD1（港澳通行證/回鄉證/台胞證）",
        "lines": [
            "I<CHN123456789<" + "<" * 15,
            "8501014M2501018CHN" + "<" * 11 + "0",
            "SURNAME<<GIVENNAME",
        ],
        "expect": {
            "doc_type_guess": "卡式證件",
            "last_name": "SURNAME",
            "first_name": "GIVENNAME",
            "doc_number": "123456789",
            "nationality": "中國",
            "date_of_birth": "1985-01-01",
            "sex": "M",
            "expiry_date": "2025-01-01",
        },
    },
    {
        "name": "卡式 TD1（OCR 三行黏成一行）",
        "lines": [
            "I<CHNCG416393283<412086<050316"
            + "0503165M2412098CHN<<<<<<<<<<<<"
            + "LI<<HONGDA<<<<<<<<<<<<<<<<<<<<",
        ],
        "expect": {
            "doc_type_guess": "卡式證件",
            "last_name": "LI",
            "first_name": "HONGDA",
            "doc_number": "CG4163932",
            "nationality": "中國",
            "date_of_birth": "2005-03-16",
            "sex": "M",
            "expiry_date": "2024-12-09",
        },
    },
]


def run():
    ok = 0
    for case in CASES:
        got = parse(case["lines"])
        print(f"\n=== {case['name']} ===")
        print("解析結果：", got)
        if not got:
            print("❌ 解析回傳 None")
            continue
        failed = False
        for k, v in case["expect"].items():
            if got.get(k) != v:
                print(f"❌ 欄位 {k} 期望 {v!r}，實際 {got.get(k)!r}")
                failed = True
        if not failed:
            print("✅ 通過")
            ok += 1
    print(f"\n結果：{ok}/{len(CASES)} 通過")


if __name__ == "__main__":
    run()
