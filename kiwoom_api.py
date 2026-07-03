"""키움증권 REST API 연동 - 1단계: 보유 내역(계좌평가잔고내역) 조회.

사용법:
    1. config_local.py에 KIWOOM_APP_KEY / KIWOOM_APP_SECRET / KIWOOM_ACCOUNT_NO 입력
    2. KIWOOM_IS_MOCK = True (모의투자)로 먼저 테스트
    3. python kiwoom_api.py 실행

참고: 키움 REST API는 정식 문서가 동적 페이지라 직접 크롤링이 어려워, 공개된
커뮤니티 라이브러리(kiwoom-rest-api)의 실제 구현을 참고해 엔드포인트/헤더 형식을
확인했습니다. kt00018 요청 바디의 정확한 필드명은 발급받은 키로 실제 호출해보면서
오류 메시지를 참고해 조정이 필요할 수 있습니다.
"""

import json
import sys
from datetime import datetime

import requests

try:
    from config_local import (
        KIWOOM_APP_KEY,
        KIWOOM_APP_SECRET,
        KIWOOM_ACCOUNT_NO,
        KIWOOM_IS_MOCK,
    )
except ImportError:
    KIWOOM_APP_KEY = ""
    KIWOOM_APP_SECRET = ""
    KIWOOM_ACCOUNT_NO = ""
    KIWOOM_IS_MOCK = True

PROD_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"


def get_base_url(is_mock=True):
    return MOCK_URL if is_mock else PROD_URL


def issue_access_token(app_key, app_secret, is_mock=True):
    """접근토큰발급 (OAuth2). 성공 시 토큰 문자열을 반환."""
    url = f"{get_base_url(is_mock)}/oauth2/token"
    resp = requests.post(
        url,
        json={
            "grant_type": "client_credentials",
            "appkey": app_key,
            "secretkey": app_secret,
        },
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RuntimeError(f"토큰 발급 실패: {data}")
    return token


def fetch_account_balance(access_token, account_no, is_mock=True):
    """계좌평가잔고내역요청 (kt00018) - 보유 종목 내역 조회."""
    url = f"{get_base_url(is_mock)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00018",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    body = {
        "acnt_no": account_no,
        "qry_tp": "1",  # 조회구분
        "dmst_stex_tp": "KRX",  # 국내거래소구분
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_holdings():
    """보유 내역을 조회해서 반환 (종목코드/종목명/수량/평가금액 등 원본 응답 그대로)."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    return fetch_account_balance(token, KIWOOM_ACCOUNT_NO, KIWOOM_IS_MOCK)


def get_realized_pl(stk_cd, strt_dt=""):
    """일자별종목별실현손익요청_일자 (ka10072) - 특정 종목의 실현손익·실제 수수료·세금 조회.

    stk_cd: 종목코드 6자리
    strt_dt: 시작일자 YYYYMMDD (빈값이면 당일)
    반환: tdy_sel_pl(당일매도손익), pl_rt(손익율), tdy_trde_cmsn(수수료), tdy_trde_tax(세금)
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    from datetime import datetime as _dt
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10072",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "stk_cd": stk_cd,
        "strt_dt": strt_dt or _dt.now().strftime("%Y%m%d"),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_daily_balance(qry_dt=""):
    """일별잔고수익률 (ka01690) - 특정 날짜의 포트폴리오 평가금액·수익률·종목별 잔고 조회.

    qry_dt: YYYYMMDD. 빈값이면 당일.
    tot_evlt_amt: 브로커 계산 총 평가금액 (직접 계산보다 정확)
    dbst_bal: 예수금
    day_bal_rt: 종목별 현재가·평가손익·수익률
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka01690",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    from datetime import datetime as _dt
    body = {"qry_dt": qry_dt or _dt.now().strftime("%Y%m%d")}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_settlement_balance():
    """체결잔고요청 (kt00005) - 주문가능현금(ord_alowa)과 종목별 결제잔고(setl_remn) 제공.

    ord_alowa: 실제 매수 주문에 사용 가능한 현금 (미결제·수수료 등 반영된 정확한 값)
    setl_remn: 종목별 결제 완료된 잔고 (미체결 매수 주문 미포함 → 실제 체결 수량)
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00005",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {"dmst_stex_tp": "KRX"}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_period_eval(fr_dt, to_dt):
    """계좌기간별수익률현황요청 (kt00016) - 기간 내 총입금·총출금·평가손익·수익률 조회.

    fr_dt, to_dt: YYYYMMDD
    termin_tot_trns: 기간내총입금, termin_tot_pymn: 기간내총출금
    evltv_prft: 평가손익 (입출금 반영), prft_rt: 수익률
    invt_bsamt: 투자원금평잔
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00016",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {"fr_dt": fr_dt, "to_dt": to_dt}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_order_status(access_token, account_no, is_mock=True, qry_tp="1"):
    """계좌별주문체결현황요청 (kt00009) - 당일 주문/체결 현황 조회.

    qry_tp: "0"-전체(미체결 포함), "1"-체결만.
    """
    url = f"{get_base_url(is_mock)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00009",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    body = {
        "acnt_no": account_no,
        "qry_tp": qry_tp,
        "stk_bond_tp": "0",
        "sell_tp": "0",
        "mrkt_tp": "0",
        "dmst_stex_tp": "KRX",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_order_status(qry_tp="1"):
    """당일 주문/체결 현황을 조회해서 반환. qry_tp="0"이면 미체결 주문도 포함."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    return fetch_order_status(token, KIWOOM_ACCOUNT_NO, KIWOOM_IS_MOCK, qry_tp)


def get_open_orders(sell_tp="0"):
    """계좌별주문체결내역상세요청 (kt00007) - qry_tp=3(미체결만) 으로 조회.

    ord_remnq(주문잔량) 직접 제공, mdfy_cncl(정정취소) 필드로 취소 여부 판별 가능.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt00007",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "ord_dt": "",           # 당일
        "qry_tp": "3",          # 미체결만
        "stk_bond_tp": "0",     # 전체
        "sell_tp": sell_tp,
        "stk_cd": "",           # 전종목
        "fr_ord_no": "",
        "dmst_stex_tp": "%",    # 전체 거래소
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_fills(sell_tp="0"):
    """체결요청 (ka10076) - oso_qty(미체결수량)/ord_stt(주문상태) 포함, 미체결 판별에 더 정확.

    sell_tp: "0"-전체, "1"-매도, "2"-매수.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10076",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "stk_cd": "",
        "qry_tp": "0",    # 0:전체 종목
        "sell_tp": sell_tp,
        "ord_no": "",
        "stex_tp": "0",   # 0:통합
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def check_stock_warning(stk_cds):
    """종목정보 조회 (ka10100) - 투자유의종목 여부 확인.

    orderWarning: 0=정상, 2=정리매매, 3=단기과열, 4=투자위험, 5=투자경고
    매수 주문 전 호출해서 위험 종목을 사전 차단한다.
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET을 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)
    url = f"{get_base_url(KIWOOM_IS_MOCK)}/api/dostk/stkinfo"
    warnings = {}
    for code in stk_cds:
        try:
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "ka10100",
                "cont-yn": "N",
                "next-key": "",
                "authorization": f"Bearer {token}",
            }
            resp = requests.post(url, headers=headers, json={"stk_cd": code}, timeout=15)
            data = resp.json()
            warnings[code] = {
                "orderWarning": data.get("orderWarning", "0"),
                "state": data.get("state", ""),
                "name": data.get("name", ""),
            }
        except Exception:
            warnings[code] = {"orderWarning": "0", "state": "", "name": ""}
    return warnings


def fetch_stock_quote(access_token, stk_cd, is_mock=True):
    """종목기본정보요청 (ka10001) - 현재가 등 기본 시세 조회 (성과추적용 가격 스냅샷에 사용)."""
    url = f"{get_base_url(is_mock)}/api/dostk/stkinfo"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "ka10001",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    body = {"stk_cd": stk_cd}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_quote(access_token, stk_cd, api_id, is_mock=True):
    """호가/시세 조회 공통 함수 (/api/dostk/mrkcond 계열)."""
    url = f"{get_base_url(is_mock)}/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": api_id,
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {access_token}",
    }
    resp = requests.post(url, headers=headers, json={"stk_cd": stk_cd}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_stock_quotes(stk_cds):
    """여러 종목코드의 현재가를 조회해서 {코드: 가격} 형태로 반환.

    시간대별 최적 API 자동 선택:
    - 09:00~15:30 정규장: ka10004(주식호가) sel_fpr_bid(매도최우선호가) — 최유리지정가 매수 실제 체결가
    - 16:00~18:00 시간외단일가: ka10087(시간외단일가) ovt_sigpric_cur_prc
    - 그 외: ka10001(종목기본정보) cur_prc
    """
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET을 먼저 입력하세요.")
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_IS_MOCK)

    t = datetime.now().time()
    t_0900 = datetime.strptime("09:00", "%H:%M").time()
    t_1530 = datetime.strptime("15:30", "%H:%M").time()
    t_1600 = datetime.strptime("16:00", "%H:%M").time()
    t_1800 = datetime.strptime("18:00", "%H:%M").time()

    prices = {}
    for code in stk_cds:
        try:
            # ka10001 단일 호출로 현재가·상한가·하한가·예상체결가 한 번에 조회
            # 시간외단일가(16:00~18:00)에는 ka10087로 정확한 시간외 현재가 추가 조회
            data = fetch_stock_quote(token, code, KIWOOM_IS_MOCK)
            price_raw = data.get("cur_prc") or data.get("stck_prpr")
            p = abs(int(price_raw)) if price_raw else None

            if t_1600 <= t < t_1800:
                try:
                    d2 = fetch_quote(token, code, "ka10087", KIWOOM_IS_MOCK)
                    ovt = d2.get("ovt_sigpric_cur_prc")
                    if ovt:
                        p = abs(int(ovt))
                except Exception:
                    pass  # 실패 시 ka10001 cur_prc 유지

            upl = data.get("upl_pric")
            lst = data.get("lst_pric")
            exp = data.get("exp_cntr_pric")
            prices[code] = {
                "price": p,
                "upper_limit": abs(int(upl)) if upl else None,
                "lower_limit": abs(int(lst)) if lst else None,
                "exp_price": abs(int(exp)) if exp else None,
            } if p else None
        except Exception:
            prices[code] = None
    return prices


def ensure_market_hours(now=None):
    """09:00~18:00(정규장+시간외) 범위가 아니면 명확한 에러로 막는다.

    주문/취소/정정 모두 이 시간 밖에서는 키움 거래소 자체가 마감 상태라 거부되는데,
    그 경우 "취소 실패" 같은 모호한 메시지 대신 장마감 때문임을 바로 알 수 있게 한다.
    """
    now = now or datetime.now()
    t = now.time()
    if t < datetime.strptime("09:00", "%H:%M").time() or t >= datetime.strptime("18:00", "%H:%M").time():
        raise RuntimeError(f"장마감 시간({t.strftime('%H:%M')})이라 지금은 처리할 수 없습니다. 09:00~18:00 사이에 다시 시도해주세요.")
    return t


def resolve_trde_tp(price, now=None):
    """현재 시각(장중/시간외)에 맞는 거래구분(trde_tp) 코드를 정한다.

    정규장(09:00~15:30)은 최유리지정가(상대 최우선호가에 즉시 체결 시도, 시장가보다 슬리피지가
    제한적이면서 고정 지정가보다 체결 확률이 높음)를 기본으로 쓴다.
    15:30~16:00 장마감후시간외(종가 거래), 16:00~18:00 시간외단일가(종가 ±10%)는 최유리지정가를
    지원하지 않아 고정 가격으로 주문한다. 그 외 시간은 예약주문을 지원하지 않으므로 주문을 막는다.
    """
    t = ensure_market_hours(now)
    if t < datetime.strptime("15:30", "%H:%M").time():
        return "06" if price else "03"  # 06: 최유리지정가, 03: 시장가 - 정규장
    if t < datetime.strptime("16:00", "%H:%M").time():
        if not price:
            raise RuntimeError("장마감후시간외(15:30~16:00)는 시장가 주문을 지원하지 않습니다. 종가로 주문해주세요.")
        return "81"  # 장마감후시간외 - 종가 거래
    if not price:
        raise RuntimeError("시간외단일가(16:00~18:00)는 시장가 주문을 지원하지 않습니다. 가격을 지정해주세요.")
    return "62"  # 시간외단일가 - 종가 ±10% 범위


def place_order(stk_cd, qty, side, price=None, is_mock=KIWOOM_IS_MOCK):
    """주식 매수/매도 주문 (kt10000/kt10001).

    side: "buy" 또는 "sell". 현재 시각에 따라 정규장/시간외 거래구분을 자동으로 선택한다.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다.")
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    trde_tp = resolve_trde_tp(price)
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, is_mock)
    url = f"{get_base_url(is_mock)}/api/dostk/ordr"
    api_id = "kt10000" if side == "buy" else "kt10001"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": api_id,
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    # 최유리지정가(06)/시장가(03)는 가격을 거래소가 정하므로 ord_uv를 비워야 한다.
    needs_price = trde_tp in ("00", "81", "62")
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": stk_cd,
        "ord_qty": str(int(qty)),
        "trde_tp": trde_tp,
        "ord_uv": str(int(price)) if (needs_price and price) else "",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def modify_order(stk_cd, orig_ord_no, mdfy_qty, mdfy_uv, is_mock=KIWOOM_IS_MOCK):
    """주식 정정주문 (kt10002) - 미체결 주문의 잔량/가격을 새 값으로 정정한다."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    ensure_market_hours()
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, is_mock)
    url = f"{get_base_url(is_mock)}/api/dostk/ordr"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt10002",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "mdfy_qty": str(int(mdfy_qty)),
        "mdfy_uv": str(int(mdfy_uv)),
        "mdfy_cond_uv": "",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def cancel_order(stk_cd, orig_ord_no, cncl_qty, is_mock=KIWOOM_IS_MOCK):
    """주식 취소주문 (kt10003) - 미체결 주문을 취소한다."""
    if not (KIWOOM_APP_KEY and KIWOOM_APP_SECRET and KIWOOM_ACCOUNT_NO):
        raise RuntimeError("config_local.py에 KIWOOM_APP_KEY/KIWOOM_APP_SECRET/KIWOOM_ACCOUNT_NO를 먼저 입력하세요.")
    ensure_market_hours()
    token = issue_access_token(KIWOOM_APP_KEY, KIWOOM_APP_SECRET, is_mock)
    url = f"{get_base_url(is_mock)}/api/dostk/ordr"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "api-id": "kt10003",
        "cont-yn": "N",
        "next-key": "",
        "authorization": f"Bearer {token}",
    }
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "cncl_qty": str(int(cncl_qty)),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"[시스템] 모의투자 서버 사용: {KIWOOM_IS_MOCK}")
    try:
        result = get_holdings()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"❌ 조회 실패: {e}")
