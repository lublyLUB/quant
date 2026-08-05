"""장 마감 직후 종가만 가볍게 갱신하는 스크립트.

새벽 3시 update_data.py(재무제표까지 포함하는 무거운 전체 배치)와 별개로, 장 마감 직후
저녁에 한 번 더 실행해 data.js의 price/market_cap 필드만 당일 종가로 교체한다.
PBR/PER 등 가격 기반 비율과 순위는 재계산하지 않는다 - 재무제표(DART)는 인트라데이에
바뀌지 않으므로, 다음날 새벽 배치 때 함께 갱신된다.
"""
import json
import os
import sys
from datetime import datetime

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import kiwoom_api

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JS_PATH = os.path.join(BASE_DIR, "data.js")
PACKAGE_PREFIX = "const KOSPI_QUANT_PACKAGE = "

# data.js에 있는 종목 배열 키 전부 - 각 항목이 code/price/market_cap 필드를 갖는다.
STOCK_ARRAY_KEYS = [
    "super_value", "super_value_smallcap", "super_value_smallcap30",
    "momentum_value", "momentum_value_smallcap", "momentum_value_smallcap30",
    "fscore_value", "quality_value", "fama_value",
    "super_quality_value", "value_momentum_value", "quality_momentum_value",
    "ultra_value", "ultra_value_smallcap", "ultra_value_smallcap30",
    "ncav_value",
]


def _load_package():
    with open(DATA_JS_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.startswith(PACKAGE_PREFIX):
        raise RuntimeError("data.js 형식이 예상과 다릅니다.")
    body = content[len(PACKAGE_PREFIX):].rstrip().rstrip(";")
    return json.loads(body)


def _save_package(package):
    js_content = f"{PACKAGE_PREFIX}{json.dumps(package, ensure_ascii=False, indent=4)};\n"
    with open(DATA_JS_PATH, "w", encoding="utf-8") as f:
        f.write(js_content)


def update_closing_prices():
    package = _load_package()
    flags = kiwoom_api.get_stock_list_flags()  # {code: {price, market_cap(원 단위), ...}}

    updated = 0
    missing_codes = set()
    for key in STOCK_ARRAY_KEYS:
        for s in package.get(key) or []:
            code = s.get("code")
            flag = flags.get(code) if code else None
            if not flag or not flag.get("price"):
                if code:
                    missing_codes.add(code)
                continue
            s["price"] = flag["price"]
            s["market_cap"] = int(flag["market_cap"] / 100_000_000)  # 억 단위
            updated += 1

    package.setdefault("server", {})["price_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_package(package)
    print(f"✅ 종가 갱신 완료: {updated}건 반영, 조회 실패 {len(missing_codes)}종목")
    return updated, len(missing_codes)


def deploy_to_github():
    print("🚀 [배포 시작] 깃허브 원격 저장소 동기화 중...")
    os.chdir(BASE_DIR)
    os.system("git add data.js")
    os.system('git commit -m "🤖 [자동화] 장마감 종가 갱신"')
    res = os.system("git push origin main")
    if res == 0:
        print("🚀 [배포 성공] 깃허브 원격 저장소 동기화 완료!")
    else:
        print("❌ 깃허브 업로드 중 오류가 발생했습니다.")
    return res == 0


if __name__ == "__main__":
    skip_deploy = "--no-deploy" in sys.argv
    update_closing_prices()
    if not skip_deploy:
        deploy_to_github()
