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

# 업데이트 주기가 긴 데이터는 디스크에 캐싱해서 재검증 시간을 단축한다.
# - DART 재무제표: 분기 단위로만 바뀜 -> 최신 분기로 이미 캐싱돼 있으면 API 호출 스킵
# - 유상증자 여부: 하루 단위로만 갱신
# - 월간 가격 스냅샷: 과거 날짜는 불변값이라 한 번 캐싱하면 재사용
DART_FINANCIALS_CACHE_FILE = os.path.join(BASE_DIR, "cache_dart_financials.json")
CAPITAL_INCREASE_CACHE_FILE = os.path.join(BASE_DIR, "cache_capital_increase.json")
MONTHLY_PRICE_CACHE_FILE = os.path.join(BASE_DIR, "cache_monthly_price.json")
INDUTY_CODE_CACHE_FILE = os.path.join(BASE_DIR, "cache_induty_code.json")
ADMIN_ISSUE_CACHE_FILE = os.path.join(BASE_DIR, "cache_admin_issue.json")


def load_json_cache(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_json_cache(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


DART_ACCOUNTS = {
    "assets": ("BS", "ifrs-full_Assets"),
    "current_assets": ("BS", "ifrs-full_CurrentAssets"),
    "current_liabilities": ("BS", "ifrs-full_CurrentLiabilities"),
    "liabilities": ("BS", "ifrs-full_Liabilities"),
    "equity": ("BS", "ifrs-full_Equity"),
    "revenue": ("IS", "ifrs-full_Revenue"),
    "cost_of_sales": ("IS", "ifrs-full_CostOfSales"),
    "net_income": ("IS", "ifrs-full_ProfitLoss"),
    "operating_income": ("IS", "dart_OperatingIncomeLoss"),
    "operating_cf": ("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities"),
    "capex": ("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    "dividends_paid": ("CF", "ifrs-full_DividendsPaidClassifiedAsFinancingActivities"),
}

# 영업이익/순이익은 전년동기 분기 단독값(frmtrm_q_amount)을 DART가 같은 응답에서 같이 주므로
# YoY 성장률은 추가 API 호출 없이 계산 가능
YOY_ACCOUNTS = ("net_income", "operating_income")

# 분기 단독값이 따로 없는(=thstrm_amount가 연초 누적치인) CF 계정들. 1분기 외 보고서에서는
# 같은 해 이전 분기의 누적치를 빼서 단독 분기값을 derive해야 한다.
CUMULATIVE_ONLY_ACCOUNTS = ("operating_cf", "capex")


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


def has_recent_capital_increase(corp_code, cache):
    """최근 1년간 유상증자결정 공시(주요사항보고서) 여부. 하루 단위로 캐싱."""
    today_str = datetime.now().strftime("%Y%m%d")
    cached = cache.get(corp_code)
    if cached and cached.get("date") == today_str:
        return cached.get("value")

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
        value = False
    elif data.get("status") != "000":
        return None
    else:
        value = any("유상증자" in (item.get("report_nm") or "").replace(" ", "") for item in data.get("list", []) or [])

    cache[corp_code] = {"date": today_str, "value": value}
    return value


# 금융업(은행/보험/증권 등 KSIC 대분류 K, 64~66) + 지주회사(DART induty_code 64992) 제외용
FINANCIAL_INDUTY_PREFIXES = ("64", "65", "66")
HOLDING_NAME_PATTERNS = ("홀딩스", "홀딩", "지주")


def fetch_induty_code(corp_code, cache):
    """DART 회사개요에서 업종코드(induty_code)를 가져온다. 업종은 거의 안 바뀌므로 영구 캐싱."""
    if corp_code in cache:
        return cache[corp_code]
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/company.json",
            params={"crtfc_key": DART_API_KEY, "corp_code": corp_code},
            timeout=15,
        )
        data = r.json()
    except Exception:
        return None
    if data.get("status") != "000":
        return None
    induty_code = data.get("induty_code")
    cache[corp_code] = induty_code
    return induty_code


def is_financial_or_holding(corp_code, name, induty_cache):
    """금융업(은행/보험/증권) 또는 지주회사인지 판별 - 업종코드 + 회사명 패턴을 함께 본다."""
    induty_code = fetch_induty_code(corp_code, induty_cache)
    if induty_code and induty_code.startswith(FINANCIAL_INDUTY_PREFIXES):
        return True
    return any(p in name for p in HOLDING_NAME_PATTERNS)


def get_admin_status(corp_code, cache):
    """관리종목 현재 상태를 판정. 최근 2년 거래소공시(I)에서 '관리종목지정'/'관리종목지정해제'/
    '관리종목지정우려' 중 가장 최근 건으로 (지정여부, 지정우려여부)를 판단한다. 하루 단위 캐싱."""
    today_str = datetime.now().strftime("%Y%m%d")
    cached = cache.get(corp_code)
    if cached and cached.get("date") == today_str:
        return cached.get("issue"), cached.get("warning")

    end_de = datetime.now()
    bgn_de = end_de - timedelta(days=730)
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bgn_de": bgn_de.strftime("%Y%m%d"),
                "end_de": end_de.strftime("%Y%m%d"),
                "pblntf_ty": "I",  # 거래소공시
                "page_count": 100,
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        return None, None

    if data.get("status") == "013":
        issue, warning = False, False
    elif data.get("status") != "000":
        return None, None
    else:
        events = []  # (날짜, 종류) 종류: 'issue'(지정) / 'release'(해제) / 'warning'(지정우려)
        for item in data.get("list", []) or []:
            nm = item.get("report_nm") or ""
            # 실제 공시명은 "관리종목지정"처럼 붙어 있거나 "관리종목 지정"처럼 띄어 있는 등 표기가
            # 제각각이라(예: "기타시장안내(관리종목 지정사유 추가 ...)"), 띄어쓰기를 무시하고 매칭한다
            nm_compact = nm.replace(" ", "")
            if "관리종목" not in nm_compact or "지정" not in nm_compact:
                continue
            kind = "warning" if "우려" in nm_compact else ("release" if "해제" in nm_compact else "issue")
            events.append((item.get("rcept_dt", ""), kind))
        events.sort(key=lambda e: e[0])
        latest_kind = events[-1][1] if events else None
        issue = latest_kind == "issue"
        warning = latest_kind == "warning"

    cache[corp_code] = {"date": today_str, "issue": issue, "warning": warning}
    return issue, warning


def _fetch_period_accounts(corp_code, year, reprt_code, fs_div, keys):
    """주어진 보고서에서 지정된 계정들의 (당기 분기단독, 당기 누적) 값을 한 번에 가져온다."""
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

    targets = {key: DART_ACCOUNTS[key] for key in keys}
    result = {}
    for row in data.get("list", []):
        sj_div = row.get("sj_div")
        for key, (target_sj, account_id) in targets.items():
            if sj_div == target_sj and row.get("account_id") == account_id and key not in result:
                try:
                    single = float(row.get("thstrm_amount", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    continue
                try:
                    cum = float(row.get("thstrm_add_amount", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    cum = single  # CF계정은 add_amount가 없고 thstrm_amount 자체가 누적치
                result[key] = {"single": single, "cum": cum}

    return result or None


BASELINE_KEYS = ("operating_income", "net_income", "operating_cf", "capex")


def fetch_baseline_period(corp_code, year, reprt_code, fs_div):
    """현재 보고서의 분기단독값을 derive하기 위해 필요한, 같은 해의 직전 보고서 데이터를 가져온다.
    1분기 보고서는 누적=단독이라 베이스라인이 필요 없다."""
    if reprt_code == "11012":  # 반기 -> 같은 해 1분기
        return _fetch_period_accounts(corp_code, year, "11013", fs_div, BASELINE_KEYS)
    elif reprt_code == "11014":  # 3분기 -> 같은 해 반기
        return _fetch_period_accounts(corp_code, year, "11012", fs_div, BASELINE_KEYS)
    elif reprt_code == "11011":  # 사업보고서 -> 같은 해 3분기
        return _fetch_period_accounts(corp_code, year, "11014", fs_div, BASELINE_KEYS)
    return None


def fetch_prev_year_q4_base(corp_code, year, fs_div):
    """1분기 보고서일 때 "전분기"(전년 4분기) 계산에 필요한 전년 사업보고서/3분기 데이터."""
    annual = _fetch_period_accounts(corp_code, year - 1, "11011", fs_div, BASELINE_KEYS)
    q3 = _fetch_period_accounts(corp_code, year - 1, "11014", fs_div, BASELINE_KEYS)
    if not annual or not q3:
        return None
    return {k: annual[k]["single"] - q3[k]["cum"] for k in annual if k in q3}


def extract_borrowings(rows):
    """총차입금 추정치: DART는 '차입금' 단일 표준계정이 없어 회사마다 계정ID가 다르므로,
    재무상태표(BS)에서 계정명에 '차입금' 또는 '사채'가 들어간 항목을 전부 합산한다."""
    total = 0.0
    found = False
    for row in rows:
        if row.get("sj_div") != "BS":
            continue
        name = row.get("account_nm") or ""
        if "차입금" in name or "사채" in name:
            try:
                total += float(row.get("thstrm_amount", "0").replace(",", ""))
                found = True
            except (ValueError, AttributeError):
                pass
    return total if found else None


def fetch_bs_snapshot_yoy(corp_code, year, reprt_code, fs_div):
    """전년동기 시점의 자산총계/부채총계 스냅샷 (자산성장률, 차입금 YoY용)."""
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(year - 1),
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

    result = {}
    for row in data.get("list", []):
        for key in ("assets", "liabilities"):
            target_sj, account_id = DART_ACCOUNTS[key]
            if row.get("sj_div") == target_sj and row.get("account_id") == account_id and key not in result:
                try:
                    result[key] = float(row.get("thstrm_amount", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    pass

    borrowings = extract_borrowings(data.get("list", []))
    result["borrowings"] = borrowings if borrowings is not None else result.get("liabilities")

    return result or None


def fetch_latest_annual_dividend(corp_code, fs_div):
    """배당율은 분기 단위가 아니라 가장 최근 사업보고서(연간)의 현금배당 총액 기준으로 계산한다."""
    candidates = [c for c in _build_period_candidates() if c[1] == "11011"]
    if not candidates:
        return None
    year = candidates[0][0]
    result = _fetch_period_accounts(corp_code, year, "11011", fs_div, ("dividends_paid",))
    return result["dividends_paid"]["single"] if result and "dividends_paid" in result else None


def fetch_dart_financials(corp_code, cache=None):
    """가장 최근에 공시된 분기/반기/3분기/사업보고서에서 핵심 재무계정을 가져와 연환산한다.
    같은 분기로 이미 캐싱된 결과가 있으면 API 호출 없이 그대로 재사용한다."""
    candidates = _build_period_candidates()
    if cache is not None and candidates:
        best_year, best_reprt_code, _ = candidates[0]
        cached = cache.get(corp_code)
        if cached and cached.get("_period") == f"{best_year}-{best_reprt_code}":
            return cached

    for year, reprt_code, months in candidates:
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

            # 차입금(이자부담부채) - 표준계정 대신 계정명 매칭으로 추출, 못 찾으면 부채총계로 대체
            borrowings = extract_borrowings(data.get("list", []))
            values["borrowings"] = borrowings if borrowings is not None else values["liabilities"]

            # DART 손익계산서(IS) 항목의 thstrm_amount는 사업보고서가 아니면 이미 "당기 1개 분기" 단독값이다
            # (반기보고서도 6개월 누적이 아니라 2분기 단독값을 thstrm_amount로 줌 - 누적은 thstrm_add_amount).
            # 사업보고서(11011)는 thstrm_amount 자체가 연간 총액이므로 그대로 사용.
            for key in ("revenue", "cost_of_sales", "net_income", "operating_income"):
                if key in values:
                    values[f"quarter_{key}"] = values[key]

            baseline = None if reprt_code == "11013" else fetch_baseline_period(corp_code, year, reprt_code, fs_div)

            # CF 계정(영업활동현금흐름/CapEx)은 분기 단독값이 따로 없고 연초 누적치만 주어지므로
            # 1분기가 아니면 직전 보고서의 누적치를 빼서 분기 단독값을 derive한다.
            for key in CUMULATIVE_ONLY_ACCOUNTS:
                if key not in values:
                    continue
                if reprt_code == "11013":
                    values[f"quarter_{key}"] = values[key]
                elif baseline and key in baseline:
                    values[f"quarter_{key}"] = values[key] - baseline[key]["cum"]
                # baseline 조회 실패 시 quarter_{key}는 채워지지 않음 (해당 지표 계산 제외)

            # 직전 분기(3개월 단독) 영업이익/순이익 - 모멘텀(전분기대비) 계산용
            if reprt_code == "11013":
                prev_q = fetch_prev_year_q4_base(corp_code, year, fs_div)
            elif baseline:
                prev_q = {k: v["single"] for k, v in baseline.items() if k in ("operating_income", "net_income")}
            else:
                prev_q = None
            if prev_q:
                values["prev_operating_income"] = prev_q.get("operating_income")
                values["prev_net_income"] = prev_q.get("net_income")

            # 전년동기 시점 자산총계/부채총계 - 자산성장률, 영업이익/차입금 비율 YoY 계산용
            bs_yoy = fetch_bs_snapshot_yoy(corp_code, year, reprt_code, fs_div)
            if bs_yoy:
                values["assets_yoy"] = bs_yoy.get("assets")
                values["borrowings_yoy"] = bs_yoy.get("borrowings")

            # 19. 배당율 - 분기 단독값이 아니라 가장 최근 사업보고서의 연간 배당총액 기준
            if reprt_code == "11011":
                values["annual_dividends_paid"] = values.get("dividends_paid")
            else:
                values["annual_dividends_paid"] = fetch_latest_annual_dividend(corp_code, fs_div)

            values["_period"] = f"{year}-{reprt_code}"
            if cache is not None:
                cache[corp_code] = values
            return values

    return None

def fetch_krx_market_data():
    print("[시스템] 공공데이터포털(금융위) API 연동을 시작합니다...")
    
    url = "http://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
    
    def fetch_market(mrkt_cls, target_date):
        params = {
            "serviceKey": API_KEY,
            "resultType": "json",
            "numOfRows": "3000",
            "pageNo": "1",
            "mrktCls": mrkt_cls,
            "basDt": target_date,
        }
        try:
            response = requests.get(url, params=params, timeout=15)
            data = response.json()
            return data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
        except Exception:
            return []

    items = []
    krx_basis_date = None
    # 최근 7일 중 가장 최신 영업일 데이터 탐색 (KOSPI 기준일에 KOSDAQ도 함께 조회)
    for i in range(7):
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")

        kospi_items = fetch_market("KOSPI", target_date)
        if kospi_items:
            kosdaq_items = fetch_market("KOSDAQ", target_date)
            items = kospi_items + kosdaq_items
            krx_basis_date = target_date
            print(f"✅ [성공] {target_date} 기준 영업일 데이터 조회 성공! (KOSPI {len(kospi_items)}개, KOSDAQ {len(kosdaq_items)}개)")
            break

    if not items:
        print("⚠️ [경고] 데이터 로딩에 실패했습니다.")
        return False

    print(f"✅ [성공] 국토 데이터 수집 완료! 종목 수: {len(items)}개")

    # 10. 주가 변동성 - 최근 12개월 + 오늘, 총 13개 월간 스냅샷 종가로 월간수익률 표준편차 계산
    # 과거 날짜의 시세는 불변값이므로 연-월 단위로 캐싱해서 매번 새로 받아오지 않는다.
    monthly_price_cache = load_json_cache(MONTHLY_PRICE_CACHE_FILE)
    print("[변동성] 최근 12개월 월간 시세 조회를 시작합니다 (캐싱된 과거 월은 스킵)...")
    monthly_snapshots = []
    cache_dirty = False
    for months_back in range(12, -1, -1):
        if months_back == 0:
            # 오늘자 시세는 이미 위에서 받아온 items를 그대로 재사용 (중복 호출 없음)
            monthly_snapshots.append({
                it.get("srtnCd"): float(it.get("clpr", 0) or 0) for it in items if it.get("clpr")
            })
            continue

        anchor = datetime.now() - timedelta(days=months_back * 30)
        cache_key = anchor.strftime("%Y-%m")
        if cache_key in monthly_price_cache:
            monthly_snapshots.append(monthly_price_cache[cache_key])
            continue

        snapshot = {}
        for backoff in range(7):
            target_date = (anchor - timedelta(days=backoff)).strftime("%Y%m%d")
            kospi_snap = fetch_market("KOSPI", target_date)
            if kospi_snap:
                kosdaq_snap = fetch_market("KOSDAQ", target_date)
                snapshot = {
                    it.get("srtnCd"): float(it.get("clpr", 0) or 0)
                    for it in (kospi_snap + kosdaq_snap) if it.get("clpr")
                }
                break
        monthly_price_cache[cache_key] = snapshot
        cache_dirty = True
        monthly_snapshots.append(snapshot)

    if cache_dirty:
        save_json_cache(MONTHLY_PRICE_CACHE_FILE, monthly_price_cache)

    volatility_map = {}
    all_codes = set()
    for snap in monthly_snapshots:
        all_codes.update(snap.keys())
    for code in all_codes:
        prices = [snap.get(code) for snap in monthly_snapshots if snap.get(code)]
        if len(prices) < 4:
            continue
        returns = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        volatility_map[code] = round((variance ** 0.5) * 100, 2)  # %
    print(f"[변동성] {len(volatility_map)}개 종목 변동성 계산 완료.")

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

        dart_financials_cache = load_json_cache(DART_FINANCIALS_CACHE_FILE)
        capital_increase_cache = load_json_cache(CAPITAL_INCREASE_CACHE_FILE)
        induty_code_cache = load_json_cache(INDUTY_CODE_CACHE_FILE)
        admin_issue_cache = load_json_cache(ADMIN_ISSUE_CACHE_FILE)
        cache_hits = 0
        flagged_count = 0

        print(f"[DART] {len(raw_stocks)}개 종목의 재무제표 조회를 시작합니다 (캐싱된 종목은 스킵)...")
        for i, s in enumerate(raw_stocks):
            corp_code = corp_map.get(s["code"])
            if not corp_code:
                continue

            # 금융회사/지주회사/관리종목/관리종목지정우려 여부만 표시해두고 제외는 하지 않음
            # (12.NCAV에서만 실제 제외 필터링, 추천 메뉴에서는 배지로 표기)
            is_fin_holding = is_financial_or_holding(corp_code, s["name"], induty_code_cache)
            is_admin, is_admin_warning = get_admin_status(corp_code, admin_issue_cache)
            if is_fin_holding or is_admin or is_admin_warning:
                flagged_count += 1

            candidates = _build_period_candidates()
            best_period = f"{candidates[0][0]}-{candidates[0][1]}" if candidates else None
            cached_entry = dart_financials_cache.get(corp_code)
            if cached_entry and cached_entry.get("_period") == best_period:
                cache_hits += 1
            fin = fetch_dart_financials(corp_code, cache=dart_financials_cache)
            if not fin:
                continue

            assets = fin.get("assets", 0)
            liabilities = fin.get("liabilities", 0)
            equity = fin.get("equity", assets - liabilities)
            current_assets = fin.get("current_assets", 0)
            current_liabilities = fin.get("current_liabilities", 0)
            borrowings = fin.get("borrowings", liabilities)
            quarter_revenue = fin.get("quarter_revenue")
            quarter_cost_of_sales = fin.get("quarter_cost_of_sales")
            quarter_net_income = fin.get("quarter_net_income")
            quarter_operating_income = fin.get("quarter_operating_income")
            quarter_operating_cf = fin.get("quarter_operating_cf")
            quarter_capex = fin.get("quarter_capex")
            dart_periods_used.append(fin.get("_period"))

            def growth_rate(curr, base):
                if curr is None or base is None or base == 0:
                    return None
                return round((curr - base) / abs(base) * 100, 1)

            # 11~14. 이익 모멘텀 - 전분기대비/전년동기대비 영업이익·순이익 성장률
            op_growth_qoq = growth_rate(quarter_operating_income, fin.get("prev_operating_income"))
            op_growth_yoy = growth_rate(quarter_operating_income, fin.get("operating_income_yoy"))
            ni_growth_qoq = growth_rate(quarter_net_income, fin.get("prev_net_income"))
            ni_growth_yoy = growth_rate(quarter_net_income, fin.get("net_income_yoy"))

            # 6. 신 F-스코어+저PBR - 3개 이진지표(1/0)의 합산 점수
            cap_increase = has_recent_capital_increase(corp_code, capital_increase_cache)
            cap_increase_flag = 0 if cap_increase else (1 if cap_increase is False else None)
            ni_pos_flag = None if quarter_net_income is None else (1 if quarter_net_income >= 0 else 0)
            cf_pos_flag = None if quarter_operating_cf is None else (1 if quarter_operating_cf >= 0 else 0)
            if None in (cap_increase_flag, ni_pos_flag, cf_pos_flag):
                f_score = None
            else:
                f_score = cap_increase_flag + ni_pos_flag + cf_pos_flag

            # 8. 영업이익/차입금(계정명 매칭으로 추출한 총차입금) 비율의 전년동기대비 증가율
            borrowings_yoy = fin.get("borrowings_yoy")
            op_to_debt_now = (quarter_operating_income / borrowings) if (quarter_operating_income is not None and borrowings) else None
            op_to_debt_yoy = (
                fin.get("operating_income_yoy") / borrowings_yoy
                if (fin.get("operating_income_yoy") is not None and borrowings_yoy)
                else None
            )
            op_debt_growth_yoy = growth_rate(op_to_debt_now, op_to_debt_yoy)

            # 9. 자산성장률 (전년동기대비)
            assets_yoy = fin.get("assets_yoy")
            asset_growth_yoy = growth_rate(assets, assets_yoy)

            # 음수/적자도 실제 비율로 계산하고, 데이터 자체가 없거나(None) 분모가 0일 때만 0.0(계산불가 자리값)
            market_cap = s["market_cap"]
            per = round(market_cap / quarter_net_income, 2) if quarter_net_income else 0.0
            pbr = round(market_cap / equity, 2) if equity else 0.0
            psr = round(market_cap / quarter_revenue, 2) if quarter_revenue else 0.0
            pcr = round(market_cap / quarter_operating_cf, 2) if quarter_operating_cf else 0.0
            fcf = (quarter_operating_cf - quarter_capex) if (quarter_operating_cf is not None and quarter_capex is not None) else None
            pfcr = round(market_cap / fcf, 2) if fcf else 0.0
            debt_ratio = round(borrowings / equity * 100, 1) if equity else None  # 17. 차입금비율(=차입금/자본)
            # 지주회사 등 일부 업종은 표준 IFRS 매출/매출원가 계정을 쓰지 않아 데이터가 없을 수 있음
            gpa = (
                round((quarter_revenue - quarter_cost_of_sales) / assets * 100, 1)
                if (assets > 0 and quarter_revenue and quarter_cost_of_sales is not None)
                else None
            )
            ncav_total = current_assets - liabilities
            ncav = int(ncav_total / 100000000)  # 억 단위
            ncav_ratio = round(ncav_total / market_cap, 2) if market_cap > 0 else 0.0

            # 18. 유동비율 = 유동자산 / 유동부채
            current_ratio = round(current_assets / current_liabilities * 100, 1) if current_liabilities > 0 else None

            # 19. 배당율 = 최근 사업보고서 연간 배당총액 / 시가총액
            annual_dividends_paid = fin.get("annual_dividends_paid")
            dividend_yield = (
                round(annual_dividends_paid / market_cap * 100, 2)
                if (annual_dividends_paid is not None and market_cap > 0)
                else None
            )

            valid_stocks.append({
                **s,
                "equity": equity,
                "assets": assets,
                "liabilities": liabilities,
                "current_assets": current_assets,
                "current_liabilities": current_liabilities,
                "borrowings": borrowings,
                "quarter_revenue": quarter_revenue,
                "quarter_cost_of_sales": quarter_cost_of_sales,
                "per": per,
                "pbr": pbr,
                "psr": psr,
                "pcr": pcr,
                "pfcr": pfcr,
                "ncav": ncav,
                "ncav_ratio": ncav_ratio,
                "gpa": gpa,
                "debt_ratio": debt_ratio,
                "op_debt_growth_yoy": op_debt_growth_yoy,
                "asset_growth_yoy": asset_growth_yoy,
                "quarter_net_income": quarter_net_income,
                "quarter_operating_cf": quarter_operating_cf,
                "quarter_capex": quarter_capex,
                "fcf": fcf,
                "quarter_operating_income": quarter_operating_income,
                "prev_operating_income": fin.get("prev_operating_income"),
                "operating_income_yoy": fin.get("operating_income_yoy"),
                "prev_net_income": fin.get("prev_net_income"),
                "net_income_yoy": fin.get("net_income_yoy"),
                "cap_increase_flag": cap_increase_flag,
                "ni_pos_flag": ni_pos_flag,
                "cf_pos_flag": cf_pos_flag,
                "f_score": f_score,
                "op_growth_qoq": op_growth_qoq,
                "op_growth_yoy": op_growth_yoy,
                "ni_growth_qoq": ni_growth_qoq,
                "ni_growth_yoy": ni_growth_yoy,
                "price_volatility": volatility_map.get(s["code"]),
                "current_ratio": current_ratio,
                "is_fin_holding": is_fin_holding,
                "is_admin_issue": is_admin,
                "is_admin_warning": is_admin_warning,
                "dividend_yield": dividend_yield,
            })

            if (i + 1) % 100 == 0:
                print(f"[DART] 진행 중... {i + 1}/{len(raw_stocks)}")

        save_json_cache(DART_FINANCIALS_CACHE_FILE, dart_financials_cache)
        save_json_cache(CAPITAL_INCREASE_CACHE_FILE, capital_increase_cache)
        save_json_cache(INDUTY_CODE_CACHE_FILE, induty_code_cache)
        save_json_cache(ADMIN_ISSUE_CACHE_FILE, admin_issue_cache)
        print(f"[DART] 재무제표 보강 완료: {len(valid_stocks)}개 종목 확보 (캐시 적중 {cache_hits}건, 금융/지주/관리종목 표시 {flagged_count}건)")

    # 13. 슈퍼 가치 4대 통합 스크리너 연산부
    def rank_super_value(pool):
        # 음수가 있을 수 있으므로 원값 오름차순이 아니라 역수의 내림차순으로 순위를 매긴다 (음수도 포함)
        # 0.0은 "계산 불가(데이터 없음)"를 뜻하는 자리값이라 0으로 나누는 걸 막기 위해서만 제외한다
        # dict()로 복사해서 사용 - 전체/소형주 풀을 번갈아 랭킹할 때 같은 종목 객체를 공유하면
        # 나중 호출의 순위가 앞서 계산해둔 결과를 덮어써버리는 문제를 방지한다
        base = [dict(s) for s in pool if s["per"] != 0 and s["pbr"] != 0 and s["psr"] != 0 and s["pfcr"] != 0]
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

    # 6. 신 F-스코어+저PBR 연산부 - 3개 지표(유상증자 없음/순이익>=0/영업CF>=0) 모두 충족하는 종목만, PBR 오름차순
    fscore_base = [s for s in valid_stocks if s["f_score"] == 3 and s["pbr"] > 0]
    fscore_base.sort(key=lambda x: x["pbr"])

    # 15. 슈퍼 퀄리티 연산부 - 6번과 동일한 F-스코어 조건(유상증자 없음/순이익>=0/영업CF>=0)이지만
    # PBR 대신 6.GP/A, 8.영업이익/차입금 증가율, 9.자산성장률, 10.주가변동성 4개 지표로 랭킹
    quality_base = [
        s for s in valid_stocks
        if s["f_score"] == 3
        and s["gpa"] is not None
        and s["op_debt_growth_yoy"] is not None
        and s["asset_growth_yoy"] is not None
        and s["price_volatility"] is not None
    ]
    quality_base = [dict(s) for s in quality_base]
    quality_base.sort(key=lambda x: x["gpa"], reverse=True)  # 6. GP/A 내림차순
    for idx, s in enumerate(quality_base): s["gpa_r"] = idx + 1
    quality_base.sort(key=lambda x: x["op_debt_growth_yoy"], reverse=True)  # 8. 영업이익/차입금 증가율 내림차순
    for idx, s in enumerate(quality_base): s["op_debt_r"] = idx + 1
    quality_base.sort(key=lambda x: x["asset_growth_yoy"])  # 9. 자산성장률 오름차순
    for idx, s in enumerate(quality_base): s["asset_growth_r"] = idx + 1
    quality_base.sort(key=lambda x: x["price_volatility"])  # 10. 주가 변동성 오름차순
    for idx, s in enumerate(quality_base): s["volatility_r"] = idx + 1
    for s in quality_base:
        s["quality_score"] = s["gpa_r"] + s["op_debt_r"] + s["asset_growth_r"] + s["volatility_r"]
    quality_base.sort(key=lambda x: x["quality_score"])

    # 16. 파마의 최종 병기 연산부 - 소형주(시가총액 하위 20%) 한정 + 4.PBR/6.GP/A/9.자산성장률 3개 지표
    # 조건: PBR>0.25, GP/A>0, 자산성장률>-20% / 정렬: PBR 오름차순(=역수 내림차순), GP/A 내림차순, 자산성장률 오름차순
    fama_base = [
        s for s in valid_stocks
        if s["market_cap"] <= smallcap_cutoff
        and s["pbr"] > 0.25
        and s["gpa"] is not None and s["gpa"] > 0
        and s["asset_growth_yoy"] is not None and s["asset_growth_yoy"] > -20
    ]
    fama_base = [dict(s) for s in fama_base]
    fama_base.sort(key=lambda x: x["pbr"])  # 4. PBR 역수의 내림차순 = 원값 오름차순
    for idx, s in enumerate(fama_base): s["pbr_r"] = idx + 1
    fama_base.sort(key=lambda x: x["gpa"], reverse=True)  # 6. GP/A 내림차순
    for idx, s in enumerate(fama_base): s["gpa_r"] = idx + 1
    fama_base.sort(key=lambda x: x["asset_growth_yoy"])  # 9. 자산성장률 오름차순
    for idx, s in enumerate(fama_base): s["asset_growth_r"] = idx + 1
    for s in fama_base:
        s["fama_score"] = s["pbr_r"] + s["gpa_r"] + s["asset_growth_r"]
    fama_base.sort(key=lambda x: x["fama_score"])

    # 18. 슈퍼 가치+퀄리티 연산부
    # 조건: 7.신F-스코어 3점만 / 1,2,4,5(PER,PCR,PBR,PSR) 역수의 내림차순, 6.GP/A 내림차순,
    #       8.영업이익/차입금 증가율 내림차순, 9.자산성장률·10.주가변동성 오름차순
    super_quality_base = [
        s for s in valid_stocks
        if s["f_score"] == 3
        and s["per"] != 0 and s["pcr"] != 0 and s["pbr"] != 0 and s["psr"] != 0
        and s["gpa"] is not None
        and s["op_debt_growth_yoy"] is not None
        and s["asset_growth_yoy"] is not None
        and s["price_volatility"] is not None
    ]
    super_quality_base = [dict(s) for s in super_quality_base]
    super_quality_base.sort(key=lambda x: 1 / x["per"], reverse=True)
    for idx, s in enumerate(super_quality_base): s["per_r"] = idx + 1
    super_quality_base.sort(key=lambda x: 1 / x["pcr"], reverse=True)
    for idx, s in enumerate(super_quality_base): s["pcr_r"] = idx + 1
    super_quality_base.sort(key=lambda x: 1 / x["pbr"], reverse=True)
    for idx, s in enumerate(super_quality_base): s["pbr_r"] = idx + 1
    super_quality_base.sort(key=lambda x: 1 / x["psr"], reverse=True)
    for idx, s in enumerate(super_quality_base): s["psr_r"] = idx + 1
    super_quality_base.sort(key=lambda x: x["gpa"], reverse=True)
    for idx, s in enumerate(super_quality_base): s["gpa_r"] = idx + 1
    super_quality_base.sort(key=lambda x: x["op_debt_growth_yoy"], reverse=True)
    for idx, s in enumerate(super_quality_base): s["op_debt_r"] = idx + 1
    super_quality_base.sort(key=lambda x: x["asset_growth_yoy"])
    for idx, s in enumerate(super_quality_base): s["asset_growth_r"] = idx + 1
    super_quality_base.sort(key=lambda x: x["price_volatility"])
    for idx, s in enumerate(super_quality_base): s["volatility_r"] = idx + 1
    for s in super_quality_base:
        s["super_quality_score"] = (
            s["per_r"] + s["pcr_r"] + s["pbr_r"] + s["psr_r"]
            + s["gpa_r"] + s["op_debt_r"] + s["asset_growth_r"] + s["volatility_r"]
        )
    super_quality_base.sort(key=lambda x: x["super_quality_score"])

    # 20. 밸류+모멘텀 연산부 - 1,3,4,5(PER,PFCR,PBR,PSR) 역수의 내림차순 + 11~14(영업이익·순이익 QoQ/YoY) 내림차순
    value_momentum_base = [
        s for s in valid_stocks
        if s["per"] != 0 and s["pfcr"] != 0 and s["pbr"] != 0 and s["psr"] != 0
        and s["op_growth_qoq"] is not None and s["op_growth_yoy"] is not None
        and s["ni_growth_qoq"] is not None and s["ni_growth_yoy"] is not None
    ]
    value_momentum_base = [dict(s) for s in value_momentum_base]
    value_momentum_base.sort(key=lambda x: 1 / x["per"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["per_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: 1 / x["pfcr"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["pfcr_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: 1 / x["pbr"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["pbr_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: 1 / x["psr"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["psr_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: x["op_growth_qoq"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["op_qoq_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: x["op_growth_yoy"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["op_yoy_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: x["ni_growth_qoq"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["ni_qoq_r"] = idx + 1
    value_momentum_base.sort(key=lambda x: x["ni_growth_yoy"], reverse=True)
    for idx, s in enumerate(value_momentum_base): s["ni_yoy_r"] = idx + 1
    for s in value_momentum_base:
        s["value_momentum_score"] = (
            s["per_r"] + s["pfcr_r"] + s["pbr_r"] + s["psr_r"]
            + s["op_qoq_r"] + s["op_yoy_r"] + s["ni_qoq_r"] + s["ni_yoy_r"]
        )
    value_momentum_base.sort(key=lambda x: x["value_momentum_score"])

    # 21. 슈퍼 퀄리티+모멘텀 연산부
    # 조건: 7.신F-스코어 3점만 / 6,8(GP/A, 영업이익/차입금 증가율) 내림차순, 9,10(자산성장률,주가변동성) 오름차순,
    #       11~14(영업이익·순이익 QoQ/YoY) 내림차순
    quality_momentum_base = [
        s for s in valid_stocks
        if s["f_score"] == 3
        and s["gpa"] is not None
        and s["op_debt_growth_yoy"] is not None
        and s["asset_growth_yoy"] is not None
        and s["price_volatility"] is not None
        and s["op_growth_qoq"] is not None and s["op_growth_yoy"] is not None
        and s["ni_growth_qoq"] is not None and s["ni_growth_yoy"] is not None
    ]
    quality_momentum_base = [dict(s) for s in quality_momentum_base]
    quality_momentum_base.sort(key=lambda x: x["gpa"], reverse=True)
    for idx, s in enumerate(quality_momentum_base): s["gpa_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["op_debt_growth_yoy"], reverse=True)
    for idx, s in enumerate(quality_momentum_base): s["op_debt_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["asset_growth_yoy"])
    for idx, s in enumerate(quality_momentum_base): s["asset_growth_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["price_volatility"])
    for idx, s in enumerate(quality_momentum_base): s["volatility_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["op_growth_qoq"], reverse=True)
    for idx, s in enumerate(quality_momentum_base): s["op_qoq_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["op_growth_yoy"], reverse=True)
    for idx, s in enumerate(quality_momentum_base): s["op_yoy_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["ni_growth_qoq"], reverse=True)
    for idx, s in enumerate(quality_momentum_base): s["ni_qoq_r"] = idx + 1
    quality_momentum_base.sort(key=lambda x: x["ni_growth_yoy"], reverse=True)
    for idx, s in enumerate(quality_momentum_base): s["ni_yoy_r"] = idx + 1
    for s in quality_momentum_base:
        s["quality_momentum_score"] = (
            s["gpa_r"] + s["op_debt_r"] + s["asset_growth_r"] + s["volatility_r"]
            + s["op_qoq_r"] + s["op_yoy_r"] + s["ni_qoq_r"] + s["ni_yoy_r"]
        )
    quality_momentum_base.sort(key=lambda x: x["quality_momentum_score"])

    # 22. 울트라 연산부 - 1,3,4,5(PER,PFCR,PBR,PSR) 역수 내림차순 + 6,8(GP/A,영업이익/차입금증가율) 내림차순
    # + 9,10(자산성장률,변동성) 오름차순 + 7.신F-스코어 3점 필터 + 11~14(영업이익·순이익 QoQ/YoY) 내림차순 (소형주 옵션 지원)
    def rank_ultra(pool):
        base = [
            s for s in pool
            if s["f_score"] == 3
            and s["per"] != 0 and s["pfcr"] != 0 and s["pbr"] != 0 and s["psr"] != 0
            and s["gpa"] is not None
            and s["op_debt_growth_yoy"] is not None
            and s["asset_growth_yoy"] is not None
            and s["price_volatility"] is not None
            and s["op_growth_qoq"] is not None and s["op_growth_yoy"] is not None
            and s["ni_growth_qoq"] is not None and s["ni_growth_yoy"] is not None
        ]
        base = [dict(s) for s in base]
        base.sort(key=lambda x: 1 / x["per"], reverse=True)
        for idx, s in enumerate(base): s["per_r"] = idx + 1
        base.sort(key=lambda x: 1 / x["pfcr"], reverse=True)
        for idx, s in enumerate(base): s["pfcr_r"] = idx + 1
        base.sort(key=lambda x: 1 / x["pbr"], reverse=True)
        for idx, s in enumerate(base): s["pbr_r"] = idx + 1
        base.sort(key=lambda x: 1 / x["psr"], reverse=True)
        for idx, s in enumerate(base): s["psr_r"] = idx + 1
        base.sort(key=lambda x: x["gpa"], reverse=True)
        for idx, s in enumerate(base): s["gpa_r"] = idx + 1
        base.sort(key=lambda x: x["op_debt_growth_yoy"], reverse=True)
        for idx, s in enumerate(base): s["op_debt_r"] = idx + 1
        base.sort(key=lambda x: x["asset_growth_yoy"])
        for idx, s in enumerate(base): s["asset_growth_r"] = idx + 1
        base.sort(key=lambda x: x["price_volatility"])
        for idx, s in enumerate(base): s["volatility_r"] = idx + 1
        base.sort(key=lambda x: x["op_growth_qoq"], reverse=True)
        for idx, s in enumerate(base): s["op_qoq_r"] = idx + 1
        base.sort(key=lambda x: x["op_growth_yoy"], reverse=True)
        for idx, s in enumerate(base): s["op_yoy_r"] = idx + 1
        base.sort(key=lambda x: x["ni_growth_qoq"], reverse=True)
        for idx, s in enumerate(base): s["ni_qoq_r"] = idx + 1
        base.sort(key=lambda x: x["ni_growth_yoy"], reverse=True)
        for idx, s in enumerate(base): s["ni_yoy_r"] = idx + 1
        for s in base:
            s["ultra_score"] = (
                s["per_r"] + s["pfcr_r"] + s["pbr_r"] + s["psr_r"]
                + s["gpa_r"] + s["op_debt_r"] + s["asset_growth_r"] + s["volatility_r"]
                + s["op_qoq_r"] + s["op_yoy_r"] + s["ni_qoq_r"] + s["ni_yoy_r"]
            )
        base.sort(key=lambda x: x["ultra_score"])
        return base

    ultra_base = rank_ultra(valid_stocks)
    ultra_base_smallcap = rank_ultra([s for s in valid_stocks if s["market_cap"] <= smallcap_cutoff])

    # 12. NCAV 청산가치 & 퀄리티 스크리너 연산부
    # 조건: 1) 순유동자산 > 시가총액  2) 최신 분기 순이익 > 0  3) 차입금비율 200% 이하  4) 상위 20개만 노출
    # GP/A 상위 50% 필터는 기본 비적용 - 프론트엔드 체크박스로 켤 때만 별도 리스트(ncav_base_gpa) 적용
    def ncav_filter_base(pool):
        return [
            s for s in pool
            if s["ncav_ratio"] > 1
            and s["quarter_net_income"] is not None and s["quarter_net_income"] > 0
            and s["debt_ratio"] is not None and s["debt_ratio"] <= 200
            and not s["is_fin_holding"] and not s["is_admin_issue"]
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

    def to_eok(v):
        return int(v / 100000000) if v is not None else None

    def package_super_value(base):
        packaged = []
        for idx, s in enumerate(base[:30]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "price": s["price"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "pbr": s["pbr"],
                "pbr_r": s["pbr_r"],
                "per": s["per"],
                "per_r": s["per_r"],
                "pfcr": s["pfcr"],
                "pfcr_r": s["pfcr_r"],
                "psr": s["psr"],
                "psr_r": s["psr_r"],
                "avg_r": round(s["int_score"] / 4, 1),
                "quarter_net_income": to_eok(s["quarter_net_income"]),
                "equity": to_eok(s["equity"]),
                "quarter_operating_cf": to_eok(s["quarter_operating_cf"]),
                "quarter_capex": to_eok(s["quarter_capex"]),
                "fcf": to_eok(s["fcf"]),
                "quarter_revenue": to_eok(s["quarter_revenue"]),
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
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "op_growth_qoq": s["op_growth_qoq"],
                "op_qoq_r": s["op_qoq_r"],
                "op_growth_yoy": s["op_growth_yoy"],
                "op_yoy_r": s["op_yoy_r"],
                "ni_growth_qoq": s["ni_growth_qoq"],
                "ni_qoq_r": s["ni_qoq_r"],
                "ni_growth_yoy": s["ni_growth_yoy"],
                "ni_yoy_r": s["ni_yoy_r"],
                "avg_r": round(s["momentum_score"] / 4, 1),
                "quarter_operating_income": to_eok(s["quarter_operating_income"]),
                "prev_operating_income": to_eok(s["prev_operating_income"]),
                "operating_income_yoy": to_eok(s["operating_income_yoy"]),
                "quarter_net_income": to_eok(s["quarter_net_income"]),
                "prev_net_income": to_eok(s["prev_net_income"]),
                "net_income_yoy": to_eok(s["net_income_yoy"]),
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
            "quarter_net_income": int(s["quarter_net_income"] / 100000000) if s["quarter_net_income"] is not None else None,  # 억 단위
            "quarter_operating_cf": int(s["quarter_operating_cf"] / 100000000) if s["quarter_operating_cf"] is not None else None,  # 억 단위
            "f_score": s["f_score"],
            "pbr": s["pbr"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "equity": int(s["equity"] / 100000000),  # 억 단위
        })

    # 15. 슈퍼 퀄리티 상위 30개 패키징
    quality_value = []
    for idx, s in enumerate(quality_base[:30]):
        quality_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "price": s["price"],
            "cap_increase_flag": s["cap_increase_flag"],
            "ni_pos_flag": s["ni_pos_flag"],
            "cf_pos_flag": s["cf_pos_flag"],
            "gpa": s["gpa"],
            "gpa_r": s["gpa_r"],
            "op_debt_growth_yoy": s["op_debt_growth_yoy"],
            "op_debt_r": s["op_debt_r"],
            "asset_growth_yoy": s["asset_growth_yoy"],
            "asset_growth_r": s["asset_growth_r"],
            "price_volatility": s["price_volatility"],
            "volatility_r": s["volatility_r"],
            "avg_r": round(s["quality_score"] / 4, 1),
        })

    # 16. 파마의 최종 병기 상위 30개 패키징
    fama_value = []
    for idx, s in enumerate(fama_base[:30]):
        fama_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "pbr": s["pbr"],
            "pbr_r": s["pbr_r"],
            "gpa": s["gpa"],
            "gpa_r": s["gpa_r"],
            "asset_growth_yoy": s["asset_growth_yoy"],
            "asset_growth_r": s["asset_growth_r"],
            "avg_r": round(s["fama_score"] / 3, 1),
        })

    # 18. 슈퍼 가치+퀄리티 상위 30개 패키징
    super_quality_value = []
    for idx, s in enumerate(super_quality_base[:30]):
        super_quality_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "per": s["per"], "per_r": s["per_r"],
            "pcr": s["pcr"], "pcr_r": s["pcr_r"],
            "pbr": s["pbr"], "pbr_r": s["pbr_r"],
            "psr": s["psr"], "psr_r": s["psr_r"],
            "gpa": s["gpa"], "gpa_r": s["gpa_r"],
            "op_debt_growth_yoy": s["op_debt_growth_yoy"], "op_debt_r": s["op_debt_r"],
            "asset_growth_yoy": s["asset_growth_yoy"], "asset_growth_r": s["asset_growth_r"],
            "price_volatility": s["price_volatility"], "volatility_r": s["volatility_r"],
            "avg_r": round(s["super_quality_score"] / 8, 1),
        })

    # 20. 밸류+모멘텀 상위 30개 패키징
    value_momentum_value = []
    for idx, s in enumerate(value_momentum_base[:30]):
        value_momentum_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "per": s["per"], "per_r": s["per_r"],
            "pfcr": s["pfcr"], "pfcr_r": s["pfcr_r"],
            "pbr": s["pbr"], "pbr_r": s["pbr_r"],
            "psr": s["psr"], "psr_r": s["psr_r"],
            "op_growth_qoq": s["op_growth_qoq"], "op_qoq_r": s["op_qoq_r"],
            "op_growth_yoy": s["op_growth_yoy"], "op_yoy_r": s["op_yoy_r"],
            "ni_growth_qoq": s["ni_growth_qoq"], "ni_qoq_r": s["ni_qoq_r"],
            "ni_growth_yoy": s["ni_growth_yoy"], "ni_yoy_r": s["ni_yoy_r"],
            "avg_r": round(s["value_momentum_score"] / 8, 1),
        })

    # 21. 슈퍼 퀄리티+모멘텀 상위 30개 패키징
    quality_momentum_value = []
    for idx, s in enumerate(quality_momentum_base[:30]):
        quality_momentum_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "gpa": s["gpa"], "gpa_r": s["gpa_r"],
            "op_debt_growth_yoy": s["op_debt_growth_yoy"], "op_debt_r": s["op_debt_r"],
            "asset_growth_yoy": s["asset_growth_yoy"], "asset_growth_r": s["asset_growth_r"],
            "price_volatility": s["price_volatility"], "volatility_r": s["volatility_r"],
            "op_growth_qoq": s["op_growth_qoq"], "op_qoq_r": s["op_qoq_r"],
            "op_growth_yoy": s["op_growth_yoy"], "op_yoy_r": s["op_yoy_r"],
            "ni_growth_qoq": s["ni_growth_qoq"], "ni_qoq_r": s["ni_qoq_r"],
            "ni_growth_yoy": s["ni_growth_yoy"], "ni_yoy_r": s["ni_yoy_r"],
            "avg_r": round(s["quality_momentum_score"] / 8, 1),
        })

    # 22. 울트라 상위 30개 패키징 (전체 / 소형주 한정)
    def package_ultra(base):
        packaged = []
        for idx, s in enumerate(base[:30]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "price": s["price"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "per": s["per"], "per_r": s["per_r"],
                "pfcr": s["pfcr"], "pfcr_r": s["pfcr_r"],
                "pbr": s["pbr"], "pbr_r": s["pbr_r"],
                "psr": s["psr"], "psr_r": s["psr_r"],
                "gpa": s["gpa"], "gpa_r": s["gpa_r"],
                "op_debt_growth_yoy": s["op_debt_growth_yoy"], "op_debt_r": s["op_debt_r"],
                "asset_growth_yoy": s["asset_growth_yoy"], "asset_growth_r": s["asset_growth_r"],
                "price_volatility": s["price_volatility"], "volatility_r": s["volatility_r"],
                "op_growth_qoq": s["op_growth_qoq"], "op_qoq_r": s["op_qoq_r"],
                "op_growth_yoy": s["op_growth_yoy"], "op_yoy_r": s["op_yoy_r"],
                "ni_growth_qoq": s["ni_growth_qoq"], "ni_qoq_r": s["ni_qoq_r"],
                "ni_growth_yoy": s["ni_growth_yoy"], "ni_yoy_r": s["ni_yoy_r"],
                "avg_r": round(s["ultra_score"] / 12, 1),
            })
        return packaged

    ultra_value = package_ultra(ultra_base)
    ultra_value_smallcap = package_ultra(ultra_base_smallcap)

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
                "debt_ratio": s["debt_ratio"],
                "current_assets": to_eok(s["current_assets"]),
                "liabilities": to_eok(s["liabilities"]),
                "assets": to_eok(s["assets"]),
                "quarter_revenue": to_eok(s["quarter_revenue"]),
                "quarter_cost_of_sales": to_eok(s["quarter_cost_of_sales"]),
                "borrowings": to_eok(s["borrowings"]),
                "equity": to_eok(s["equity"]),
            })
        return packaged

    ncav_value = package_ncav_value(ncav_base)
    ncav_value_gpa = package_ncav_value(ncav_base_gpa)

    # 금융회사/지주회사/관리종목 배지 표기용 (추천 메뉴에서 사용, 12.NCAV 외에는 제외하지 않고 표기만)
    stock_flags = {}
    for s in valid_stocks:
        tags = []
        if s["is_fin_holding"]:
            tags.append("금융/지주")
        if s["is_admin_issue"]:
            tags.append("관리")
        if s["is_admin_warning"]:
            tags.append("관리우려")
        if tags:
            stock_flags[s["name"]] = tags

    # 추천 메뉴의 "PBR/GP-A" 참고 컬럼용 - 전략 소속과 무관하게 종목 자체의 값을 표시
    stock_metrics = {
        s["name"]: {
            "pbr": s["pbr"],
            "gpa": s["gpa"],
            "f_score": s["f_score"],
            "asset_growth_yoy": s["asset_growth_yoy"],
        }
        for s in valid_stocks
    }

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
        "quality_value": quality_value,
        "fama_value": fama_value,
        "super_quality_value": super_quality_value,
        "value_momentum_value": value_momentum_value,
        "quality_momentum_value": quality_momentum_value,
        "ultra_value": ultra_value,
        "ultra_value_smallcap": ultra_value_smallcap,
        "stock_flags": stock_flags,
        "stock_metrics": stock_metrics,
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