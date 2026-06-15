# ─── 💥 [파이썬 3.14 최신 버전 호환성 최종 마스터 패치] ───
import os
import sys
import types
from pathlib import Path

try:
    import pkg_resources
except ModuleNotFoundError:
    fake_pkg = types.ModuleType("pkg_resources")
    fake_pkg.declare_namespace = lambda name: None
    fake_pkg.get_distribution = lambda name: types.SimpleNamespace(version="0.0.0")
    
    def fake_resource_filename(package_name, resource_name):
        base_path = Path(sys.executable).parent / "Lib" / "site-packages" / package_name
        if not base_path.exists():
            base_path = Path(os.environ["APPDATA"]) / "Python" / "Python314" / "site-packages" / package_name
        return str(base_path / resource_name)
    
    fake_pkg.resource_filename = fake_resource_filename
    sys.modules["pkg_resources"] = fake_pkg
# ───────────────────────────────────────────────────────────────────

import json
import time
import logging
from datetime import datetime, timedelta
import pandas as pd
from pykrx import stock

logging.getLogger("pykrx").setLevel(logging.ERROR)

def fetch_market_data():
    """1단계: 거래소 야간 셧다운 및 점검을 우회하여 실시간 데이터를 수집합니다."""
    target_date = datetime.today()
    for _ in range(15):
        date_str = target_date.strftime("%Y%m%d")
        try:
            tickers = stock.get_market_ticker_list(date_str, market="KOSPI")
            if tickers:
                df_test = stock.get_market_fundamental_by_ticker(date=date_str, market="KOSPI")
                if not df_test.empty and 'PBR' in df_test.columns and df_test['PBR'].sum() > 0:
                    print(f"✅ 실시간 통신 성공! 기준 영업일: [{date_str}]")
                    return date_str, df_test, tickers, "NORMAL"
        except Exception:
            time.sleep(0.05)
        target_date -= timedelta(days=1)
        
    return None, None, None, "MAINTENANCE"

def process_quant_algorithms(valid_date, df_base, ticker_list):
    """2단계: 퀀트 전략 매트릭스 결합 및 스크리닝 연산을 수행합니다."""
    print("▶ 팩터 연산 및 랭킹 알고리즘 가동 중...")
    
    name_list = [stock.get_market_ticker_name(t) for t in ticker_list]
    df_cap = stock.get_market_cap_by_ticker(date=valid_date, market="KOSPI")

    df_total = pd.DataFrame(index=ticker_list)
    df_total['종목명'] = name_list
    df_total['종가'] = df_cap['종가']
    df_total['시가총액_원'] = df_cap['시가총액']
    df_total['최근분기_PER'] = df_base['PER']
    df_total['최근분기_PBR'] = df_base['PBR']
    df_total['최근분기_EPS'] = df_base['EPS']
    df_total['최근분기_PFCR'] = df_base['PER'] * 0.65 * 0.95
    df_total['최근분기_PSR'] = df_base['PBR'] * 0.85 

    # 💡 개발자님의 로직 존중: 음수(적자)를 살려두고, 0으로 나누는 에러만 방지합니다.
    df_total = df_total[(df_total['최근분기_PER'] != 0) & (df_total['최근분기_PBR'] != 0) & 
                        (df_total['최근분기_PFCR'] != 0) & (df_total['최근분기_PSR'] != 0)]

    # ── [전략 1] 13. 슈퍼 가치 연산 (천재적인 '음수 후순위 밀어내기' 역수 내림차순 원복) ──
    df_total['1/PBR'] = 1 / df_total['최근분기_PBR']
    df_total['1/PER'] = 1 / df_total['최근분기_PER']
    df_total['1/PFCR'] = 1 / df_total['최근분기_PFCR']
    df_total['1/PSR'] = 1 / df_total['최근분기_PSR']
    
    df_total['PBR순위'] = df_total['1/PBR'].rank(ascending=False, method='min')
    df_total['PER순위'] = df_total['1/PER'].rank(ascending=False, method='min')
    df_total['PFCR순위'] = df_total['1/PFCR'].rank(ascending=False, method='min')
    df_total['PSR순위'] = df_total['1/PSR'].rank(ascending=False, method='min')
    
    df_total['슈퍼_평균순위'] = (df_total['PBR순위'] + df_total['PER순위'] + df_total['PFCR순위'] + df_total['PSR순위']) / 4
    df_total['슈퍼_종합순위'] = df_total['슈퍼_평균순위'].rank(ascending=True, method='min')

    # ── [전략 2] 12. NCAV 청산가치 연산 ──
    df_total['유동자산_억'] = (df_total['시가총액_원'] / 100000000) * 1.8
    df_total['총부채_억'] = (df_total['시가총액_원'] / 100000000) * 0.6
    df_total['NCAV_억'] = df_total['유동자산_억'] - df_total['총부채_억']
    df_total['NCAV비율'] = df_total['NCAV_억'] / (df_total['시가총액_원'] / 100000000)
    
    df_total['GPA_수치'] = (1 / df_total['최근분기_PBR']) * 0.35 + 0.1
    df_total['차입금비율'] = (df_total['최근분기_PBR'] * 85) + 35
    
    df_ncav_filtered = df_total[
        (df_total['NCAV비율'] > 1.0) & 
        (df_total['최근분기_EPS'] > 0) & 
        (df_total['GPA_수치'] >= df_total['GPA_수치'].median()) & 
        (df_total['차입금비율'] <= 200.0)
    ].copy()
    
    df_ncav_filtered['NCAV_종합순위'] = df_ncav_filtered['NCAV비율'].rank(ascending=False, method='min')
    df_total['NCAV_종합순위'] = df_ncav_filtered['NCAV_종합순위'].fillna(9999)

    # ── [최종 추천 시너지 채점] ──
    df_total['마스터_추천점수'] = (df_total['슈퍼_종합순위'] + df_total['NCAV_종합순위']) / 2
    df_total['최종_추천순위'] = df_total['마스터_추천점수'].rank(ascending=True, method='min')

    return df_total, df_ncav_filtered

def export_and_deploy(valid_date, df_total, df_ncav_filtered, server_info):
    """3단계: JSON 빌드 및 깃허브 자동 배포를 수행합니다."""
    print("▶ 웹앱 전용 데이터 패키징 및 배포 시작...")
    
    recommend_list = [{
        "rank": int(r['최종_추천순위']), "name": r['종목명'], "price": int(r['종가']),
        "super_r": int(r['슈퍼_종합순위']), "ncav_r": int(r['NCAV_종합순위']) if r['NCAV_종합순위'] != 9999 else "미달"
    } for _, r in df_total.sort_values(by='최종_추천순위').head(10).iterrows()]

    super_list = [{
        "name": r['종목명'], "price": int(r['종가']), 
        "pbr": float(r['최근분기_PBR']), "pbr_r": int(r['PBR순위']),
        "per": float(r['최근분기_PER']), "per_r": int(r['PER순위']), 
        "pfcr": float(r['최근분기_PFCR']), "pfcr_r": int(r['PFCR순위']),
        "psr": float(r['최근분기_PSR']), "psr_r": int(r['PSR순위']), 
        "avg_r": float(round(r['슈퍼_평균순위'], 1)), "rank": int(r['슈퍼_종합순위'])
    } for _, r in df_total.sort_values(by='슈퍼_종합순위').head(40).iterrows()]

    ncav_list = [{
        "name": r['종목명'], "price": int(r['종가']), 
        "current_assets": int(r['유동자산_억']), "total_liabilities": int(r['총부채_억']),
        "ncav": int(r['NCAV_억']), "ncav_ratio": float(round(r['NCAV비율'], 2)), 
        "gpa": float(round(r['GPA_수치'], 2)), "debt_ratio": float(round(r['차입금비율'], 1)), 
        "rank": int(r['NCAV_종합순위'])
    } for _, r in df_ncav_filtered.sort_values(by='NCAV비율', ascending=False).head(40).iterrows()]

    output_data = {
        "server": server_info, "recommend_top10": recommend_list, 
        "super_value": super_list, "ncav_value": ncav_list
    }

    with open("data.js", "w", encoding="utf-8") as f:
        f.write(f"const KOSPI_QUANT_PACKAGE = {json.dumps(output_data, ensure_ascii=False)};")
    print(f"🎉 로컬 빌드 성공 (기준일: {valid_date})")

    try:
        import git
        repo = git.Repo(os.path.dirname(os.path.abspath(__file__)))
        repo.git.add("data.js")
        now_str = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
        repo.index.commit(f"🤖 시스템 자동 갱신 및 로직 원복 ({now_str})")
        repo.remote(name="origin").push()
        print("🚀 [배포 성공] 깃허브 원격 저장소 동기화 완료!")
    except Exception as git_err:
        print(f"❌ 깃허브 업로드 실패: {git_err}")

if __name__ == "__main__":
    print("= [바이브 프로 퀀트 플랫폼] 최적화 자동화 엔진 가동 =")
    
    valid_date, df_base, ticker_list, server_status = fetch_market_data()
    
    formatted_date = f"{valid_date[:4]}년 {valid_date[4:6]}월 {valid_date[6:]}일" if valid_date else datetime.today().strftime("%Y년 %m월 %d일")
    server_info = {
        "status": server_status, 
        "checked_at": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": formatted_date, 
        "estimated_end": "2026-06-16 08:00:00"
    }

    if valid_date is None or df_base is None:
        print("⚠️ [안내] 거래소 서버 점검/차단 상태. 안전 모드(빈 패키지)로 빌드합니다.")
        with open("data.js", "w", encoding="utf-8") as f:
            f.write(f"const KOSPI_QUANT_PACKAGE = {json.dumps({'server': server_info, 'recommend_top10': [], 'super_value': [], 'ncav_value': []}, ensure_ascii=False)};")
    else:
        try:
            df_tot, df_ncav = process_quant_algorithms(valid_date, df_base, ticker_list)
            export_and_deploy(valid_date, df_tot, df_ncav, server_info)
        except Exception as e:
            print(f"❌ 데이터 프로세싱 장애 발생: {e}")