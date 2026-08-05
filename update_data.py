import os
import sys
import json
import re
import socket
import threading
import zipfile
import io
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# requests의 timeout=은 TLS 핸드셰이크 구간까지는 안 먹는 경우가 있어, 특정 연결이
# 응답도 끊음도 없이 무한 대기에 빠지면 전체 배치가 그 자리에서 영영 멈춰버린다.
# 소켓 레벨 기본 타임아웃을 걸어 이런 경우에도 강제로 끊기도록 안전장치를 둔다.
socket.setdefaulttimeout(30)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from config_local import DATA_GO_KR_API_KEY as API_KEY, DART_API_KEY
except ImportError:
    API_KEY = os.environ.get("DATA_GO_KR_API_KEY", "")
    DART_API_KEY = os.environ.get("DART_API_KEY", "")

try:
    import kiwoom_api
except Exception:
    kiwoom_api = None

# 매 호출마다 새 TCP 연결을 맺는 대신 세션으로 재사용 - 하루에 수천 번씩 호출하다 보면
# 연결을 계속 새로 맺고 끊는 오버헤드가 커지고, Windows에서는 임시 포트가 고갈돼
# "OSError: [Errno 22] Invalid argument" 같은 소켓 오류로 이어지기도 한다.
_http_session = requests.Session()


def _safe_get(url, params=None, timeout=15):
    """TLS 핸드셰이크가 (요청 timeout=이 적용 안 되는 지점에서) 응답도 끊음도 없이 멈춰버리는
    경우가 실제로 관측돼, socket.setdefaulttimeout()만으로는 못 끊는다 - non-blocking 소켓을
    폴링하며 스핀하는 코드 경로라 소켓 타임아웃 자체가 안 걸린다. 실제 요청을 별도 스레드에서
    돌리고 이쪽에서 wall-clock으로 강제 포기해서, 그 연결 하나 때문에 전체 배치가
    영영 멈추는 일은 없도록 한다. (포기해도 스레드 자체는 데몬으로 남아 조용히 죽는다.)"""
    box = {}

    def _do():
        try:
            box["r"] = _http_session.get(url, params=params, timeout=timeout)
        except Exception as e:
            box["e"] = e

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout + 10)
    if t.is_alive():
        raise TimeoutError(f"{timeout + 10}초 내에 응답이 없어 포기: {url}")
    if "e" in box:
        raise box["e"]
    return box["r"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORP_CODE_CACHE = os.path.join(BASE_DIR, "dart_corp_map.json")

# 외부 API 조회 단계 5곳(시세/베타용 월간 시세·지수/ka10099/ka10032/DART) 각각의 진행률을
# 파일로 남겨서, order_server.py가 수동 실행 중 어느 단계인지 텔레그램으로 보고할 수 있게 한다.
UPDATE_PROGRESS_FILE = os.path.join(BASE_DIR, "update_progress.json")
TOTAL_UPDATE_STAGES = 5


def _write_progress(stage_no, stage, done, total):
    try:
        with open(UPDATE_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "stage": stage,
                "stage_no": stage_no,
                "stage_total": TOTAL_UPDATE_STAGES,
                "done": done,
                "total": total,
                "updated_at": datetime.now().isoformat(),
            }, f)
    except Exception:
        pass

# 업데이트 주기가 긴 데이터는 디스크에 캐싱해서 재검증 시간을 단축한다.
# - DART 재무제표: 분기 단위로만 바뀜 -> 최신 분기로 이미 캐싱돼 있으면 API 호출 스킵
# - 유상증자 여부: 하루 단위로만 갱신
# - 월간 가격 스냅샷: 과거 날짜는 불변값이라 한 번 캐싱하면 재사용
DART_FINANCIALS_CACHE_FILE = os.path.join(BASE_DIR, "cache_dart_financials.json")
CAPITAL_INCREASE_CACHE_FILE = os.path.join(BASE_DIR, "cache_capital_increase.json")
MONTHLY_PRICE_CACHE_FILE = os.path.join(BASE_DIR, "cache_monthly_price.json")
MONTHLY_INDEX_CACHE_FILE = os.path.join(BASE_DIR, "cache_monthly_index.json")
INDUTY_CODE_CACHE_FILE = os.path.join(BASE_DIR, "cache_induty_code.json")
ADMIN_ISSUE_CACHE_FILE = os.path.join(BASE_DIR, "cache_admin_issue.json")
TRADE_AMT_CACHE_FILE  = os.path.join(BASE_DIR, "cache_trade_amt.json")
STOCK_RIGHTS_CACHE_FILE = os.path.join(BASE_DIR, "cache_stock_rights.json")

# 우선주는 종목명이 관례적으로 "우"/"우B" 등으로 끝남(예: 삼성전자우, 현대차2우B).
# 전환우선주는 "대한제당3우B(전환)"처럼 뒤에 괄호 설명이 붙기도 해서 그 형태도 함께 잡는다.
# 우선주는 시가총액이 보통주와 별도이지만 DART 재무제표(순이익/자기자본 등)는 보통주와
# 공유하므로, 그대로 PER/PBR 등을 계산하면 실제보다 훨씬 저평가된 것처럼 왜곡된다.
_PREFERRED_STOCK_RE = re.compile(r".*우[A-Z]?(\([^)]*\))?$")


def is_preferred_stock(name: str) -> bool:
    return bool(_PREFERRED_STOCK_RE.match(name or ""))


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
    "issued_capital": ("BS", "ifrs-full_IssuedCapital"),  # 자본금 - 자본잠식률 계산용
    "retained_earnings": ("BS", "ifrs-full_RetainedEarnings"),  # 이익잉여금 - PBR 저평가/자본훼손 판별용
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


CORP_CODE_CACHE_MAX_AGE_DAYS = 7  # 신규 상장사가 DART 목록에 반영되도록 주기적으로 재조회


def get_corp_code_map():
    """종목코드(stock_code) -> DART corp_code 매핑. 로컬 캐시 사용(신규 상장 반영을 위해 주기적 갱신)."""
    if os.path.exists(CORP_CODE_CACHE):
        age_days = (datetime.now().timestamp() - os.path.getmtime(CORP_CODE_CACHE)) / 86400
        if age_days < CORP_CODE_CACHE_MAX_AGE_DAYS:
            with open(CORP_CODE_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)

    try:
        r = _safe_get(
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
    except Exception as e:
        if os.path.exists(CORP_CODE_CACHE):
            print(f"⚠️ [경고] DART corp_code 목록 재조회 실패, 기존(만료된) 캐시 사용: {e}")
            with open(CORP_CODE_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        raise


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
    """오늘 날짜 기준으로 이미 공시 마감이 지난 보고서들을 최신순으로 나열.

    마감 전이라도 가장 가까운 다음 보고서를 맨 앞 후보로 하나 더 끼워 넣는다 — 이미
    조기 제출한 기업은 fetch_dart_financials가 이 후보에서 바로 데이터를 찾아 반영하고,
    아직 제출 전인 기업은 DART가 status!="000"을 반환하므로 자동으로 다음 후보(마감이
    지난 직전 확정 보고서)로 폴백된다.
    """
    today = datetime.now()
    candidates = []
    upcoming = None  # (due_date, bsns_year, reprt_code, months) — 마감 전인 것 중 가장 임박한 것
    for bsns_year in (today.year, today.year - 1, today.year - 2):
        for reprt_code, months, due_month, due_day, due_year_offset in REPORT_PERIODS:
            due_date = datetime(bsns_year + due_year_offset, due_month, due_day)
            if due_date <= today:
                period_end = datetime(bsns_year, ((months - 1) // 3 + 1) * 3 if months < 12 else 12, 1)
                candidates.append((period_end, bsns_year, reprt_code, months))
            elif upcoming is None or due_date < upcoming[0]:
                upcoming = (due_date, bsns_year, reprt_code, months)

    candidates.sort(key=lambda c: c[0], reverse=True)
    result = [(year, code, months) for (_end, year, code, months) in candidates]
    if upcoming is not None:
        _, up_year, up_code, up_months = upcoming
        result.insert(0, (up_year, up_code, up_months))
    return result


def has_recent_capital_increase(corp_code, cache):
    """최근 1년간 유상증자결정 공시(주요사항보고서) 여부. 하루 단위로 캐싱.

    제목에 "철회"가 들어간 공시(유상증자결정 철회)까지 유상증자로 오탐하면 안 되므로 제외한다.
    """
    today_str = datetime.now().strftime("%Y%m%d")
    cached = cache.get(corp_code)
    if cached and cached.get("date") == today_str:
        return cached.get("value")

    end_de = datetime.now()
    bgn_de = end_de - timedelta(days=365)
    try:
        r = _safe_get(
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
        value = any(
            "유상증자" in (nm := (item.get("report_nm") or "").replace(" ", "")) and "철회" not in nm
            for item in data.get("list", []) or []
        )

    cache[corp_code] = {"date": today_str, "value": value}
    return value


# 금융업(은행/보험/증권 등 KSIC 대분류 K, 64~66) + 지주회사(DART induty_code 64992) 제외용
FINANCIAL_INDUTY_PREFIXES = ("64", "65", "66")
HOLDING_NAME_PATTERNS = ("홀딩스", "홀딩", "지주")
# 실질적으로 그룹 지주회사 역할을 하지만, DART 업종코드가 옛 사업부(제조업 등) 코드로 남아있고
# 회사명에도 "지주"/"홀딩스"가 없어 위 패턴으로 못 잡는 케이스의 수동 예외 목록.
# (공정위 지정 지주회사 현황 기준 - 최신 지정/해제 여부는 주기적으로 재확인 필요)
HOLDING_NAME_EXACT = {
    "한화", "SK", "GS", "LS", "CJ", "두산", "효성", "코오롱", "DL", "한진칼",
}


def fetch_induty_code(corp_code, cache):
    """DART 회사개요에서 업종코드(induty_code)를 가져온다. 업종은 거의 안 바뀌므로 영구 캐싱."""
    if corp_code in cache:
        return cache[corp_code]
    try:
        r = _safe_get(
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
    if name in HOLDING_NAME_EXACT:
        return True
    return any(p in name for p in HOLDING_NAME_PATTERNS)


def get_admin_status(corp_code, cache):
    """관리종목·관리우려·거래정지·투자경고 현재 상태를 판정.
    최근 2년 거래소공시(I)에서 가장 최근 지정/해제 공시로 판단. 하루 단위 캐싱."""
    today_str = datetime.now().strftime("%Y%m%d")
    cached = cache.get(corp_code)
    if cached and cached.get("date") == today_str:
        return cached.get("issue"), cached.get("warning"), cached.get("halt"), cached.get("inv_warn")

    end_de = datetime.now()
    bgn_de = end_de - timedelta(days=730)
    try:
        r = _safe_get(
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
        return None, None, None, None

    if data.get("status") == "013":
        issue, warning, halt, inv_warn = False, False, False, False
    elif data.get("status") != "000":
        return None, None, None, None
    else:
        # 각 항목별 (날짜, 지정여부) 이벤트 리스트
        admin_events = []    # 관리종목
        halt_events = []     # 거래정지
        inv_warn_events = [] # 투자경고/위험/단기과열

        for item in data.get("list", []) or []:
            nm = (item.get("report_nm") or "").replace(" ", "")
            dt = item.get("rcept_dt", "")

            if "관리종목" in nm and "지정" in nm:
                kind = "warning" if "우려" in nm else ("release" if "해제" in nm else "issue")
                admin_events.append((dt, kind))

            if "매매거래정지" in nm or ("거래정지" in nm and "해제" not in nm and "우려" not in nm):
                halt_events.append((dt, "issue"))
            elif "매매거래재개" in nm or ("거래정지해제" in nm):
                halt_events.append((dt, "release"))

            if any(kw in nm for kw in ("투자경고", "투자위험", "단기과열지정")):
                inv_warn_events.append((dt, "issue"))
            elif any(kw in nm for kw in ("투자경고해제", "투자위험해제", "단기과열해제")):
                inv_warn_events.append((dt, "release"))

        def is_active(events):
            if not events:
                return False
            events.sort(key=lambda e: e[0])
            return events[-1][1] == "issue"

        admin_events.sort(key=lambda e: e[0])
        latest_admin = admin_events[-1][1] if admin_events else None
        issue = latest_admin == "issue"
        warning = latest_admin == "warning"
        halt = is_active(halt_events)
        inv_warn = is_active(inv_warn_events)

    cache[corp_code] = {"date": today_str, "issue": issue, "warning": warning, "halt": halt, "inv_warn": inv_warn}
    return issue, warning, halt, inv_warn


def _fetch_period_accounts(corp_code, year, reprt_code, fs_div, keys):
    """주어진 보고서에서 지정된 계정들의 (당기 분기단독, 당기 누적) 값을 한 번에 가져온다."""
    try:
        r = _safe_get(
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
            # 손익계산서는 회사마다 "IS"(손익계산서) 또는 "CIS"(포괄손익계산서)로 다르게 공시되므로 둘 다 인정
            sj_matches = sj_div == target_sj or (target_sj == "IS" and sj_div == "CIS")
            if sj_matches and row.get("account_id") == account_id and key not in result:
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


BASELINE_KEYS = ("operating_income", "net_income", "operating_cf", "capex", "revenue", "cost_of_sales")


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
    """전년동기 시점의 자산총계/부채총계/자기자본/이익잉여금 스냅샷
    (자산성장률·차입금 YoY, ROE 추이·이익잉여금 추이 판별용)."""
    try:
        r = _safe_get(
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
        for key in ("assets", "liabilities", "equity", "retained_earnings"):
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
        cached = cache.get(corp_code)
        # 스키마 누락 감지 - 이번에 새로 추가한 필드(이익잉여금 등)가 캐시에 아예 없으면, 밑의
        # _upcoming_checked 단축 경로를 전부 건너뛰고 무조건 실제 재조회해서 백필한다. (지난번
        # _upcoming_checked 백필 때는 이 케이스도 같이 걸려서 "확인한 것으로 기록만 하고 반환"
        # 분기를 타 버렸고, 그 결과 새 필드가 채워지지 않는 채로 7일 재차단되는 버그가 있었다.)
        needs_schema_refetch = bool(cached) and "retained_earnings" not in cached
        if cached and not needs_schema_refetch and cached.get("_period") == f"{candidates[0][0]}-{candidates[0][1]}":
            return cached  # 조기제출분까지 이미 최신 보고서로 캐싱됨
        # 맨 앞 후보는 "마감 전 다음 보고서"라 대다수 기업은 아직 미제출 상태다. 이걸 기준으로
        # 캐시를 무효화하면 매일 전종목이 캐시 미스로 재조회되므로, 확정 보고서(두 번째 후보)로
        # 캐싱된 종목은 조기제출 여부만 7일에 한 번 재확인하고 그 사이엔 캐시를 그대로 쓴다.
        if cached and not needs_schema_refetch and len(candidates) > 1 and cached.get("_period") == f"{candidates[1][0]}-{candidates[1][1]}":
            if "_upcoming_checked" not in cached:
                # 이 검증 로직이 생기기 전에 저장된 레거시 캐시 항목 - "확인한 적 없음"으로 보고
                # 즉시 재조회하면 전종목이 한꺼번에 API를 두드려 요청 폭주(429)가 발생한다.
                # 오늘 확인한 것으로 우선 기록만 해두고, 다음 주기부터 정상적으로 7일 체크한다.
                cached["_upcoming_checked"] = datetime.now().strftime("%Y%m%d")
                return cached
            last_checked = str(cached.get("_upcoming_checked") or "")
            try:
                age_days = (datetime.now() - datetime.strptime(last_checked, "%Y%m%d")).days
            except ValueError:
                age_days = 999
            if age_days < 7:
                return cached

    for year, reprt_code, months in candidates:
        for fs_div in ("CFS", "OFS"):
            try:
                r = _safe_get(
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
                    # 손익계산서는 회사마다 "IS" 또는 "CIS"(포괄손익계산서)로 다르게 공시되므로 둘 다 인정
                    sj_matches = sj_div == target_sj or (target_sj == "IS" and sj_div == "CIS")
                    if sj_matches and account_id == target_id and key not in values:
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

            baseline = None if reprt_code == "11013" else fetch_baseline_period(corp_code, year, reprt_code, fs_div)

            # DART 손익계산서(IS) 항목의 thstrm_amount는 분기/반기/3분기 보고서에서는 이미
            # "당기 1개 분기" 단독값이다 (반기보고서도 6개월 누적이 아니라 2분기 단독값).
            # 사업보고서(11011)만 thstrm_amount가 연간 총액이므로, 같은 해 3분기 누적(baseline)을
            # 빼서 4분기 단독값으로 맞춘다 — 그대로 쓰면 PER/PSR/GPA가 다른 종목 대비 4배
            # 스케일로 어긋난 채 순위 경쟁을 하게 된다. baseline 조회 실패 시에는 quarter_ 값을
            # 채우지 않아 해당 지표 계산에서 제외한다(왜곡된 값보다 결측이 안전).
            for key in ("revenue", "cost_of_sales", "net_income", "operating_income"):
                if key not in values:
                    continue
                if reprt_code == "11011":
                    if baseline and key in baseline:
                        values[f"quarter_{key}"] = values[key] - baseline[key]["cum"]
                else:
                    values[f"quarter_{key}"] = values[key]

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

            # 전년동기 시점 자산총계/부채총계/자기자본/이익잉여금 - 자산성장률, 영업이익/차입금 비율 YoY,
            # PBR 저평가/자본훼손 판별(ROE 추이·이익잉여금 추이)에 사용
            bs_yoy = fetch_bs_snapshot_yoy(corp_code, year, reprt_code, fs_div)
            if bs_yoy:
                values["assets_yoy"] = bs_yoy.get("assets")
                values["borrowings_yoy"] = bs_yoy.get("borrowings")
                values["equity_yoy"] = bs_yoy.get("equity")
                values["retained_earnings_yoy"] = bs_yoy.get("retained_earnings")

            # 19. 배당율 - 분기 단독값이 아니라 가장 최근 사업보고서의 연간 배당총액 기준
            if reprt_code == "11011":
                values["annual_dividends_paid"] = values.get("dividends_paid")
            else:
                values["annual_dividends_paid"] = fetch_latest_annual_dividend(corp_code, fs_div)

            values["_period"] = f"{year}-{reprt_code}"
            values["_upcoming_checked"] = datetime.now().strftime("%Y%m%d")
            # 계정 자체가 없어서 못 찾은 경우와 "아직 이 필드를 도입하기 전이라 시도조차 안 한 캐시"를
            # 구분할 수 있도록, 시도는 했지만 못 찾은 경우도 명시적으로 None을 남긴다(키 자체는 항상 존재).
            values.setdefault("retained_earnings", None)
            if cache is not None:
                cache[corp_code] = values
            return values

    return None


# DART 거래소공시(pblntf_ty="I")의 report_nm 키워드로 액면분할/병합 결정 공시를 찾는다.
# 처음엔 공공데이터 주식권리일정정보(예탁결제원 확정 일정)로 이걸 잡으려 했으나, 실제 DART
# 공시 대비 1/3만 잡혀(제3자배정 유상증자와 같은 구조적 누락) DART 직접 조회로 되돌렸다.
SPLIT_KEYWORDS = {"주식분할결정": "액면분할", "주식병합결정": "액면병합"}


def _update_rights_cache():
    """지난 실행 이후 새로 발생한 액면분할/병합 결정 공시만 누적 수집.

    종목코드 없이 전체 종목을 조회하면 DART가 검색기간을 3개월로 제한하므로, 최초 백필은
    3개월(약 85일)씩 나눠서 하고 그 뒤로는 어제~오늘만 확인하면 된다(캐시라 재실행해도 안전).
    """
    cache = load_json_cache(STOCK_RIGHTS_CACHE_FILE)
    split_events = cache.setdefault("split_events", {})  # {종목명: [{"date":.., "type":"액면분할"}, ...]}
    last_fetched = cache.get("last_fetched")
    start = (
        datetime.strptime(last_fetched, "%Y%m%d") + timedelta(days=1)
        if last_fetched else datetime.now() - timedelta(days=395)
    )
    today = datetime.now()
    if start > today:
        return cache

    window = timedelta(days=85)  # DART 3개월(무-corp_code) 제한보다 여유있게
    end_de = today
    try:
        while end_de >= start:
            bgn_de = max(end_de - window, start)
            page = 1
            while True:
                r = _safe_get(
                    "https://opendart.fss.or.kr/api/list.json",
                    params={
                        "crtfc_key": DART_API_KEY,
                        "bgn_de": bgn_de.strftime("%Y%m%d"),
                        "end_de": end_de.strftime("%Y%m%d"),
                        "pblntf_ty": "I",  # 거래소공시 - 액면분할/병합 결정은 여기로 공시됨
                        "page_count": 100,
                        "page_no": page,
                    },
                    timeout=20,
                )
                data = r.json()
                if data.get("status") not in ("000", "013"):  # 013=조회결과 없음(정상)
                    break
                items = data.get("list", []) or []
                for it in items:
                    nm = (it.get("report_nm") or "").strip()
                    if "정정" in nm:
                        continue
                    for kw, label in SPLIT_KEYWORDS.items():
                        if kw in nm:
                            name = it.get("corp_name", "")
                            ev = {"date": it.get("rcept_dt", ""), "type": label}
                            if ev not in split_events.setdefault(name, []):
                                split_events[name].append(ev)
                            break
                if page * 100 >= int(data.get("total_count", 0)):
                    break
                page += 1
            end_de = bgn_de - timedelta(days=1)
    except Exception:
        pass  # 실패해도 last_fetched를 안 미뤄서 다음 실행에서 전체 구간을 재시도함
    else:
        cache["last_fetched"] = today.strftime("%Y%m%d")
    save_json_cache(STOCK_RIGHTS_CACHE_FILE, cache)
    return cache


def _has_split_between(split_events, name, start_dt, end_dt):
    """name 종목이 [start_dt, end_dt] 구간에 액면분할/병합 이벤트가 있었는지."""
    for ev in split_events.get(name, []):
        try:
            ev_dt = datetime.strptime(ev["date"], "%Y%m%d")
        except ValueError:
            continue
        if start_dt <= ev_dt <= end_dt:
            return True
    return False


def fetch_krx_market_data():
    today_str = datetime.now().strftime("%Y%m%d")
    print("[시스템] 공공데이터포털(금융위) API 연동을 시작합니다...")
    
    url = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
    index_url = "https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex"

    def fetch_index_snapshot(target_date):
        """해당 날짜의 코스피/코스닥 종합지수 주가. 10. 주가변동성을 베타로 계산하는 데 쓰는 시장수익률."""
        try:
            r = _safe_get(index_url, params={
                "serviceKey": API_KEY, "resultType": "json",
                "numOfRows": "300", "pageNo": "1", "basDt": target_date,
            }, timeout=15)
            items = r.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
        except Exception:
            return {}
        snap = {}
        for it in items:
            if it.get("idxNm") == "코스피" and it.get("idxCsf") == "KOSPI시리즈":
                snap["kospi"] = float(it.get("clpr", 0) or 0)
            elif it.get("idxNm") == "코스닥" and it.get("idxCsf") == "KOSDAQ시리즈":
                snap["kosdaq"] = float(it.get("clpr", 0) or 0)
        return snap

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
            response = _safe_get(url, params=params, timeout=15)
            data = response.json()
            return data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
        except Exception:
            return []

    items = []
    krx_basis_date = None
    _write_progress(1, "시세 데이터 조회", 0, 1)
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
    _write_progress(1, "시세 데이터 조회", 1, 1)

    # 10. 주가 변동성(베타) - 최근 12개월 + 오늘, 총 13개 월간 스냅샷으로 종목수익률을 같은 시점의
    # 코스피/코스닥 지수수익률과 비교해서 베타(시장 대비 민감도)를 계산한다.
    # 과거 날짜의 시세는 불변값이므로 연-월 단위로 캐싱해서 매번 새로 받아오지 않는다.
    monthly_price_cache = load_json_cache(MONTHLY_PRICE_CACHE_FILE)
    monthly_index_cache = load_json_cache(MONTHLY_INDEX_CACHE_FILE)
    print("[베타] 최근 12개월 월간 시세/지수 조회를 시작합니다 (캐싱된 과거 월은 스킵)...")
    monthly_snapshots = []      # [{종목코드: 종가}, ...] 13개, 과거->현재 순
    monthly_index_snapshots = []  # [{"kospi":.., "kosdaq":..}, ...] 13개, 위와 같은 시점
    position_dates = []         # 위와 같은 순서의 각 스냅샷 대략적 기준일 - 액면분할 구간 판별용
    cache_dirty = False
    index_cache_dirty = False
    for idx, months_back in enumerate(range(12, -1, -1)):
        _write_progress(2, "베타용 월간 시세/지수 조회", idx + 1, 13)
        if months_back == 0:
            # 오늘자 시세는 이미 위에서 받아온 items를 그대로 재사용 (중복 호출 없음)
            monthly_snapshots.append({
                it.get("srtnCd"): float(it.get("clpr", 0) or 0) for it in items if it.get("clpr")
            })
            monthly_index_snapshots.append(fetch_index_snapshot(krx_basis_date))
            position_dates.append(datetime.now())
            continue

        anchor = datetime.now() - timedelta(days=months_back * 30)
        position_dates.append(anchor)
        cache_key = anchor.strftime("%Y-%m")
        if cache_key in monthly_price_cache:
            monthly_snapshots.append(monthly_price_cache[cache_key])
        else:
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

        if cache_key in monthly_index_cache:
            monthly_index_snapshots.append(monthly_index_cache[cache_key])
        else:
            index_snap = {}
            for backoff in range(7):
                target_date = (anchor - timedelta(days=backoff)).strftime("%Y%m%d")
                index_snap = fetch_index_snapshot(target_date)
                if index_snap.get("kospi") and index_snap.get("kosdaq"):
                    break
            monthly_index_cache[cache_key] = index_snap
            index_cache_dirty = True
            monthly_index_snapshots.append(index_snap)

    if cache_dirty:
        save_json_cache(MONTHLY_PRICE_CACHE_FILE, monthly_price_cache)
    if index_cache_dirty:
        save_json_cache(MONTHLY_INDEX_CACHE_FILE, monthly_index_cache)

    # 같은 시점끼리의 월간 시장수익률 (포지션 i: i-1->i 구간)
    def market_returns(key):
        prices = [snap.get(key) for snap in monthly_index_snapshots]
        rets = []
        for i in range(1, len(prices)):
            if prices[i - 1] and prices[i]:
                rets.append((i, prices[i] / prices[i - 1] - 1))
        return dict(rets)  # {포지션: 수익률}

    kospi_rets = market_returns("kospi")
    kosdaq_rets = market_returns("kosdaq")
    stock_market_map = {it.get("srtnCd"): it.get("mrktCtg", "KOSPI") for it in items}
    code_to_name = {it.get("srtnCd"): it.get("itmsNm", "") for it in items}

    # 액면분할/병합이 있었던 달은 종가가 조정 안 된 채로 반토막/배로 튀어 베타를 왜곡하므로,
    # 그 구간(포지션 i-1~i)만 베타 회귀에서 제외한다(해당 종목의 다른 구간은 그대로 사용).
    rights_cache = _update_rights_cache()
    split_events = rights_cache.get("split_events", {})

    volatility_map = {}  # 이름은 유지하지만 실제 값은 베타
    all_codes = set()
    for snap in monthly_snapshots:
        all_codes.update(snap.keys())
    for code in all_codes:
        prices = [snap.get(code) for snap in monthly_snapshots]
        mkt_rets = kospi_rets if stock_market_map.get(code) == "KOSPI" else kosdaq_rets
        name = code_to_name.get(code, "")
        pairs = []
        for i in range(1, len(prices)):
            if prices[i - 1] and prices[i] and i in mkt_rets:
                if name and _has_split_between(split_events, name, position_dates[i - 1], position_dates[i]):
                    continue
                pairs.append((prices[i] / prices[i - 1] - 1, mkt_rets[i]))
        if len(pairs) < 4:
            continue
        stock_rets = [p[0] for p in pairs]
        mkt_only = [p[1] for p in pairs]
        mean_s = sum(stock_rets) / len(stock_rets)
        mean_m = sum(mkt_only) / len(mkt_only)
        cov = sum((s - mean_s) * (m - mean_m) for s, m in pairs) / len(pairs)
        var_m = sum((m - mean_m) ** 2 for m in mkt_only) / len(mkt_only)
        if var_m == 0:
            continue
        volatility_map[code] = round(cov / var_m, 2)  # 베타
    print(f"[베타] {len(volatility_map)}개 종목 베타 계산 완료.")

    # ka10099로 전 종목 플래그+시세(현재가·시가총액) 조회 - 공공데이터 시세는 확정치가 익영업일
    # 오후 1시에나 올라와 최소 2영업일 지연되므로, 스크리닝에 직접 쓰는 오늘자 종가/시가총액은
    # 실시간인 키움 값으로 대체한다(재무제표·과거 월간 시세·지수는 지연이 문제되지 않아 그대로 사용).
    # 뒤에서 관리종목 등 플래그 판정에도 재사용하므로 여기서 한 번만 조회한다.
    print("[ka10099] 전 종목 관리종목·거래정지·투자경고 플래그 및 시세 조회 중...")
    _write_progress(3, "ka10099 전 종목 플래그/시세 조회", 0, 1)
    ka10099_count = 0
    update_warnings = []  # 텔레그램 요약 알림용 - 실패/경고 항목 누적
    try:
        if kiwoom_api is None:
            raise RuntimeError("kiwoom_api 모듈 로드 실패")
        stock_flags_map = kiwoom_api.get_stock_list_flags()
        print(f"[ka10099] {len(stock_flags_map)}개 종목 플래그/시세 조회 완료.")
        ka10099_count = len(stock_flags_map)
    except Exception as e:
        print(f"⚠️ [ka10099] 플래그/시세 조회 실패, 공공데이터 시세로 폴백: {e}")
        update_warnings.append(f"ka10099 플래그/시세 조회 실패(공공데이터 시세로 폴백): {e}")
        stock_flags_map = {}
    # ka10099가 0개 반환 시(주말·장외 등) 거래정지/투자경고는 DART 폴백 없이 건너뜀
    # — DART 거래정지 파싱은 오판정이 많아 ka10099 없이는 관리종목만 판정
    ka10099_available = len(stock_flags_map) > 0
    _write_progress(3, "ka10099 전 종목 플래그/시세 조회", 1, 1)

    # 시세 API에는 PER/PBR/PSR/EPS 항목이 없으므로 가격/시총/종목코드만 추출.
    # 가격·시가총액은 ka10099(실시간) 우선, 그 종목이 없거나 값이 0이면 공공데이터로 폴백.
    raw_stocks = []
    for item in items:
        try:
            code = item.get("srtnCd", "")
            name = item.get("itmsNm", "")
            kiwoom_flag = stock_flags_map.get(code)
            if kiwoom_flag and kiwoom_flag.get("price") and kiwoom_flag.get("market_cap"):
                price = kiwoom_flag["price"]
                market_cap = kiwoom_flag["market_cap"]
            else:
                price = int(item.get("clpr", 0)) if item.get("clpr") else 0
                market_cap = float(item.get("mrktTotAmt", 0) or 0)
            if price <= 0 or market_cap <= 0 or not code or is_preferred_stock(name):
                continue
            raw_stocks.append({
                "name": name,
                "price": price,
                "code": code,
                "market_cap": market_cap,
                "trade_value": float(item.get("trPrc", 0) or 0),
            })
        except Exception:
            continue

    # ------------------------------------------------------------------------
    # DART 재무제표 연동 - 시가총액 대비 재무수치로 PER/PBR/PSR/PFCR을 직접 계산
    # ------------------------------------------------------------------------
    valid_stocks = []
    dart_periods_used = []
    ka10032_count = 0
    cache_hits = 0
    flagged_count = 0
    if not DART_API_KEY:
        print("⚠️ [경고] DART_API_KEY가 설정되지 않아 가치지표를 계산할 수 없습니다.")
        update_warnings.append("DART_API_KEY 미설정 - 가치지표 계산 안 됨")
    else:
        try:
            corp_map = get_corp_code_map()
        except Exception as e:
            print(f"⚠️ [경고] DART corp_code 매핑 실패: {e}")
            update_warnings.append(f"DART corp_code 매핑 실패: {e}")
            corp_map = {}

        dart_financials_cache = load_json_cache(DART_FINANCIALS_CACHE_FILE)
        capital_increase_cache = load_json_cache(CAPITAL_INCREASE_CACHE_FILE)
        induty_code_cache = load_json_cache(INDUTY_CODE_CACHE_FILE)
        admin_issue_cache = load_json_cache(ADMIN_ISSUE_CACHE_FILE)
        trade_amt_cache = load_json_cache(TRADE_AMT_CACHE_FILE)
        cache_hits = 0
        flagged_count = 0

        # ka10099 플래그/시세는 위에서 raw_stocks 만들 때 이미 조회해둔 stock_flags_map을 재사용한다.

        # ka10032 bulk 거래대금 조회 (per-stock ka10086 루프 대체)
        print("[ka10032] 전 종목 거래대금 일괄 조회 중...")
        _write_progress(4, "ka10032 전 종목 거래대금 조회", 0, 1)
        try:
            if kiwoom_api is None:
                raise RuntimeError("kiwoom_api 모듈 로드 실패")
            bulk_trade_map = kiwoom_api.get_bulk_trade_amounts()
            print(f"[ka10032] {len(bulk_trade_map)}개 종목 거래대금 조회 완료.")
            ka10032_count = len(bulk_trade_map)
            # 캐시 갱신: 오늘 날짜로 일괄 업데이트
            for code, amt in bulk_trade_map.items():
                trade_amt_cache[code] = {"date": today_str, "amt": amt}
        except Exception as e:
            print(f"⚠️ [ka10032] 거래대금 조회 실패, 기존 캐시 사용: {e}")
            update_warnings.append(f"ka10032 거래대금 조회 실패(기존 캐시 사용): {e}")
            bulk_trade_map = {}
        _write_progress(4, "ka10032 전 종목 거래대금 조회", 1, 1)

        print(f"[DART] {len(raw_stocks)}개 종목의 재무제표 조회를 시작합니다 (캐싱된 종목은 스킵)...")
        _write_progress(5, "DART 재무제표 조회", 0, len(raw_stocks))
        for i, s in enumerate(raw_stocks):
            corp_code = corp_map.get(s["code"])
            if not corp_code:
                continue

            # 금융회사/지주회사/관리종목/관리종목지정우려 여부만 표시해두고 제외는 하지 않음
            # (12.NCAV에서만 실제 제외 필터링, 추천 메뉴에서는 배지로 표기)
            is_fin_holding = is_financial_or_holding(corp_code, s["name"], induty_code_cache)
            if s["code"] in stock_flags_map:
                # ka10099 결과 우선 사용
                f = stock_flags_map[s["code"]]
                is_admin         = f["is_admin"]
                is_admin_warning = f["is_admin_warning"]
                is_halt          = f["is_halt"]
                is_inv_warn      = f["is_inv_warn"]
                is_margin100     = f.get("is_margin100", False)
                is_delisting     = f.get("is_delisting", False)
            elif ka10099_available:
                # ka10099 조회됐으나 해당 종목 미포함 → 정상 종목으로 간주
                is_admin, is_admin_warning, is_halt, is_inv_warn = False, False, False, False
                is_margin100 = False
                is_delisting = False
            else:
                # ka10099 미사용(주말 등) → DART로 관리종목만 판정, 거래정지/투자경고는 False
                is_admin, is_admin_warning, _, _ = get_admin_status(corp_code, admin_issue_cache)
                is_halt, is_inv_warn = False, False
                is_margin100 = False
                is_delisting = False

            # 거래대금: ka10032 bulk 조회 결과 우선, 없으면 캐시, 없으면 기존값
            cached_ta = trade_amt_cache.get(s["code"])
            avg_trade_amt = cached_ta.get("amt", 0) if cached_ta else s.get("trade_value", 0)
            if is_fin_holding or is_admin or is_admin_warning or is_halt or is_inv_warn:
                flagged_count += 1

            candidates = _build_period_candidates()
            # 맨 앞 후보(마감 전 다음 보고서)와 확정 보고서(두 번째 후보) 둘 다 캐시 유효로 집계
            valid_periods = {f"{y}-{c}" for (y, c, _m) in candidates[:2]}
            cached_entry = dart_financials_cache.get(corp_code)
            if cached_entry and cached_entry.get("_period") in valid_periods:
                cache_hits += 1
            fin = fetch_dart_financials(corp_code, cache=dart_financials_cache)
            if not fin:
                continue

            assets = fin.get("assets", 0)
            liabilities = fin.get("liabilities", 0)
            equity = fin.get("equity", assets - liabilities)
            issued_capital = fin.get("issued_capital")
            # 자본잠식률 = (자본금 - 자본총계) / 자본금 * 100. 자본금이 없거나 0이면 판단 불가.
            capital_impair_rt = (
                round((issued_capital - equity) / issued_capital * 100, 1)
                if issued_capital else None
            )
            is_capital_impair_50 = capital_impair_rt is not None and capital_impair_rt >= 50
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
            retained_earnings = fin.get("retained_earnings")
            roe = round(quarter_net_income * 4 / equity * 100, 1) if (quarter_net_income is not None and equity) else None

            # 지주회사 등 일부 업종은 표준 IFRS 매출/매출원가 계정을 쓰지 않아 데이터가 없을 수 있음
            gpa = (
                round((quarter_revenue - quarter_cost_of_sales) / assets * 100, 1)
                if (assets > 0 and quarter_revenue and quarter_cost_of_sales is not None)
                else None
            )

            # 20. 저평가 점수(10점 만점) - 종목이 진짜 저평가인지 위험한지 8개 재무 신호를
            # 점수화해서 판정한다(신호가 없으면 그 항목만 0점 처리 - 판정 자체는 항상 나온다).
            value_score = 0
            if quarter_operating_cf is not None and quarter_operating_cf > 0:
                value_score += 2
            if roe is not None and roe >= 5:
                value_score += 2
            if debt_ratio is not None and debt_ratio <= 100:
                value_score += 1
            if debt_ratio is not None and debt_ratio <= 30:
                value_score += 1
            if f_score is not None and f_score >= 2:
                value_score += 1
            if retained_earnings is not None and retained_earnings > 0:
                value_score += 1
            if (quarter_operating_income is not None and quarter_operating_income > 0
                    and fin.get("prev_operating_income") is not None and fin["prev_operating_income"] > 0):
                value_score += 1  # 영업이익 2분기 연속 흑자
            if quarter_net_income is not None and quarter_net_income > 0:
                value_score += 1  # 순이익 흑자
            if value_score >= 10:
                value_tier = "극저평가"
            elif value_score >= 8:
                value_tier = "저평가"
            elif value_score >= 2:
                value_tier = "판단보류"
            else:
                value_tier = "위험"
            ncav_total = current_assets - liabilities
            ncav = int(ncav_total / 100000000)  # 억 단위
            ncav_ratio = round(ncav_total / market_cap, 2) if market_cap > 0 else 0.0

            # 18. 유동비율 = 유동자산 / 유동부채
            current_ratio = round(current_assets / current_liabilities * 100, 1) if current_liabilities > 0 else None

            # 19. 배당율 = 최근 사업보고서 연간 배당총액 / 시가총액
            # 배당금지급은 현금흐름표 유출 항목이라 회사에 따라 음수로 공시되므로 절대값으로 정규화
            annual_dividends_paid = fin.get("annual_dividends_paid")
            dividend_yield = (
                round(abs(annual_dividends_paid) / market_cap * 100, 2)
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
                "op_to_debt_now": op_to_debt_now,
                "op_to_debt_yoy": op_to_debt_yoy,
                "asset_growth_yoy": asset_growth_yoy,
                "assets_yoy": assets_yoy,
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
                "is_halt": is_halt,
                "is_inv_warn": is_inv_warn,
                "is_margin100": is_margin100,
                "is_delisting": is_delisting,
                "is_capital_impair_50": is_capital_impair_50,
                "capital_impair_rt": capital_impair_rt,
                "trade_value": avg_trade_amt,
                "dividend_yield": dividend_yield,
                "value_score": value_score,
                "value_tier": value_tier,
            })

            if (i + 1) % 100 == 0:
                print(f"[DART] 진행 중... {i + 1}/{len(raw_stocks)}")
                # 수동 실행이 예상보다 오래 걸릴 때 order_server.py가 30분 단위로 진행상황을
                # 텔레그램으로 보고할 수 있도록, 진행률을 파일로 남겨둔다(프로세스/재시작과 무관).
                _write_progress(5, "DART 재무제표 조회", i + 1, len(raw_stocks))
                # 캐시를 끝에서 한 번만 저장하면 중간에 멈추거나 죽었을 때 이미 백필한 재무데이터가
                # 전부 유실돼 다음 실행에서 같은 구간을 처음부터 다시 반복하게 된다 - 100종목마다
                # 중간 저장해서 중단 시에도 그때까지의 백필 결과를 보존한다.
                save_json_cache(DART_FINANCIALS_CACHE_FILE, dart_financials_cache)
                save_json_cache(CAPITAL_INCREASE_CACHE_FILE, capital_increase_cache)
                save_json_cache(INDUTY_CODE_CACHE_FILE, induty_code_cache)
                save_json_cache(ADMIN_ISSUE_CACHE_FILE, admin_issue_cache)
                save_json_cache(TRADE_AMT_CACHE_FILE, trade_amt_cache)

        save_json_cache(DART_FINANCIALS_CACHE_FILE, dart_financials_cache)
        save_json_cache(CAPITAL_INCREASE_CACHE_FILE, capital_increase_cache)
        save_json_cache(INDUTY_CODE_CACHE_FILE, induty_code_cache)
        save_json_cache(ADMIN_ISSUE_CACHE_FILE, admin_issue_cache)
        save_json_cache(TRADE_AMT_CACHE_FILE, trade_amt_cache)
        print(f"[DART] 재무제표 보강 완료: {len(valid_stocks)}개 종목 확보 (캐시 적중 {cache_hits}건, 금융/지주/관리종목 표시 {flagged_count}건)")

        # 전체 종목(시가총액 기준) 중 상/하위 몇 %에 속하는지 - 자세히 모드에서 시가총액 아래 표기용
        total_count = len(valid_stocks)
        by_market_cap = sorted(valid_stocks, key=lambda x: x["market_cap"], reverse=True)
        for rank, s in enumerate(by_market_cap, start=1):
            s["market_cap_pct_from_top"] = round(rank / total_count * 100)

        # 11~14. 영업이익/순이익 QoQ·YoY 순위는 항상 전체 종목 기준으로 한 번만 계산해서 종목에 붙여둔다.
        # 소형주 등 옵션이 적용된 화면도 이 전체 순위를 그대로 보여줘야 하고(부분집합 재계산 금지),
        # 4개 중 하나라도 음수면 해당 지표 기반 전략에서 제외한다.
        MOMENTUM_KEYS = ("op_growth_qoq", "op_growth_yoy", "ni_growth_qoq", "ni_growth_yoy")
        MOMENTUM_RANK_KEYS = ("op_qoq_r", "op_yoy_r", "ni_qoq_r", "ni_yoy_r")
        momentum_pool = [
            s for s in valid_stocks
            if all(s.get(k) is not None and s[k] >= 0 for k in MOMENTUM_KEYS)
        ]
        for src_key, rank_key in zip(MOMENTUM_KEYS, MOMENTUM_RANK_KEYS):
            momentum_pool.sort(key=lambda x: x[src_key], reverse=True)
            for idx, s in enumerate(momentum_pool):
                s[rank_key] = idx + 1

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

    # 소형주(시가총액 200억원 이상 종목 중 하위 20%) 한정 슈퍼 가치 랭킹 - 200억 미만은 관리종목
    # 지정 기준(2026.7.1부터 시행)에 걸리는 상장폐지 위험군이라, 이걸 그대로 포함해 하위 20%를
    # 잡으면 "저평가 소형주"가 아니라 대부분 상장폐지 위험 종목으로 채워지는 문제가 있었다.
    SMALLCAP_MIN_MARKET_CAP = 20_000_000_000  # 200억원
    market_caps = sorted(s["market_cap"] for s in valid_stocks if s["market_cap"] >= SMALLCAP_MIN_MARKET_CAP)
    smallcap_cutoff = market_caps[max(int(len(market_caps) * 0.2) - 1, 0)] if market_caps else 0
    super_base_smallcap = rank_super_value([s for s in valid_stocks if SMALLCAP_MIN_MARKET_CAP <= s["market_cap"] <= smallcap_cutoff])

    # 11. 이익 모멘텀 전략 연산부 - 4개 지표 순위는 전체 종목 기준으로 이미 계산되어 있음(위 MOMENTUM_RANK_KEYS)
    def rank_momentum(pool):
        # dict()로 복사 - 전체/소형주 풀 간 종목 객체 공유로 순위가 서로 덮어써지는 문제 방지
        base = [dict(s) for s in pool if s.get("op_qoq_r") is not None]
        for s in base: s["momentum_score"] = s["op_qoq_r"] + s["op_yoy_r"] + s["ni_qoq_r"] + s["ni_yoy_r"]
        base.sort(key=lambda x: x["momentum_score"])
        return base

    momentum_base = rank_momentum(valid_stocks)
    momentum_base_smallcap = rank_momentum([s for s in valid_stocks if SMALLCAP_MIN_MARKET_CAP <= s["market_cap"] <= smallcap_cutoff])

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
        and s["asset_growth_yoy"] is not None and s["asset_growth_yoy"] > -20
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
        if SMALLCAP_MIN_MARKET_CAP <= s["market_cap"] <= smallcap_cutoff
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
        s["fama_score"] = s["pbr_r"] * 0.5 + s["gpa_r"] * 0.25 + s["asset_growth_r"] * 0.25  # PBR 50%, GP/A 25%, 자산성장률 25% 가중평균
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
        and s.get("op_qoq_r") is not None  # 11~14 지표는 전체 종목 기준 순위(음수 제외 적용됨)를 그대로 사용
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
        and s.get("op_qoq_r") is not None  # 11~14 지표는 전체 종목 기준 순위(음수 제외 적용됨)를 그대로 사용
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
            and s.get("op_qoq_r") is not None  # 11~14 지표는 전체 종목 기준 순위가 이미 계산되어 있어야 함(음수 제외 포함)
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
        # op_qoq_r/op_yoy_r/ni_qoq_r/ni_yoy_r는 전체 종목 기준으로 이미 계산된 값을 그대로 사용(재계산 금지)
        for s in base:
            s["ultra_score"] = (
                s["per_r"] + s["pfcr_r"] + s["pbr_r"] + s["psr_r"]
                + s["gpa_r"] + s["op_debt_r"] + s["asset_growth_r"] + s["volatility_r"]
                + s["op_qoq_r"] + s["op_yoy_r"] + s["ni_qoq_r"] + s["ni_yoy_r"]
            )
        base.sort(key=lambda x: x["ultra_score"])
        return base

    ultra_base = rank_ultra(valid_stocks)
    ultra_base_smallcap = rank_ultra([s for s in valid_stocks if SMALLCAP_MIN_MARKET_CAP <= s["market_cap"] <= smallcap_cutoff])

    # 12. NCAV 청산가치 & 퀄리티 스크리너 연산부
    # 조건: 1) 순유동자산 > 시가총액  2) 최신 분기 순이익 > 0  3) 차입금비율 200% 이하
    #       4) GP/A 전체 종목 상위 50% 이내 (항상 적용)  5) 상위 20개만 노출
    gpa_pool = sorted((s["gpa"] for s in valid_stocks if s["gpa"] is not None), reverse=True)
    gpa_cutoff = gpa_pool[len(gpa_pool) // 2 - 1] if gpa_pool else None

    def ncav_filter_base(pool):
        return [
            s for s in pool
            if s["ncav_ratio"] > 1
            and s["quarter_net_income"] is not None and s["quarter_net_income"] > 0
            and s["debt_ratio"] is not None and s["debt_ratio"] <= 200
            and s["gpa"] is not None and gpa_cutoff is not None and s["gpa"] >= gpa_cutoff
            and not s["is_fin_holding"] and not s["is_admin_issue"]
        ]

    ncav_base = ncav_filter_base(valid_stocks)
    ncav_base.sort(key=lambda x: x["ncav_ratio"], reverse=True)

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
        for idx, s in enumerate(base[:100]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "code": s["code"],
                "price": s["price"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
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

    # 슈퍼 가치 상위 100개 패키징 (전체 / 소형주 한정)
    super_value = package_super_value(super_base)
    super_value_smallcap = package_super_value(super_base_smallcap)

    def package_momentum(base):
        packaged = []
        for idx, s in enumerate(base[:100]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "code": s["code"],
                "price": s["price"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
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

    # 이익 모멘텀 상위 100개 패키징 (전체 / 소형주 한정)
    momentum_value = package_momentum(momentum_base)
    momentum_value_smallcap = package_momentum(momentum_base_smallcap)

    # 신 F-스코어+저PBR 상위 100개 패키징
    fscore_value = []
    for idx, s in enumerate(fscore_base[:100]):
        fscore_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "code": s["code"],
            "price": s["price"],
            "cap_increase_flag": s["cap_increase_flag"],
            "ni_pos_flag": s["ni_pos_flag"],
            "cf_pos_flag": s["cf_pos_flag"],
            "quarter_net_income": int(s["quarter_net_income"] / 100000000) if s["quarter_net_income"] is not None else None,  # 억 단위
            "quarter_operating_cf": int(s["quarter_operating_cf"] / 100000000) if s["quarter_operating_cf"] is not None else None,  # 억 단위
            "f_score": s["f_score"],
            "pbr": s["pbr"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
            "equity": int(s["equity"] / 100000000),  # 억 단위
        })

    # 15. 슈퍼 퀄리티 상위 100개 패키징
    quality_value = []
    for idx, s in enumerate(quality_base[:100]):
        quality_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "code": s["code"],
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
            "quarter_revenue": to_eok(s["quarter_revenue"]),
            "quarter_cost_of_sales": to_eok(s["quarter_cost_of_sales"]),
            "assets": to_eok(s["assets"]),
            "assets_yoy": to_eok(s["assets_yoy"]),
            "op_to_debt_now": s["op_to_debt_now"],
            "op_to_debt_yoy": s["op_to_debt_yoy"],
        })

    # 16. 파마의 최종 병기 상위 100개 패키징
    fama_value = []
    for idx, s in enumerate(fama_base[:100]):
        fama_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "code": s["code"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
            "pbr": s["pbr"],
            "pbr_r": s["pbr_r"],
            "gpa": s["gpa"],
            "gpa_r": s["gpa_r"],
            "asset_growth_yoy": s["asset_growth_yoy"],
            "asset_growth_r": s["asset_growth_r"],
            "avg_r": round(s["fama_score"], 1),
            "equity": to_eok(s["equity"]),
            "quarter_revenue": to_eok(s["quarter_revenue"]),
            "quarter_cost_of_sales": to_eok(s["quarter_cost_of_sales"]),
            "assets": to_eok(s["assets"]),
            "assets_yoy": to_eok(s.get("assets_yoy")),
        })

    # 18. 슈퍼 가치+퀄리티 상위 100개 패키징
    super_quality_value = []
    for idx, s in enumerate(super_quality_base[:100]):
        super_quality_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "code": s["code"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
            "per": s["per"], "per_r": s["per_r"],
            "pcr": s["pcr"], "pcr_r": s["pcr_r"],
            "pbr": s["pbr"], "pbr_r": s["pbr_r"],
            "psr": s["psr"], "psr_r": s["psr_r"],
            "gpa": s["gpa"], "gpa_r": s["gpa_r"],
            "op_debt_growth_yoy": s["op_debt_growth_yoy"], "op_debt_r": s["op_debt_r"],
            "asset_growth_yoy": s["asset_growth_yoy"], "asset_growth_r": s["asset_growth_r"],
            "price_volatility": s["price_volatility"], "volatility_r": s["volatility_r"],
            "avg_r": round(s["super_quality_score"] / 8, 1),
            "equity": to_eok(s["equity"]),
            "quarter_operating_cf": to_eok(s["quarter_operating_cf"]),
            "quarter_net_income": to_eok(s["quarter_net_income"]),
            "quarter_revenue": to_eok(s["quarter_revenue"]),
            "quarter_cost_of_sales": to_eok(s["quarter_cost_of_sales"]),
            "assets": to_eok(s["assets"]),
            "assets_yoy": to_eok(s["assets_yoy"]),
            "op_to_debt_now": s["op_to_debt_now"],
            "op_to_debt_yoy": s["op_to_debt_yoy"],
        })

    # 20. 밸류+모멘텀 상위 100개 패키징
    value_momentum_value = []
    for idx, s in enumerate(value_momentum_base[:100]):
        value_momentum_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "code": s["code"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
            "per": s["per"], "per_r": s["per_r"],
            "pfcr": s["pfcr"], "pfcr_r": s["pfcr_r"],
            "pbr": s["pbr"], "pbr_r": s["pbr_r"],
            "psr": s["psr"], "psr_r": s["psr_r"],
            "op_growth_qoq": s["op_growth_qoq"], "op_qoq_r": s["op_qoq_r"],
            "op_growth_yoy": s["op_growth_yoy"], "op_yoy_r": s["op_yoy_r"],
            "ni_growth_qoq": s["ni_growth_qoq"], "ni_qoq_r": s["ni_qoq_r"],
            "ni_growth_yoy": s["ni_growth_yoy"], "ni_yoy_r": s["ni_yoy_r"],
            "avg_r": round(s["value_momentum_score"] / 8, 1),
            "equity": to_eok(s["equity"]),
            "quarter_operating_cf": to_eok(s["quarter_operating_cf"]),
            "quarter_capex": to_eok(s["quarter_capex"]),
            "quarter_net_income": to_eok(s["quarter_net_income"]),
            "quarter_revenue": to_eok(s["quarter_revenue"]),
            "quarter_operating_income": to_eok(s["quarter_operating_income"]),
            "prev_operating_income": to_eok(s["prev_operating_income"]),
            "operating_income_yoy": to_eok(s["operating_income_yoy"]),
            "prev_net_income": to_eok(s["prev_net_income"]),
            "net_income_yoy": to_eok(s["net_income_yoy"]),
        })

    # 21. 슈퍼 퀄리티+모멘텀 상위 100개 패키징
    quality_momentum_value = []
    for idx, s in enumerate(quality_momentum_base[:100]):
        quality_momentum_value.append({
            "rank": idx + 1,
            "name": s["name"],
            "code": s["code"],
            "price": s["price"],
            "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
            "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
            "gpa": s["gpa"], "gpa_r": s["gpa_r"],
            "op_debt_growth_yoy": s["op_debt_growth_yoy"], "op_debt_r": s["op_debt_r"],
            "asset_growth_yoy": s["asset_growth_yoy"], "asset_growth_r": s["asset_growth_r"],
            "price_volatility": s["price_volatility"], "volatility_r": s["volatility_r"],
            "op_growth_qoq": s["op_growth_qoq"], "op_qoq_r": s["op_qoq_r"],
            "op_growth_yoy": s["op_growth_yoy"], "op_yoy_r": s["op_yoy_r"],
            "ni_growth_qoq": s["ni_growth_qoq"], "ni_qoq_r": s["ni_qoq_r"],
            "ni_growth_yoy": s["ni_growth_yoy"], "ni_yoy_r": s["ni_yoy_r"],
            "avg_r": round(s["quality_momentum_score"] / 8, 1),
            "quarter_revenue": to_eok(s["quarter_revenue"]),
            "quarter_cost_of_sales": to_eok(s["quarter_cost_of_sales"]),
            "assets": to_eok(s["assets"]),
            "assets_yoy": to_eok(s["assets_yoy"]),
            "op_to_debt_now": s["op_to_debt_now"],
            "op_to_debt_yoy": s["op_to_debt_yoy"],
            "quarter_operating_income": to_eok(s["quarter_operating_income"]),
            "prev_operating_income": to_eok(s["prev_operating_income"]),
            "operating_income_yoy": to_eok(s["operating_income_yoy"]),
            "quarter_net_income": to_eok(s["quarter_net_income"]),
            "prev_net_income": to_eok(s["prev_net_income"]),
            "net_income_yoy": to_eok(s["net_income_yoy"]),
        })

    # 22. 울트라 상위 100개 패키징 (전체 / 소형주 한정)
    def package_ultra(base):
        packaged = []
        for idx, s in enumerate(base[:100]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "code": s["code"],
                "price": s["price"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
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
                "equity": to_eok(s["equity"]),
                "quarter_operating_cf": to_eok(s["quarter_operating_cf"]),
                "quarter_capex": to_eok(s["quarter_capex"]),
                "quarter_net_income": to_eok(s["quarter_net_income"]),
                "quarter_revenue": to_eok(s["quarter_revenue"]),
                "quarter_cost_of_sales": to_eok(s["quarter_cost_of_sales"]),
                "assets": to_eok(s["assets"]),
                "assets_yoy": to_eok(s["assets_yoy"]),
                "op_to_debt_now": s["op_to_debt_now"],
                "op_to_debt_yoy": s["op_to_debt_yoy"],
                "quarter_operating_income": to_eok(s["quarter_operating_income"]),
                "prev_operating_income": to_eok(s["prev_operating_income"]),
                "operating_income_yoy": to_eok(s["operating_income_yoy"]),
                "prev_net_income": to_eok(s["prev_net_income"]),
                "net_income_yoy": to_eok(s["net_income_yoy"]),
            })
        return packaged

    ultra_value = package_ultra(ultra_base)
    ultra_value_smallcap = package_ultra(ultra_base_smallcap)

    # NCAV 상위 100개 패키징 (GP/A 필터 미적용 / 적용)
    def package_ncav_value(base):
        packaged = []
        for idx, s in enumerate(base[:100]):
            packaged.append({
                "rank": idx + 1,
                "name": s["name"],
                "code": s["code"],
                "price": s["price"],
                "ncav": s["ncav"],
                "ncav_ratio": s["ncav_ratio"],
                "market_cap": int(s["market_cap"] / 100000000),  # 억 단위
                "market_cap_pct_from_top": s.get("market_cap_pct_from_top"),
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

    # 금융회사/지주회사/관리종목 배지 표기용 (추천 메뉴에서 사용, 12.NCAV 외에는 제외하지 않고 표기만)
    stock_flags = {}
    for s in valid_stocks:
        tags = []
        if s["is_fin_holding"]:
            tags.append("금융/지주")
        if s["is_admin_issue"]:
            tags.append("관리")
            # DART 공시 제목만으로는 관리종목 지정 사유(동전주/자본잠식 등)를 알 수 없어 단정할 순
            # 없지만, 이미 관리종목인데 주가까지 1,000원 미만이면 동전주 관련 정황으로 참고 표시.
            # (2026.7.1부터 코스닥·코스피 공통 주가 1,000원 미만 30거래일 연속 시 관리종목 지정)
            if 0 < (s.get("price") or 0) < 1000:
                tags.append("동전주")
        if s["is_admin_warning"]:
            tags.append("관리우려")
        if s.get("is_halt"):
            tags.append("거래정지")
        if s.get("is_inv_warn"):
            tags.append("투자경고")
        if s.get("is_delisting"):
            tags.append("정리매매(상장폐지)")
        if s.get("is_margin100"):
            tags.append("증거금100%")
        if s.get("is_capital_impair_50"):
            tags.append("자본잠식50%↑")
        if (s.get("trade_value") or 0) < 20_000_000:
            tags.append("2천만↓")
        # 코스닥 시가총액 관리종목 기준 - 2026.2 상장폐지 개혁방안으로 일정이 앞당겨져
        # 2026.7.1부터 200억원, 2027.1.1부터 300억원으로 강화된다(기존 40억원보다 훨씬 높음).
        # 30거래일 연속 미달 시 관리종목 지정 요건이라 두 뱃지 다 참고 컬럼 폭 제약상 200억이 더
        # 급한 기준이므로 둘 다 해당하면 200억만 표시한다.
        market_cap = s.get("market_cap") or 0
        if market_cap < 20_000_000_000:
            tags.append("200억↓")
        elif market_cap < 30_000_000_000:
            tags.append("300억↓")
        if tags:
            stock_flags[s["name"]] = tags

    # 추천 메뉴의 "PBR/GP-A" 참고 컬럼용 - 전략 소속과 무관하게 종목 자체의 값을 표시
    stock_metrics = {
        s["name"]: {
            "pbr": s["pbr"],
            "gpa": s["gpa"],
            "f_score": s["f_score"],
            "asset_growth_yoy": s["asset_growth_yoy"],
            "value_score": s.get("value_score"),
            "value_tier": s.get("value_tier"),
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
    }
    
    # data.js 전용 스크립트로 굽기
    js_content = f"const KOSPI_QUANT_PACKAGE = {json.dumps(package, ensure_ascii=False, indent=4)};\n"
    
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(js_content)
        
    print("▶ 오리지널 웹앱 전용 KOSPI_QUANT_PACKAGE 패키징 완료.")

    # 텔레그램 아침 요약 알림용 - 시도 내역과 "지표 자체가 실제로 채워졌는지"를 기록해둔다
    # (order_server.py가 07:00에 읽어서 전송). 전략별 종목 수(상위 N개 순위표 크기)는 밑바탕
    # 지표가 깨져도 "어쨌든 몇 개는 채워짐"으로 정상처럼 보일 수 있어서 신뢰도가 낮다 —
    # 실제로 공공데이터/DART에서 계산되는 19개 원천 지표 각각의 커버리지(전체 종목 중 값이
    # 있는 종목 수)를 직접 세는 게 이상 감지에 더 정확하다(2026-07-14/16 이익잉여금 필드
    # 누락 사고 참고).
    # (지표명, valid_stocks 키, "0.0도 계산불가로 볼지" 여부)
    COVERAGE_INDICATORS = [
        ("PER", "per", True), ("PBR", "pbr", True), ("PSR", "psr", True),
        ("PCR", "pcr", True), ("PFCR", "pfcr", True),
        ("F-스코어", "f_score", False), ("GP/A", "gpa", False),
        ("차입금비율", "debt_ratio", False), ("유동비율", "current_ratio", False),
        ("배당율", "dividend_yield", False), ("NCAV비율", "ncav_ratio", True),
        ("자산성장률", "asset_growth_yoy", False),
        ("영업이익QoQ", "op_growth_qoq", False), ("영업이익YoY", "op_growth_yoy", False),
        ("순이익QoQ", "ni_growth_qoq", False), ("순이익YoY", "ni_growth_yoy", False),
        ("영업이익/차입금YoY", "op_debt_growth_yoy", False),
        ("자본잠식률", "capital_impair_rt", False),
        ("베타", "price_volatility", False),
        ("시가총액", "market_cap", True), ("거래대금", "trade_value", True),
        ("저평가점수", "value_tier", False),
    ]
    total_valid = len(valid_stocks)
    indicator_coverage = {}
    for label, key, zero_missing in COVERAGE_INDICATORS:
        if zero_missing:
            cnt = sum(1 for s in valid_stocks if s.get(key) not in (None, 0.0))
        else:
            cnt = sum(1 for s in valid_stocks if s.get(key) is not None)
        indicator_coverage[label] = {"count": cnt, "total": total_valid}
    # 정상 상황과 크게 어긋나는(=사실상 계산이 다 깨진) 지표만 경고로 남긴다.
    low_indicator_warnings = [
        f"{label} {c['count']}/{c['total']}건({(c['count']/c['total']*100) if c['total'] else 0:.0f}%, 비정상적으로 적음)"
        for label, c in indicator_coverage.items() if total_valid > 0 and c["count"] / total_valid < 0.05
    ]

    # 업종 컬럼의 저평가 점수 배지는 극저평가/저평가/판단보류/위험 넷 중 하나로 표시되는데,
    # PBR과 무관하게 전 종목 대상이라, 분포가 한쪽으로 쏠리면(예: 전부 위험) 점수식 임계값을
    # 다시 봐야 한다는 신호다.
    pbr_judgment_breakdown = {
        "total": len(valid_stocks),
        "극저평가": sum(1 for s in valid_stocks if s.get("value_tier") == "극저평가"),
        "저평가": sum(1 for s in valid_stocks if s.get("value_tier") == "저평가"),
        "판단보류": sum(1 for s in valid_stocks if s.get("value_tier") == "판단보류"),
        "위험": sum(1 for s in valid_stocks if s.get("value_tier") == "위험"),
    }
    summary = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "krx_basis_date": krx_basis_date or "N/A",
        "dart_basis": dart_basis,
        "kospi_count": len(kospi_items),
        "kosdaq_count": len(kosdaq_items),
        "land_count": len(items),
        "ka10099_count": ka10099_count,
        "ka10032_count": ka10032_count,
        "dart_attempted": len(raw_stocks),
        "dart_valid": len(valid_stocks),
        "dart_cache_hits": cache_hits,
        "flagged_count": flagged_count,
        "indicator_coverage": indicator_coverage,
        "pbr_judgment_breakdown": pbr_judgment_breakdown,
        "low_indicator_warnings": low_indicator_warnings,
        "warnings": update_warnings,
        "deploy_success": None,  # deploy_to_github() 실행 후 __main__에서 갱신
    }
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

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
    return res == 0


def _update_summary_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_summary.json")


def _write_crash_summary(reason: str):
    """fetch_krx_market_data()가 조기 실패하거나 예외로 죽었을 때도 07:00 요약이 "실행 자체가 실패함"을
    알 수 있도록 최소 정보만 남긴다."""
    summary = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "crashed": True,
        "reason": reason,
    }
    with open(_update_summary_path(), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    skip_deploy = "--no-deploy" in sys.argv
    try:
        ok = fetch_krx_market_data()
    except Exception as e:
        _write_crash_summary(str(e))
        raise
    if ok:
        if not skip_deploy:
            deploy_success = deploy_to_github()
            try:
                with open(_update_summary_path(), encoding="utf-8") as f:
                    summary = json.load(f)
                summary["deploy_success"] = deploy_success
                with open(_update_summary_path(), "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
    else:
        _write_crash_summary("fetch_krx_market_data()가 False를 반환 (영업일 데이터 로딩 실패)")