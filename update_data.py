import os
import sys
import json
import requests
from datetime import datetime, timedelta

# ==========================================
# [필수 입력] 공공데이터포털 일반 인증키(Decoding)를 입력하세요
# ==========================================
API_KEY = "ee77101d0a1cb46a6be4fa95573a05fcf1c9331fbe8f3d3cb8e921f5b66fedfb"

def fetch_krx_market_data():
    print("[시스템] 공공데이터포털(금융위) API 연동을 시작합니다...")
    print("= [바이브 프로 퀀트 플랫폼] 무적 엔진 가동 =")
    
    url = "http://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
    
    items = []
    # 최신 영업일 데이터를 찾기 위해 오늘부터 역산하며 호출 시도
    for i in range(7):
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        
        params = {
            "serviceKey": API_KEY,
            "resultType": "json",
            "numOfRows": "1000",     # 코스피 종목을 넉넉히 수용
            "pageNo": "1",
            "mrktCls": "KOSPI",
            "basDt": target_date     # 야간/주말 호출을 위해 날짜 명시 필수
        }
        
        try:
            response = requests.get(url, params=params, timeout=15)
            data = response.json()
            fetched_items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            
            if fetched_items:
                items = fetched_items
                print(f"✅ [성공] {target_date} 기준 영업일 데이터 조회 성공!")
                break
        except Exception:
            continue

    if not items:
        print("⚠️ [경고] 최근 7일간의 데이터 로딩에 실패했습니다.")
        return False
        
    print(f"✅ [성공] 국토 데이터 수집 완료! 종목 수: {len(items)}개")
    
    # index.html 변수 규격과 100% 동기화하는 패키징 공정
    processed_data = []
    for item in items:
        # 공공 API에서 문자열로 오는 가치지표들을 안전하게 숫자형으로 정제
        try:
            clpr_val = int(item.get("clpr", 0)) if item.get("clpr") else 0
            flt_val = float(item.get("fltRt", 0.0)) if item.get("fltRt") else 0.0
            
            # API 제공 항목 매핑 (제공되지 않는 항목은 0.0 처리하여 스크립트 오류 방지)
            pbr_val = float(item.get("pbr", 0.0)) if item.get("pbr") else 0.0
            per_val = float(item.get("per", 0.0)) if item.get("per") else 0.0
            eps_val = float(item.get("eps", 0.0)) if item.get("eps") else 0.0
            
            processed_data.append({
                "code": item.get("srtnCd", ""),
                "name": item.get("itmsNm", ""),
                "close": clpr_val,
                "fltRt": flt_val,
                "pbr": pbr_val,
                "per": per_val,
                "eps": eps_val
            })
        except Exception:
            continue
        
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 프론트엔드가 다이렉트로 로딩할 수 있게 구조화
    js_content = f"const quantData = {json.dumps(processed_data, ensure_ascii=False, indent=2)};\n"
    js_content += f"const lastUpdated = '{now_str}';\n"
    
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(js_content)
        
    print("▶ 웹앱 전용 데이터 패키징 완료.")
    return True

def deploy_to_github():
    print("🚀 [배포 시작] 깃허브 원격 저장소 동기화 중...")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(current_dir)
    
    os.system("git add data.js")
    os.system('git commit -m "🤖 [자동화] 무적 API 규격 정렬 퀀트 배포"')
    res = os.system("git push origin main")
    
    if res == 0:
        print("🚀 [배포 성공] 깃허브 원격 저장소 동기화 완료!")
        print("========================================================================")
        print("[완료] 모든 자동화 프로세스가 마감되었습니다. 창을 닫으셔도 됩니다.")
        print("========================================================================")
    else:
        print("❌ 깃허브 업로드 중 오류가 발생했습니다.")

if __name__ == "__main__":
    if fetch_krx_market_data():
        deploy_to_github()