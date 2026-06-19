import os
import sys
import json
import zipfile
import io
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from config_local import DATA_GO_KR_API_KEY as API_KEY, DART_API_KEY
except ImportError:
    API_KEY = os.environ.get("DATA_GO_KR_API_KEY", "")
    DART_API_KEY = os.environ.get("DART_API_KEY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORP_CODE_CACHE = os.path.join(BASE_DIR, "dart_corp_map.json")

DART_ACCOUNTS = {
    "assets": ("BS", "ifrs-full_Assets"),
    "current_assets": ("BS", "ifrs-full_CurrentAssets"),
    "liabilities": ("BS", "ifrs-full_Liabilities"),
    "equity": ("BS", "ifrs-full_Equity"),
    "revenue": ("IS", "ifrs-full_Revenue"),
    "cost_of_sales": ("IS", "ifrs-full_CostOfSales"),
    "net_income": ("IS", "ifrs-full_ProfitLoss"),
    "operating_income": ("IS", "dart_OperatingIncomeLoss"),
    "operating_cf": ("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities"),
}

# 영업이익/순이익은 전년동기 분기 단독값(frmtrm_q_amount)을 DART가 같은 응답에서 같이 주므로
# YoY 성장률은 추가 API 호출 없이 계산 가능
YOY_ACCOUNTS = ("net_income", "operating_income")


def get_corp_code_map():
    """종목코드(stock_code) -> DART corp_code 매핑. 로컬 캐시 사용."""
    if os.path.exists(CORP_CODE_CACHE):
        with open(CORP_CODE_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)

    r = requests.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": DART_API_KEY},
        timeout=30,
    )
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read("CORPCODE.xml"))

    mapping = {}
    for item in root.findall("list"):
        stock_code = (item.find("stock_code").text or "").strip()
        if stock_code:
            mapping[stock_code] = item.find("corp_code").text.strip()

    with open(CORP_CODE_CACHE, "w", encoding="utf-8") as f:
        json.dump(mapping, f)

    return mapping


# (reprt_code, 연초부터 보고서 기간 말까지의 누적 개월 수, 공시 마감 月, 공시 마감 日, 공시마감 연도오프셋)
# CF 계정(영업활동현금흐름)만 연초 누적치로 잡혀 annualize_factor(=12/months)가 필요하다.
# IS 계정(매출/순이익/영업이익)은 thstrm_amount 자체가 분기 단독값이라 별도 처리(x4)한다.
REPORT_PERIODS = [
    ("11014", 9, 11, 14, 0),   # 3분기보고서 (1~9월 누적), 공시마감 11/14
    ("11012", 6, 8, 14, 0),    # 반기보고서 (1~6월 누적), 공시마감 8/14
    ("11013", 3, 5, 15, 0),    # 1분기보고서 (1~3월 누적), 공시마감 5/15
    ("11011", 12, 3, 31, 1),   # 사업보고서(연간), 공시마감 다음해 3/31
]


def _build_period_candidates():
    """오늘 날짜 기준으로 이미 공시 마감이 지난 보고서들을 최신순으로 나열."""
    today = datetime.now()
    candidates = []
    for bsns_year in (today.year, today.year - 1, today.year - 2):
        for reprt_code, months, due_month, due_day, due_year_offset in REPORT_PERIODS:
            due_date = datetime(bsns_year + due_year_offset, due_month, due_day)
            if due_date <= today:
                period_end = datetime(bsns_year, ((months - 1) // 3 + 1) * 3 if months < 12 else 12, 1)
                candidates.append((period_end, bsns_year, reprt_code, months))

    candidates.sort(key=lambda c: c[0], reverse=True)
    return [(year, code, months) for (_end, year, code, months) in candidates]


def has_recent_capital_increase(corp_code):
    """최근 1년간 유상증자결정 공시(주요사항보고서) 여부."""
    end_de = datetime.now()
    bgn_de = end_de - timedelta(days=365)
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bgn_de": bgn_de.strftime("%Y%m%d"),
                "end_de": end_de.strftime("%Y%m%d"),
                "pblntf_ty": "B",  # 주요사항보고
                "page_count": 100,
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        return None

    if data.get("status") == "013":  # 조회된 데이터 없음 (정상)
        return False
    if data.get("status") != "000":
        return None

    return any("유상증자" in (item.get("report_nm") or "") for item in data.get("list", []) or [])


def _fetch_is_period(corp_code, year, reprt_code, fs_div):
    """손익계산서에서 영업이익/순이익의 (당기 분기단독, 당기 누적) 값을 한 번에 가져온다."""
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        return None

    if data.get("status") != "000":
        return None

    targets = {key: DART_ACCOUNTS[key][1] for key in ("operating_income", "net_income")}
    result = {}
    for row in data.get("list", []):
        if row.get("sj_div") != "IS":
            continue
        for key, account_id in targets.items():
            if row.get("account_id") == account_id and key not in result:
                try:
                    single = float(row.get("thstrm_amount", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    continue
                try:
                    cum = float(row.get("thstrm_add_amount", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    cum = single
                result[key] = {"single": single, "cum": cum}

    return result or None


def fetch_prev_quarter_is(corp_code, year, reprt_code, fs_div):
    """직전 분기(3개월 단독)의 영업이익/순이익을 구한다. 보고서 종류별로 차감 방식이 다르다."""
    if reprt_code == "11013":  # 1분기 -> 전분기는 전년 4분기 = 전년 사업보고서(연간) - 전년 3분기 누적
        annual = _fetch_is_period(corp_code, year - 1, "11011", fs_div)
        q3 = _fetch_is_period(corp_code, year - 1, "11014", fs_div)
        if not annual or not q3:
            return None
        return {k: annual[k]["single"] - q3[k]["cum"] for k in annual if k in q3}
    elif reprt_code == "11012":  # 반기 -> 전분기는 같은 해 1분기(단독값 그대로)
        q1 = _fetch_is_period(corp_code, year, "11013", fs_div)
        return {k: v["single"] for k, v in q1.items()} if q1 else None
    elif reprt_code == "11014":  # 3분기 -> 전분기는 같은 해 반기(2분기, 단독값 그대로)
        half = _fetch_is_period(corp_code, year, "11012", fs_div)
        return {k: v["single"] for k, v in half.items()} if half else None
    elif reprt_code == "11011":  # 사업보고서 -> 전분기는 같은 해 4분기 = 연간 - 3분기 누적
        annual = _fetch_is_period(corp_code, year, "11011", fs_div)
        q3 = _fetch_is_period(corp_code, year, "11014", fs_div)
        if not annual or not q3:
            return None
        return {k: annual[k]["single"] - q3[k]["cum"] for k in annual if k in q3}
    return None


def fetch_dart_financials(corp_code):
    """가장 최근에 공시된 분기/반기/3분기/사업보고서에서 핵심 재무계정을 가져와 연환산한다."""
    for year, reprt_code, months in _build_period_candidates():
        for fs_div in ("CFS", "OFS"):
            try:
                r = requests.get(
                    "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                    params={
                        "crtfc_key": DART_API_KEY,
                        "corp_code": corp_code,
                        "bsns_year": str(year),
                        "reprt_code": reprt_code,
                        "fs_div": fs_div,
                    },
                    timeout=15,
                )
                data = r.json()
            except Exception:
                continue

            if data.get("status") != "000":
                continue

            values = {}
            for row in data.get("list", []):
                account_id = row.get("account_id")
                sj_div = row.get("sj_div")
                for key, (target_sj, target_id) in DART_ACCOUNTS.items():
                    if sj_div == target_sj and account_id == target_id and key not in values:
                        try:
                            values[key] = float(row.get("thstrm_amount", "0").replace(",", ""))
                        except (ValueError, AttributeError):
                            pass
                        if key in YOY_ACCOUNTS:
                            try:
                                values[f"{key}_yoy"] = float(row.get("frmtrm_q_amount", "0").replace(",", ""))
                            except (ValueError, AttributeError):
                                pass

            if "assets" not in values or "liabilities" not in values:
                continue

            # DART 손익계산서(IS) 항목의 thstrm_amount는 사업보고서가 아니면 이미 "당기 1개 분기" 단독값이다
            # (반기보고서도 6개월 누적이 아니라 2분기 단독값을 thstrm_amount로 줌 - 누적은 thstrm_add_amount).
            # 사업보고서(11011)는 thstrm_amount 자체가 연간 총액이므로 그대로 사용.
            for key in ("revenue", "cost_of_sales", "net_income", "operating_income"):
                if key in values:
                    values[f"quarter_{key}"] = values[key]
                    if reprt_code != "11011":
                        values[key] *= 4  # 분기 단독값을 연환산

            # 영업활동현금흐름(CF)은 DART가 분기 단독값을 따로 안 주고 연초 누적치만 제공
            if "operating_cf" in values:
                values["operating_cf"] *= (12 / months)

            # 직전 분기(3개월 단독) 영업이익/순이익 - 모멘텀(전분기대비) 계산용
            prev_q = fetch_prev_quarter_is(corp_code, year, reprt_code, fs_div)
            if prev_q:
                values["prev_operating_income"] = prev_q.get("operating_income")
                values["prev_net_income"] = prev_q.get("net_income")

            values["_period"] = f"{year}-{reprt_code}"
            return values

    return None

def fetch_krx_market_data():
    print("[시스템] 공공데이터포털(금융위) API 연동을 시작합니다...")
    
    url = "http://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
    
    items = []
    krx_basis_date = None
    # 최근 7일 중 가장 최신 영업일 데이터 탐색
    for i in range(7):
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")

        params = {
            "serviceKey": API_KEY,
            "resultType": "json",
            "numOfRows": "3000", 
            "pageNo": "1",
            "mrktCls": "KOSPI",
            "basDt": target_date
        }
        
        try:
            response = requests.get(url, params=params, timeout=15)
            data = response.json()
            fetched_items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            if fetched_items:
                items = fetched_items
                krx_basis_date = target_date
                print(f"✅ [성공] {target_date} 기준 영업일 데이터 조회 성공!")
                break
        except Exception:
            continue

    if not items:
        print("⚠️ [경고] 데이터 로딩에 실패했습니다.")
        return False
        
    print(f"✅ [성공] 국토 데이터 수집 완료! 종목 수: {len(items)}개")

    # 시세 API에는 PER/PBR/PSR/EPS 항목이 없으므로 가격/시총/종목코드만 추출
    raw_stocks = []
    for item in items:
        try:
            price = int(item.get("clpr", 0)) if item.get("clpr") else 0
            market_cap = float(item.get("mrktTotAmt", 0) or 0)
            code = item.get("srtnCd", "")
            if price <= 0 or market_cap <= 0 or not code:
                continue
            raw_stocks.append({
                "name": item.get("itmsNm", ""),
                "price": price,
                "code": code,
                "market_cap": market_cap,
            })
        except Exception:
            continue

    # ------------------------------------------------------------------------
    # DART 재무제표 연동 - 시가총액 대비 재무수치로 PER/PBR/PSR/PFCR을 직접 계산
    # ------------------------------------------------------------------------
    valid_stocks = []
    dart_periods_used = []
    if not DART_API_KEY:
        print("⚠️ [경고] DART_API_KEY가 설정되지 않아 가치지표를 계산할 수 없습니다.")
    else:
        try:
            corp_map = get_corp_code_map()
        except Exception as e:
            print(f"⚠️ [경고] DART corp_code 매핑 실패: {e}")
            corp_map = {}

        print(f"[DART] {len(raw_stocks)}개 종목의 재무제표 조회를 시작합니다 (시간이 다소 걸립니다)...")
        for i, s in enumerate(raw_stocks):
            corp_code = corp_map.get(s["code"])
            if not corp_code:
                continue
            fin = fetch_dart_financials(corp_code)
            if not fin:
                continue

            assets = fin.get("assets", 0)
            liabilities = fin.get("liabilities", 0)
            equity = fin.get("equity", assets - liabilities)
            current_assets = fin.get("current_assets", 0)
            revenue = fin.get("revenue", 0)
            cost_of_sales = fin.get("cost_of_sales", 0)
            net_income = fin.get("net_income", 0)
            operating_cf = fin.get("operating_cf", 0)
            quarter_net_income = fin.get("quarter_net_income", 0)
            quarter_operating_income = fin.get("quarter_operating_income")
            dart_periods_used.append(fin.get("_period"))

            # 11. 이익 모멘텀 전략 - 전분기대비/전년동기대비 영업이익·순이익 성장률
            def growth_rate(curr, base):
                if curr is None or base is None or base == 0:
                    return None
                return round((curr - base) / abs(base) * 100, 1)

            op_growth_qoq = growth_rate(quarter_operating_income, fin.get("prev_operating_income"))
            op_growth_yoy = growth_rate(quarter_operating_income, fin.get("operating_income_yoy"))
            ni_growth_qoq = growth_rate(quarter_net_income, fin.get("prev_net_income"))
            ni_growth_yoy = growth_rate(quarter_net_income, fin.get("net_income_yoy"))

            # 6. 신 F-스코어+저PBR - 3개 이진지표(1/0)의 합산 점수
            cap_increase = has_recent_capital_increase(corp_code)
            cap_increase_flag = 0 if cap_increase else (1 if cap_increase is False else None)
            quarter_net_income_raw = fin.get("quarter_net_income")
            ni_pos_flag = None if quarter_net_income_raw is None else (1 if quarter_net_income_raw >= 0 else 0)
            operating_cf_raw = fin.get("operating_cf")
            cf_pos_flag = None if operating_cf_raw is None else (1 if operating_cf_raw >= 0 else 0)
            if None in (cap_increase_flag, ni_pos_flag, cf_pos_flag):
                f_score = None
            else:
                f_score = cap_increase_flag + ni_pos_flag + cf_pos_flag

            market_cap = s["market_cap"]
            per = round(market_cap / net_income, 2) if net_income > 0 else 0.0
            pbr = round(market_cap / equity, 2) if equity > 0 else 0.0
            psr = round(market_cap / revenue, 2) if revenue > 0 else 0.0
            pfcr = round(market_cap / operating_cf, 2) if operating_cf > 0 else 0.0
            debt_ratio = round(liabilities / equity * 100, 1) if equity > 0 else None
            # 지주회사 등 일부 업종은 표준 IFRS 매출/매출원가 계정을 쓰지 않아 데이터가 없을 수 있음
            gpa = round((revenue - cost_of_sales) / assets * 100, 1) if (assets > 0 and revenue > 0) else None
            ncav_total = current_assets - liabilities
            ncav = int(ncav_total / 100000000)  # 억 단위
            ncav_ratio = round(ncav_total / market_cap, 2) if market_cap > 0 else 0.0
            eps = int(net_income / (market_cap / s["price"])) if net_income and market_cap else 0

            valid_stocks.append({
                **s,
                "per": per,
                "pbr": pbr,
                "psr": psr,
                "pfcr": pfcr,
                "eps": eps,
                "ncav": ncav,
                "ncav_ratio": ncav_ratio,
                "gpa": gpa,
                "debt_ratio": debt_ratio,
                "net_income": net_income,
                "quarter_net_income": quarter_net_income,
                "cap_increase_flag": cap_increase_flag,
                "ni_pos_flag": ni_pos_flag,
                "cf_pos_flag": cf_pos_flag,
                "f_score": f_score,
                "op_growth_qoq": op_growth_qoq,
                "op_growth_yoy": op_growth_yoy,
                "ni_growth_qoq": ni_growth_qoq,
                "ni_growth_yoy": ni_growth_yoy,
            })

            if (i + 1) % 100 == 0:
                print(f"[DART] 진행 중... {i + 1}/{len(raw_stocks)}")

        print(f"[DART] 재무제표 보강 완료: {len(valid_stocks)}개 종목 확보")

    # 13. 슈퍼 가치 4대 통합 스크리너 연산부
    def rank_super_value(pool):
        # 음수가 있을 수 있으므로 원값 오름차순이 아니라 역수의 내림차순으로 순위를 매긴다
        # dict()로 복사해서 사용 - 전체/소형주 풀을 번갈아 랭킹할 때 같은 종목 객체를 공유하면
        # 나중 호출의 순위가 앞서 계산해둔 결과를 덮어써버리는 문제를 방지한다
        base = [dict(s) for s in pool if s["per"] > 0 and s["pbr"] > 0 and s["psr"] > 0 and s["pfcr"] > 0]
        base.sort(key=lambda x: 1 / x["per"], reverse=True)
        for idx, s in enumerate(base): s["per_r"] = idx + 1
        base.sort(key=lambda x: 1 / x["pbr"], reverse=True)
        for idx, s in enumerate(base): s["pbr_r"] = idx + 1
        base.sort(key=lambda x: 1 / x["psr"], reverse=True)
        for idx, s in enumerate(base): s["psr_r"] = idx + 1
        base.sort(key=lambda x: 1 / x["pfcr"], reverse=True)
        for idx, s in enumerate(base): s["pfcr_r"] = idx + 1
        for s in base: s["int_score"] = s["per_r"] + s["pbr_r"] + s["psr_r"] + s["pfcr_r"]
        base.sort(key=lambda x: x["int_score"])
        return base

    super_base = rank_super_value(valid_stocks)

    # 소형주(시가총액 하위 20%) 한정 슈퍼 가치 랭킹
    market_caps = sorted(s["market_cap"] for s in valid_stocks)
    smallcap_cutoff = market_caps[max(int(len(market_caps) * 0.2) - 1, 0)] if market_caps else 0
    super_base_smallcap = rank_super_value([s for s in valid_stocks if s["market_cap"] <= smallcap_cutoff])

    # 11. 이익 모멘텀 전략 연산부
    def rank_momentum(pool):
        # dict()로 복사 - 전체/소형주 풀 간 종목 객체 공유로 순위가 서로 덮어써지는 문제 방지
        base = [
            dict(s) for s in pool
            if all(s.get(k) is not None for k in ("op_growth_qoq", "op_growth_yoy", "ni_growth_qoq", "ni_growth_yoy"))
        ]
        base.sort(key=lambda x: x["op_growth_qoq"], reverse=True)
        for idx, s in enumerate(base): s["op_qoq_r"] = idx + 1
        base.sort(key=lambda x: x["op_growth_yoy"], reverse=True)
        for idx, s in enumerate(base): s["op_yoy_r"] = idx + 1
        base.sort(key=lambda x: x["ni_growth_qoq"], reverse=True)
        for idx, s in enumerate(base): s["ni_qoq_r"] = idx + 1
        base.sort(key=lambda x: x["ni_growth_yoy"], reverse=True)
        for idx, s in enumerate(base): s["ni_yoy_r"] = idx + 1
        for s in base: s["momentum_score"] = s["op_qoq_r"] + s["op_yoy_r"] + s["ni_qoq_r"] + s["ni_yoy_r"]
        base.sort(key=lambda x: x["momentum_score"])
        return base

    momentum_base = rank_momentum(valid_stocks)
    momentum_base_smallcap = rank_momentum([s for s in valid_stocks if s["market_cap"] <= smallcap_cutoff])

    # 6. 신 F-스코어+저PBR 연산부 - 3개 지표(유상증자 없음/순이익>=0/영업CF>=0) 모두 충족하는 종목만, PBR 내림차순
    fscore_base = [s for s in valid_stocks if s["f_score"] == 3 and s["pbr"] > 0]
    fscore_base.sort(key=lambda x: x["pbr"], reverse=True)

    # 12. NCAV 청산가치 & 퀄리티 스크리너 연산부
    # 조건: 1) 순유동자산 > 시가총액  2) 최신 분기 순이익 > 0  3) 차입금비율 200% 이하  4) 상위 20개만 노출
    # GP/A 상위 50% 필터는 기본 비적용 - 프론트엔드 체크박스로 켤 때만 별도 리스트(ncav_base_gpa) 적용
    def ncav_filter_base(pool):
        return [
            s for s in pool
            if s["ncav_ratio"] > 1
            and s["net_income"] > 0
            and s["debt_ratio"] is not None and s["debt_ratio"] <= 200
        ]

    gpa_pool = sorted((s["gpa"] for s in valid_stocks if s["gpa"] is not None), reverse=True)
    gpa_cutoff = gpa_pool[len(gpa_pool) // 2 - 1] if gpa_pool else None

    ncav_base = ncav_filter_base(valid_stocks)
    ncav_base.sort(key=lambda x: x["ncav_ratio"], reverse=True)

    ncav_base_gpa = [
        s for s in ncav_base
        if s["gpa"] is not None and gpa_cutoff is not None and s["gpa"] >= gpa_cutoff
    ]
    ncav_base_gpa.sort(key=lambda x: x["ncav_ratio"], reverse=True)

    # ------------------------------------------------------------------------
    # 최종 index.html 전용 'KOSPI_QUANT_PACKAGE' 단일 객체 패키징 공정
    # ------------------------------------------------------------------------
    now = datetime.now()

    # 가장 많이 쓰인 DART 보고서 기준(분기/반기/3분기/사업보고서)을 대표값으로 표시
    REPORT_LABELS = {
        "11013": "1분기보고서", "11012": "반기보고서",
        "11014": "3분기보고서", "11011": "사업보고서",
    }
    dart_basis = "N/A"
    valid_periods = [p for p in dart_periods_used if p]
    if valid_periods:
        from collections import Counter
        top_period, _ = Counter(valid_periods).most_common(1)[0]
        year, reprt_code = top_period.split("-")
        dart_basis = f"{year}년 {REPORT_LABELS.get(reprt_code, reprt_code)}"

    def package_super_value(base):
        packaged = []
        for idx, s in enumerate(base[:30]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "price": s["price"],
                "pbr": s["pbr"],
                "pbr_r": s["pbr_r"],
                "per": s["per"],
                "per_r": s["per_r"],
                "pfcr": s["pfcr"],
                "pfcr_r": s["pfcr_r"],
                "psr": s["psr"],
                "psr_r": s["psr_r"],
                "avg_r": round(s["int_score"] / 4, 1)
            })
        return packaged

    # 슈퍼 가치 상위 30개 패키징 (전체 / 소형주 한정)
    super_value = package_super_value(super_base)
    super_value_smallcap = package_super_value(super_base_smallcap)

    def package_momentum(base):
        packaged = []
        for idx, s in enumerate(base[:30]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "price": s["price"],
                "op_growth_qoq": s["op_growth_qoq"],
                "op_qoq_r": s["op_qoq_r"],
                "op_growth_yoy": s["op_growth_yoy"],
                "op_yoy_r": s["op_yoy_r"],
                "ni_growth_qoq": s["ni_growth_qoq"],
                "ni_qoq_r": s["ni_qoq_r"],
                "ni_growth_yoy": s["ni_growth_yoy"],
                "ni_yoy_r": s["ni_yoy_r"],
                "avg_r": round(s["momentum_score"] / 4, 1)
            })
        return packaged

    # 이익 모멘텀 상위 30개 패키징 (전체 / 소형주 한정)
    momentum_value = package_momentum(momentum_base)
    momentum_value_smallcap = package_momentum(momentum_base_smallcap)

    # 신 F-스코어+저PBR 상위 30개 패키징
    fscore_value = []
    for idx, s in enumerate(fscore_base[:30]):
        fscore_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "price": s["price"],
            "cap_increase_flag": s["cap_increase_flag"],
            "ni_pos_flag": s["ni_pos_flag"],
            "cf_pos_flag": s["cf_pos_flag"],
            "f_score": s["f_score"],
            "pbr": s["pbr"],
        })

    # NCAV 상위 20개 패키징 (GP/A 필터 미적용 / 적용)
    def package_ncav_value(base):
        packaged = []
        for idx, s in enumerate(base[:20]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "price": s["price"],
                "ncav": s["ncav"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "quarter_net_income": int(s["quarter_net_income"] / 100000000),  # 억 단위
                "gpa": s["gpa"],
                "debt_ratio": s["debt_ratio"]
            })
        return packaged

    ncav_value = package_ncav_value(ncav_base)
    ncav_value_gpa = package_ncav_value(ncav_base_gpa)

    # 최종 패키지 조립
    package = {
        "server": {
            "krx_basis_date": krx_basis_date or "N/A",
            "dart_basis": dart_basis,
            "status": "NORMAL",
            "checked_at": now.strftime("%H:%M:%S"),
        },
        "super_value": super_value,
        "super_value_smallcap": super_value_smallcap,
        "momentum_value": momentum_value,
        "momentum_value_smallcap": momentum_value_smallcap,
        "fscore_value": fscore_value,
        "ncav_value": ncav_value,
        "ncav_value_gpa": ncav_value_gpa
    }
    
    # data.js 전용 스크립트로 굽기
    js_content = f"const KOSPI_QUANT_PACKAGE = {json.dumps(package, ensure_ascii=False, indent=4)};\n"
    
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(js_content)
        
    print("▶ 오리지널 웹앱 전용 KOSPI_QUANT_PACKAGE 패키징 완료.")
    return True

def deploy_to_github():
    print("🚀 [배포 시작] 깃허브 원격 저장소 동기화 중...")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(current_dir)
    
    os.system("git add data.js")
    os.system('git commit -m "🤖 [자동화] 오리지널 데이터 규격 동기화 배포"')
    res = os.system("git push origin main")
    
    if res == 0:
        print("🚀 [배포 성공] 깃허브 원격 저장소 동기화 완료!")
    else:
        print("❌ 깃허브 업로드 중 오류가 발생했습니다.")

if __name__ == "__main__":
    if fetch_krx_market_data():
        deploy_to_github()