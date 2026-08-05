"""주문 실행 메뉴용 로컬 전용 서버.

키움 실거래 키는 이 서버(내 PC) 안에서만 사용되고 브라우저로는 절대 전달되지 않습니다.
배포된(GitHub Pages) index.html에서는 이 서버에 접근할 수 없으므로 주문 실행 메뉴는
이 서버를 직접 실행한 로컬 환경에서만 동작합니다.

사용법: python order_server.py  (5050 포트로 실행, index.html을 열어둔 채로 켜두면 됩니다)
"""

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime

import requests

from flask import Flask, jsonify, request
from flask_cors import CORS

import kiwoom_api
import update_data as update_data_module  # 이름 충돌 주의: 아래 /api/update_data 라우트 함수가 update_data라는 이름을 씀

app = Flask(__name__)
CORS(app)  # 로컬 전용 서버이므로 file:// 출처(브라우저)도 허용

try:
    from config_local import API_AUTH_TOKEN
except ImportError:
    API_AUTH_TOKEN = None


@app.before_request
def _require_api_token():
    if request.method == "OPTIONS":
        return  # CORS preflight는 인증 없이 통과
    if not API_AUTH_TOKEN:
        return  # 토큰 미설정 시 기존처럼 무인증 통과 (하위 호환)
    if request.headers.get("X-API-Token") != API_AUTH_TOKEN:
        return jsonify({"error": "인증 토큰이 없거나 올바르지 않습니다."}), 401

# 성과추적 데이터 (기준점/스냅샷) - 이 PC 안에만 저장, 깃허브에는 올리지 않음
PERFORMANCE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "performance_history.json")
PORTFOLIO_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_settings.json")
PENDING_ALERTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_alerts.json")
RECOMMEND_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recommend_settings.json")
MANUAL_HOLDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manual_holdings.json")
UPDATE_SUMMARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_summary.json")
# 수동 "가격 데이터 갱신" 완료 알림 감지용 - 파일 기반이라 order_server.py가 도중에 재시작돼도
# (update_data.py 자체는 별도 프로세스라 안 죽지만, 완료를 기다리던 스레드는 같이 죽어버려서
# 텔레그램이 영영 안 가는 문제가 있었다) 스케줄러 루프가 이 파일의 트리거 시각과
# update_summary.json 갱신 시각을 비교해서 완료를 알아낸다.
MANUAL_UPDATE_MARKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manual_update_pending.json")
UPDATE_PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_progress.json")
BENCHMARK_CODES = {"kospi": "069500", "kosdaq": "229200"}  # KODEX 200 / KODEX 코스닥150 (지수 추종 ETF로 근사)


def _send_telegram(text: str):
    """텔레그램 봇으로 메시지 전송. config_local.py에 토큰/채팅ID 없으면 조용히 무시."""
    token = getattr(kiwoom_api, "_cfg", None) and None  # 아래에서 직접 import
    try:
        from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass


DATA_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
ALERT_TOP_N = 50  # 이 순위 밖으로 밀리면 알림


def _load_strategy_ranks() -> dict:
    """data.js에서 전략별 종목 순위를 파싱. {종목명: {전략키: rank}} 반환."""
    import re
    if not os.path.exists(DATA_JS_PATH):
        return {}
    with open(DATA_JS_PATH, encoding="utf-8") as f:
        content = f.read()
    # 전략 블록 파싱: "key": [ {rank, name, ...}, ... ]
    strategy_pattern = re.compile(r'"([a-z_0-9]+)":\s*\[([^\]]*)\]', re.DOTALL)
    item_pattern = re.compile(r'"rank"\s*:\s*(\d+)[^}]*"name"\s*:\s*"([^"]+)"', re.DOTALL)
    result = {}
    for m in strategy_pattern.finditer(content):
        key, block = m.group(1), m.group(2)
        for item in item_pattern.finditer(block):
            rank, name = int(item.group(1)), item.group(2)
            result.setdefault(name, {})[key] = rank
    return result


# index.html computeRecommendData()의 보르다 카운트 로직 복제. 소형주 토글/표시개수/계절제외/
# 체크한 전략은 브라우저 localStorage에만 있던 값인데, /api/recommend_settings로 index.html이
# saveOptions() 시점마다 동기화해줘서(recommend_settings.json) 서버도 실제 화면과 동일한
# 기준으로 계산할 수 있다. 설정 파일이 아직 없으면(최초 실행 등) 기본값으로 폴백.
_RECOMMEND_DATA_KEYS = {
    "fscore": "fscore_value", "momentum": "momentum_value", "ncav": "ncav_value",
    "super": "super_value", "strategy15": "quality_value", "strategy16": "fama_value",
    "strategy18": "super_quality_value", "strategy19": "value_momentum_value",
    "strategy20": "quality_momentum_value", "strategy21": "ultra_value",
}
_RECOMMEND_NO_DISPLAY_LIMIT = {"ncav", "strategy16"}
# 소형주 토글이 있는 전략 3개: (설정 키, {모드: data.js 키})
_SMALLCAP_VARIANTS = {
    "super":      ("smallcapMode",         {"none": "super_value",    "20": "super_value_smallcap",    "30": "super_value_smallcap30"}),
    "momentum":   ("momentumSmallcapMode", {"none": "momentum_value", "20": "momentum_value_smallcap", "30": "momentum_value_smallcap30"}),
    "strategy21": ("ultraSmallcapMode",    {"none": "ultra_value",    "20": "ultra_value_smallcap",     "30": "ultra_value_smallcap30"}),
}
_DEFAULT_RECOMMEND_SETTINGS = {
    "smallcapMode": "none", "momentumSmallcapMode": "none", "ultraSmallcapMode": "none",
    "displayCountMode": "20", "isExcludeSeasonMode": False, "recommendCheckedKeys": [],
}


def _load_recommend_settings() -> dict:
    if os.path.exists(RECOMMEND_SETTINGS_FILE):
        try:
            with open(RECOMMEND_SETTINGS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            return {**_DEFAULT_RECOMMEND_SETTINGS, **loaded}
        except Exception:
            pass
    return dict(_DEFAULT_RECOMMEND_SETTINGS)


def _compute_recommend_top50() -> set:
    """추천 메뉴 종합순위(보르다 카운트, 사용자 실제 설정 기준) top50에 드는 종목명 집합을 반환."""
    if not os.path.exists(DATA_JS_PATH):
        return set()
    with open(DATA_JS_PATH, encoding="utf-8") as f:
        content = f.read()
    item_pattern = re.compile(r'"rank"\s*:\s*(\d+)[^}]*"name"\s*:\s*"([^"]+)"', re.DOTALL)

    settings = _load_recommend_settings()
    display_n = int(settings.get("displayCountMode") or 20)
    checked_keys = set(settings.get("recommendCheckedKeys") or [])
    exclude_season = bool(settings.get("isExcludeSeasonMode"))

    active_keys = []
    for strat_key in _RECOMMEND_DATA_KEYS:
        if exclude_season:
            if strat_key == "ncav":
                continue
            if strat_key == "super" and settings.get("smallcapMode") != "none":
                continue
        active_keys.append(strat_key)
    # 체크된 전략이 있으면 그것만, 없으면 전체 활성 전략 사용 (JS와 동일)
    checked_active = [k for k in active_keys if k in checked_keys]
    use_keys = checked_active if checked_active else active_keys

    entries = []
    for strat_key in use_keys:
        if strat_key in _SMALLCAP_VARIANTS:
            mode_setting_key, variants = _SMALLCAP_VARIANTS[strat_key]
            data_key = variants.get(settings.get(mode_setting_key), variants["none"])
        else:
            data_key = _RECOMMEND_DATA_KEYS[strat_key]
        m = re.search(r'"' + data_key + r'":\s*\[([^\]]*)\]', content, re.DOTALL)
        if not m:
            continue
        rank_map = {}
        for item in item_pattern.finditer(m.group(1)):
            rank, name = int(item.group(1)), item.group(2)
            if strat_key not in _RECOMMEND_NO_DISPLAY_LIMIT and rank > display_n:
                continue
            rank_map[name] = rank
        if rank_map:
            entries.append({"map": rank_map})

    if not entries:
        return set()
    uniform_penalty = max(len(e["map"]) for e in entries) + 1

    all_names = set()
    for e in entries:
        all_names.update(e["map"].keys())

    min_matched = 1 if checked_active else 2
    results = []
    for name in all_names:
        matched = [e for e in entries if name in e["map"]]
        if len(matched) < min_matched:
            continue
        borda_score = sum(e["map"].get(name, uniform_penalty) for e in entries) / len(entries)
        results.append((name, borda_score))

    results.sort(key=lambda x: x[1])
    if not results:
        return set()
    base = results[:50]
    cut_score = base[-1][1]
    return {name for name, score in results if score <= cut_score}


_holiday_cache = {}  # {"YYYYMMDD": bool} - 하루에 여러 번 물어봐도 API를 반복 호출하지 않도록 캐시


def _is_market_holiday(dt) -> bool:
    """공공데이터포털 특일 정보(getRestDeInfo)로 실제 휴장일(공휴일·임시공휴일)인지 확인.

    조회 실패 시 False(평소대로 진행)로 폴백한다 - 이 체크 때문에 정상 거래일에 긴급 매도
    알림 등이 안 가는 것보다는, 어쩌다 휴일에 알림이 한 번 더 가는 쪽이 훨씬 안전하다.
    """
    key = dt.strftime("%Y%m%d")
    if key in _holiday_cache:
        return _holiday_cache[key]
    try:
        from config_local import DATA_GO_KR_API_KEY
        import xml.etree.ElementTree as ET
        r = requests.get(
            "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo",
            params={
                "serviceKey": DATA_GO_KR_API_KEY,
                "solYear": dt.strftime("%Y"),
                "solMonth": dt.strftime("%m"),
                "numOfRows": "30",
                "pageNo": "1",
            },
            timeout=10,
        )
        root = ET.fromstring(r.content)
        is_holiday = any(
            item.findtext("locdate") == key and item.findtext("isHoliday") == "Y"
            for item in root.findall(".//item")
        )
    except Exception:
        return False
    _holiday_cache[key] = is_holiday
    return is_holiday


def _check_portfolio_ranks() -> str:
    """포트폴리오 종목의 전략별 순위를 체크해 텔레그램 메시지 생성.

    "기준점"(리밸런싱 메뉴에서 수동으로 찍는 스냅샷)은 그 이후 매매가 있어도 다시 찍기 전까진
    갱신이 안 돼서, 최근 매수 종목은 빠지고 이미 판 종목은 계속 체크되는 어긋남이 생긴다.
    _check_holding_conditions()와 동일하게 kiwoom_api.get_holdings()의 실시간 보유내역을 쓴다.
    """
    try:
        h = kiwoom_api.get_holdings()
        stocks = h.get("acnt_evlt_remn_indv_tot", [])
    except Exception:
        stocks = []
    portfolio = [s.get("stk_nm", "") for s in stocks if s.get("stk_nm")]
    if not portfolio:
        return "📋 보유 종목이 없습니다."

    # 추천 메뉴 종합순위(보르다 카운트) 기준 top50 — "매칭된 전략끼리만 평균내면 실제로는
    # 탈락했는데도 좋아 보이는" 문제가 있어 단순 평균 대신 이걸로 판정한다.
    top50 = _compute_recommend_top50()
    ranks = _load_strategy_ranks()

    lines_warn = []
    for name in portfolio:
        stock_ranks = ranks.get(name, {})
        matched = len(stock_ranks)
        if name not in top50:
            lines_warn.append(f"🔴 {name}: 종합순위 TOP50 밖 ({matched}개 전략에서만 순위권)")

    now_str = datetime.now().strftime("%m/%d %H:%M")
    header = f"📊 <b>포트폴리오 순위 점검</b> ({now_str})  {len(portfolio)}종목\n"
    if lines_warn:
        return header + "\n".join(lines_warn)
    return header + "✅ 전 종목 TOP50 이내 이상 없음"


_halt_disclosure_alerted: set = set()  # 이미 알림 보낸 "종목코드:접수번호" - 서버 재시작 전까진 중복 알림 안 함


def _check_upcoming_halts() -> str:
    """보유종목(실계좌 + 매도검토 워치리스트) 중 DART에 최근 올라온 거래정지 예고·병합·분할
    결정 공시를 찾아 텔레그램으로 미리 경고한다.

    예: 주식병합/분할 결정 후 몇 달 뒤 실제 거래정지가 시작되는데, 그 사이 DART에
    "주권매매거래정지" 공시가 실제 정지일보다 며칠~몇 주 먼저 뜬다. 지금까지는 이 공시를
    아무도 감시하지 않아서 정지 당일에야 알게 되는 문제가 있었다.
    """
    try:
        codes = set()
        names_by_code = {}
        try:
            h = kiwoom_api.get_holdings()
            for s in h.get("acnt_evlt_remn_indv_tot", []):
                code = (s.get("stk_cd") or "").lstrip("A")
                if code and s.get("stk_nm"):
                    codes.add(code)
                    names_by_code[code] = s["stk_nm"]
        except Exception:
            pass
        for m in _load_manual_holdings():
            if m.get("code"):
                codes.add(m["code"])

        if not codes:
            return ""

        from datetime import timedelta
        corp_map = update_data_module.get_corp_code_map()
        bgn_de = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
        end_de = datetime.now().strftime("%Y%m%d")
        KEYWORDS = ("거래정지", "병합결정", "분할결정")

        lines = []
        for code in codes:
            corp_code = corp_map.get(code)
            if not corp_code:
                continue
            try:
                r = update_data_module._safe_get(
                    "https://opendart.fss.or.kr/api/list.json",
                    params={
                        "crtfc_key": update_data_module.DART_API_KEY,
                        "corp_code": corp_code,
                        "bgn_de": bgn_de,
                        "end_de": end_de,
                        "pblntf_ty": "I",  # 거래소공시
                        "page_count": 100,
                    },
                    timeout=15,
                )
                data = r.json()
            except Exception:
                continue
            if data.get("status") != "000":
                continue
            for item in data.get("list", []):
                title = (item.get("report_nm") or "").strip()
                if "정정" in title or not any(kw in title for kw in KEYWORDS):
                    continue
                dedup_key = f"{code}:{item.get('rcept_no', '')}"
                if dedup_key in _halt_disclosure_alerted:
                    continue
                _halt_disclosure_alerted.add(dedup_key)
                name = names_by_code.get(code, code)
                lines.append(f"🚨 {name}: {title} ({item.get('rcept_dt', '')})")

        if lines:
            return "⚠️ <b>거래정지/병합·분할 공시 감지</b>\n" + "\n".join(lines)
        return ""
    except Exception:
        return ""


def _build_update_summary_telegram(label: str = "새벽 데이터 업데이트") -> str:
    """update_data.py 실행 결과(update_summary.json)를 요약해서 텔레그램으로 보고.

    새벽 3시 자동 배치(07:00 요약)와 수동 "가격 데이터 갱신" 완료 알림 양쪽에서 공용으로 쓰여서,
    호출부가 문맥에 맞는 label("새벽 데이터 업데이트"/"수동 데이터 갱신")을 넘겨 헤더에 반영한다.

    시도 내역(공공데이터/키움/DART) + 반영 결과(전략별 종목 수) + 경고를 한눈에 보여준다.
    전략 배열이 비정상적으로 비어버리는 사고(2026-07-13 DART 캐시 버그 등)를 다음날 아침에
    바로 알아차릴 수 있도록, 각 전략 종목 수와 저수치 경고를 반드시 포함한다.
    """
    if not os.path.exists(UPDATE_SUMMARY_FILE):
        return f"⚠️ <b>{label} 요약</b>\nupdate_summary.json이 없습니다 - 배치가 실행 안 됐거나 아직 완료 전일 수 있습니다."
    try:
        with open(UPDATE_SUMMARY_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception as e:
        return f"⚠️ <b>{label} 요약</b>\n결과 파일 읽기 실패: {e}"

    if s.get("crashed"):
        return (
            f"❌ <b>{label} 실패</b> ({s.get('checked_at', '-')})\n"
            f"사유: {s.get('reason', '알 수 없음')}"
        )

    lines = [f"🌅 <b>{label} 요약</b> ({s.get('checked_at', '-')})"]

    lines.append(
        f"\n📥 <b>시도 내역</b>\n"
        f"  공공데이터(KRX): 기준일 {s.get('krx_basis_date', 'N/A')}\n"
        f"    KOSPI {s.get('kospi_count', 0)}개 · KOSDAQ {s.get('kosdaq_count', 0)}개\n"
        f"  키움 ka10099(플래그): {s.get('ka10099_count', 0)}개\n"
        f"  키움 ka10032(거래대금): {s.get('ka10032_count', 0)}개\n"
        f"  DART 재무제표: {s.get('dart_attempted', 0)}개 시도 → {s.get('dart_valid', 0)}개 확보\n"
        f"    (캐시 적중 {s.get('dart_cache_hits', 0)}건, 기준 {s.get('dart_basis', 'N/A')})"
    )

    # 전략별 종목 수(상위 N개 순위표 크기)보다, 19개 원천 지표가 실제로 몇 종목에서
    # 채워졌는지가 데이터 이상 감지에 더 직접적이라 이걸로 대체 — 순위표는 밑바탕 지표가
    # 깨져도 "어쨌든 몇 개는 채워짐"으로 정상처럼 보일 수 있다.
    indicator_coverage = s.get("indicator_coverage") or {}
    if indicator_coverage:
        total = next(iter(indicator_coverage.values())).get("total", 0)
        parts = "\n".join(
            f"  {label}: {c['count'] / total * 100:.0f}%" if total else f"  {label}: -"
            for label, c in indicator_coverage.items()
        )
        lines.append(f"\n📊 <b>지표 커버리지</b> (전체 {total}종목 중 값 있는 종목 수)\n{parts}")

    # 업종 컬럼 저평가 점수 배지와 동일한 구분(극저평가/저평가/판단보류/위험)으로 전 종목 집계.
    pbr_bd = s.get("pbr_judgment_breakdown") or {}
    pbr_total = pbr_bd.get("total", 0)
    if pbr_total:
        pbr_parts = "\n".join(
            f"  {label}: {pbr_bd.get(label, 0)} ({pbr_bd.get(label, 0) / pbr_total * 100:.0f}%)"
            for label in ("극저평가", "저평가", "판단보류", "위험")
        )
        lines.append(f"\n📊 <b>저평가 점수 판정</b> (전체 {pbr_total}종목)\n{pbr_parts}")

    warnings = list(s.get("warnings") or [])
    warnings += s.get("low_indicator_warnings") or []
    if s.get("deploy_success") is False:
        warnings.append("깃허브 배포 실패 - 웹에 반영 안 됐을 수 있음")
    if warnings:
        lines.append("\n⚠️ <b>경고</b>\n  " + "\n  ".join(warnings))
    else:
        lines.append("\n✅ 경고 없음")

    return "\n".join(lines)


def _load_performance():
    if os.path.exists(PERFORMANCE_FILE):
        with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"baselines": []}


WEIGHT_DEVIATION_ALERT_PCT = 10  # 목표 대비 실제 비중 차이가 이 %p를 넘으면 알림
_weight_deviation_alerted: set = set()  # 이미 알림 보낸 종목 — 다시 좁혀지기 전엔 재알림 안 함


def _check_weight_deviation():
    """포트폴리오 목표비중(portfolio_settings.json) 대비 실제 보유비중이 크게 벌어지면 텔레그램 알림.

    브라우저를 꺼두면 못 보는 웹 알림(notify())과 별개로, 서버에서도 동일한 편차를 감지해
    텔레그램으로 보낸다. 다시 임계값 안으로 좁혀지면 알림 상태를 리셋해 재이탈 시 다시 알린다.
    """
    settings = _load_portfolio_settings()
    stocks = settings.get("stocks") or []
    if not stocks:
        return
    total_weight = sum(float(s.get("weight") or 0) for s in stocks)
    if total_weight <= 0:
        return
    try:
        holdings = kiwoom_api.get_holdings()
    except Exception:
        return
    value_by_code = {}
    for r in holdings.get("acnt_evlt_remn_indv_tot", []):
        code = (r.get("stk_cd") or "").lstrip("A")
        if not code:
            continue
        qty   = int(r.get("rmnd_qty") or 0)
        price = int(r.get("cur_prc") or r.get("cur_pric") or 0)
        value_by_code[code] = qty * price
    total_value = sum(value_by_code.values())
    if total_value <= 0:
        return

    for s in stocks:
        code = s.get("code")
        if not code:
            continue
        target_pct = float(s.get("weight") or 0) / total_weight * 100
        cur_pct    = value_by_code.get(code, 0) / total_value * 100
        diff       = cur_pct - target_pct
        exceeded   = abs(diff) >= WEIGHT_DEVIATION_ALERT_PCT
        if exceeded and code not in _weight_deviation_alerted:
            _weight_deviation_alerted.add(code)
            name = s.get("name", code)
            _send_telegram(
                f"⚖️ <b>목표비중 이탈</b>\n"
                f"종목: {name} ({code})\n"
                f"목표비중 {target_pct:.1f}% / 현재비중 {cur_pct:.1f}%"
                f" ({'+' if diff > 0 else ''}{diff:.1f}%p)"
            )
        elif not exceeded and code in _weight_deviation_alerted:
            _weight_deviation_alerted.discard(code)


def _load_portfolio_settings() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"stocks": [], "total_investment": 0}


def _save_portfolio_settings(data: dict):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_manual_holdings() -> list:
    if os.path.exists(MANUAL_HOLDINGS_FILE):
        with open(MANUAL_HOLDINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_manual_holdings(holdings: list):
    with open(MANUAL_HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)


def _load_pending_alerts() -> dict:
    if os.path.exists(PENDING_ALERTS_FILE):
        with open(PENDING_ALERTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # last_sent 문자열 → datetime 복원
        for v in data.values():
            if isinstance(v.get("last_sent"), str):
                try:
                    v["last_sent"] = datetime.fromisoformat(v["last_sent"])
                except Exception:
                    v["last_sent"] = datetime.now()
        return data
    return {}


def _save_pending_alerts():
    serializable = {}
    for k, v in _pending_sell_alerts.items():
        entry = dict(v)
        if isinstance(entry.get("last_sent"), datetime):
            entry["last_sent"] = entry["last_sent"].isoformat()
        serializable[str(k)] = entry
    with open(PENDING_ALERTS_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def _save_performance(data):
    with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 가격(KRX 시세) 갱신 작업 상태. DART는 캐시를 그대로 쓰므로 보통 수 분 내 끝남.
update_state = {"running": False, "log": "", "done": False, "success": None}

# VI 발동 종목 실시간 캐시 — 1h WebSocket으로 갱신
_vi_active: set = set()  # 현재 VI 발동 중인 종목코드 집합
_update_watched = None  # start_order_realtime()이 반환하는 실시간 종목 등록 함수 (0g/1h/0B)

# 긴급 매도 알림 대기 목록: {message_id: {stk_cd, qty, name, reason, last_sent}}
# 서버 재시작 후에도 pending_alerts.json에서 복원
_pending_sell_alerts: dict = {}
# 당일 "아니" 응답한 종목 (당일 재알림 억제)
_dismissed_sell_alerts: set = set()
# 현재 거래정지 중이라고 이미 알림을 보낸 종목 코드 — 정지 해제되면 제거되어, 나중에
# 다시 정지되면(별개의 새 사건) 다시 알림이 나가도록 한다. (응답 여부와 무관하게 상태로만 추적)
_halt_alerted_codes: set = set()

# 추가 투자 대기 목록: {message_id: {stocks:[{code,name,qty,price}], mode, scheduled_at, last_sent}}
_pending_invest_orders: dict = {}


def _send_telegram_with_id(text: str) -> int | None:
    """텔레그램 메시지 전송 후 message_id 반환 (회신 매칭용)."""
    try:
        from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.json().get("result", {}).get("message_id")
    except Exception:
        return None


def _load_low_volume_names() -> set:
    """data.js stock_flags에서 '5억↓' 태그 종목명 집합 반환."""
    import re
    if not os.path.exists(DATA_JS_PATH):
        return set()
    with open(DATA_JS_PATH, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'(?:stockFlags|stock_flags)\s*[=:]\s*(\{[^;]*?\})\s*[,;]', content, re.DOTALL)
    if not m:
        return set()
    names = set()
    for entry in re.finditer(r'"([^"]+)":\s*\[([^\]]*)\]', m.group(1)):
        if '5억↓' in entry.group(2):
            names.add(entry.group(1))
    return names


def _send_sell_alert(stk_cd: str, qty: int, name: str, reason: str, is_urgent: bool = True) -> int | None:
    """매도 관련 알림 전송 후 message_id 반환.

    is_urgent=True  : 확정·비가역 조치(관리종목/거래정지/정리매매/투자위험) — 되돌릴 여지가 없어 즉시 대응 권고
    is_urgent=False : 잠정·가역 신호(관리우려/단기과열/투자경고/거래대금부족) — 상황을 보며 판단, 강제 아님
    """
    if is_urgent:
        header = "🚨 <b>긴급 매도 알림</b>"
        cta = "👉 이 메시지에 <b>매도</b> 또는 <b>아니</b>로 회신하세요.\n(5분 내 응답 없으면 재발송)"
    else:
        header = "👀 <b>보유 종목 주의</b>"
        cta = "👉 상황을 보고 필요하다고 판단되면 <b>매도</b>로 회신하세요. (즉시 조치 필요는 아닙니다)"
    msg_id = _send_telegram_with_id(
        f"{header}\n"
        f"종목: <b>{name}</b> ({stk_cd})\n"
        f"보유수량: <b>{qty:,}주</b>\n"
        f"사유: <b>{reason}</b>\n\n"
        f"{cta}"
    )
    return msg_id


def _calc_invest_plan(available_cash: int) -> list:
    """포트폴리오 설정 비중에 따라 종목별 매수 수량·금액 계산.

    반환: [{code, name, weight, alloc_amt, price, qty}]
    """
    settings = _load_portfolio_settings()
    stocks   = settings.get("stocks") or []
    if not stocks:
        return []
    total_weight = sum(float(s.get("weight") or 0) for s in stocks)
    if total_weight <= 0:
        return []
    codes  = [s["code"] for s in stocks]
    quotes = kiwoom_api.get_stock_quotes(codes)
    plan   = []
    for s in stocks:
        code   = s["code"]
        name   = s.get("name", code)
        weight = float(s.get("weight") or 0) / total_weight
        alloc  = int(available_cash * weight)
        q      = quotes.get(code)
        price  = (q["price"] if isinstance(q, dict) else q) if q else 0
        qty    = int(alloc / price) if price > 0 else 0
        if qty > 0:
            plan.append({"code": code, "name": name, "weight": round(weight * 100, 1),
                         "alloc_amt": alloc, "price": price, "qty": qty})
    return plan


def _send_invest_alert(plan: list, available_cash: int) -> int | None:
    """추가 투자 알림 발송 후 message_id 반환."""
    lines = [f"💰 <b>추가 투자 알림</b>\n투자가능금액: <b>{available_cash:,}원</b>\n"]
    for s in plan:
        lines.append(f"  {s['name']}({s['weight']}%) "
                     f"→ {s['qty']:,}주 × {s['price']:,}원 = {s['qty']*s['price']:,}원")
    lines.append(
        "\n👉 이 메시지에 회신해주세요:\n"
        "  <b>즉시</b> → 최유리지정가로 바로 주문\n"
        "  <b>동시호가</b> → 15:20 장 마감 동시호가로 주문\n"
        "  <b>취소</b> → 이번 투자 건너뜀\n"
        "(15:00까지 응답 없으면 자동으로 동시호가 진행)"
    )
    return _send_telegram_with_id("\n".join(lines))


def _execute_invest_plan(plan: list, mode: str):
    """추가 투자 계획 실행. mode: 'immediate'(최유리지정가) | 'auction'(동시호가)."""
    results = []
    for s in plan:
        try:
            price = kiwoom_api.get_closing_auction_price(s["code"]) if mode == "auction" else None
            kiwoom_api.place_order(s["code"], s["qty"], "buy", price)
            results.append(f"✅ {s['name']} {s['qty']:,}주")
        except Exception as e:
            results.append(f"❌ {s['name']} 실패: {e}")
    label = "동시호가" if mode == "auction" else "즉시"
    _send_telegram(
        f"📋 <b>추가 투자 주문 완료</b> ({label})\n" + "\n".join(results)
    )


def _check_new_deposit():
    """kt00017 ina_amt를 이전 값과 비교해 새 입금이 있으면 추가 투자 알림 발송.

    last_ina_amt는 알림 발송(또는 "포트폴리오 없음" 안내)까지 성공적으로 끝난 뒤에만
    기록한다. 중간에 예외가 나서 여기서 기록해버리면 사용자는 알림을 영영 못 받는데
    다음 체크부터는 "이미 처리된 입금"으로 취급돼 재시도조차 안 되기 때문이다.
    """
    history = _load_performance()
    prev_ina = int(history.get("last_ina_amt") or 0)
    try:
        dep     = kiwoom_api.get_today_deposit()
        cur_ina = int(dep.get("ina_amt") or 0)
    except Exception:
        return
    if cur_ina <= prev_ina:
        return

    try:
        # 전체 주문가능금액으로 투자 계획 수립
        try:
            dep_detail    = kiwoom_api.get_deposit_detail()
            available_cash = int(dep_detail.get("ord_alow_amt") or 0)
        except Exception:
            available_cash = cur_ina

        plan = _calc_invest_plan(available_cash)
        if not plan:
            _send_telegram("💰 입금 감지됐으나 포트폴리오 설정이 없습니다.\n추가 투자 메뉴에서 종목을 설정해주세요.")
        else:
            msg_id = _send_invest_alert(plan, available_cash)
            if msg_id:
                _pending_invest_orders[msg_id] = {
                    "plan":         plan,
                    "available_cash": available_cash,
                    "mode":         None,          # 응답 전
                    "last_sent":    datetime.now(),
                    "confirmed":    False,
                }
    except Exception:
        return  # 알림 발송 실패 — last_ina_amt를 기록하지 않아 다음 체크에서 재시도됨

    # 여기까지 왔다면 알림(또는 안내) 발송이 끝난 것이므로 이제 기록한다
    history["last_ina_amt"] = cur_ina
    _save_performance(history)


def _check_holding_conditions(check_volume: bool = False):
    """보유 종목 조건 점검 — 확정·비가역(즉시 대응) / 잠정·가역(상황 판단) 두 등급으로 구분.

    확정·비가역 — 거래소가 이미 결론 내려 되돌릴 여지가 없음: 거래정지, 관리종목, 정리매매(상장폐지확정), 투자위험
    잠정·가역   — 일시적이고 며칠~몇 주 내 풀리는 경우가 많음: 관리종목 지정우려, 단기과열, 투자경고, 거래대금부족

    check_volume=True 이면 5억 미만 거래대금도 체크 (08:00 1회 실행). 나머지는 매 10분 점검.
    """
    try:
        h = kiwoom_api.get_holdings()
        stocks = h.get("acnt_evlt_remn_indv_tot", [])
        if not stocks:
            return
        low_vol_names = _load_low_volume_names() if check_volume else set()
        codes = [(s.get("stk_cd") or "").lstrip("A") for s in stocks if s.get("stk_cd")]
        name_map = {(s.get("stk_cd") or "").lstrip("A"): s.get("stk_nm", "") for s in stocks}
        qty_map  = {(s.get("stk_cd") or "").lstrip("A"): int(s.get("rmnd_qty") or 0) for s in stocks}

        warnings = kiwoom_api.check_stock_warning(codes)

        for code in codes:
            if code in _dismissed_sell_alerts:
                continue
            w     = warnings.get(code, {})
            name  = name_map.get(code, code)
            qty   = qty_map.get(code, 0)
            state = w.get("state", "")
            owarn = str(w.get("orderWarning", "0"))

            urgent_reasons = []  # 확정·비가역 — 즉시 대응 권고
            watch_reasons  = []  # 잠정·가역 — 상황을 보며 판단, 강제 아님

            if "정지" in state:
                urgent_reasons.append("거래정지")
            if "관리" in state and "우려" not in state:
                urgent_reasons.append("관리종목")
            if owarn == "2":
                urgent_reasons.append("정리매매(상장폐지확정)")
            if owarn == "4":
                urgent_reasons.append("투자위험")
            if "우려" in state:
                watch_reasons.append("관리종목 지정우려")
            if owarn == "3":
                watch_reasons.append("단기과열")
            if owarn == "5":
                watch_reasons.append("투자경고")
            if check_volume and name in low_vol_names:
                watch_reasons.append("거래대금 5억 미만")

            if not urgent_reasons and not watch_reasons:
                _halt_alerted_codes.discard(code)
                continue

            # 거래정지는 매도 자체가 불가능해서 반복 알림이 무의미하다. 다만 "응답 대기 중이냐"가
            # 아니라 "지금도 정지 상태가 이어지고 있냐"로 판단해야, 정지가 풀렸다가 나중에 별개
            # 사유로 다시 정지되는 경우엔 새로 알림이 나간다.
            if "거래정지" in urgent_reasons:
                if code in _halt_alerted_codes:
                    continue
                _halt_alerted_codes.add(code)
            else:
                _halt_alerted_codes.discard(code)

            is_urgent = bool(urgent_reasons)
            reason_str = " · ".join(urgent_reasons + watch_reasons)
            # 이미 대기 중인 알림 있으면 5분 경과 시 재발송
            existing = next((v for v in _pending_sell_alerts.values() if v["stk_cd"] == code), None)
            if existing:
                if (datetime.now() - existing["last_sent"]).total_seconds() >= 300:
                    existing["last_sent"] = datetime.now()
                    _send_sell_alert(code, qty, name, reason_str, is_urgent)
            else:
                msg_id = _send_sell_alert(code, qty, name, reason_str, is_urgent)
                if msg_id:
                    _pending_sell_alerts[msg_id] = {
                        "stk_cd":    code,
                        "qty":       qty,
                        "name":      name,
                        "reason":    reason_str,
                        "last_sent": datetime.now(),
                    }
    except Exception:
        pass


def _run_update_data():
    update_state.update(running=True, log="", done=False, success=None)
    # 완료 감지를 이 프로세스/스레드 생존 여부에 의존하지 않도록, 트리거 시각을 파일로 남겨둔다.
    # (_daily_alert_scheduler가 주기적으로 이 파일과 update_summary.json 갱신 시각을 비교해서
    # 완료를 감지하고 텔레그램을 보낸다 - order_server.py가 도중에 재시작돼도 안 끊긴다.)
    try:
        with open(MANUAL_UPDATE_MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump({"triggered_at": datetime.now().isoformat()}, f)
    except Exception:
        pass
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "update_data.py", "--no-deploy"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in proc.stdout:
            update_state["log"] += line
        proc.wait()
        update_state["success"] = (proc.returncode == 0)
    except Exception as e:
        update_state["log"] += f"\n오류: {e}"
        update_state["success"] = False
    finally:
        update_state["running"] = False
        update_state["done"] = True


@app.route("/api/holdings", methods=["GET"])
def holdings():
    try:
        data = kiwoom_api.get_holdings()
        codes = [(r.get("stk_cd") or "").lstrip("A")
                 for r in data.get("acnt_evlt_remn_indv_tot", []) if r.get("stk_cd")]
        if codes and _update_watched:
            _update_watched(codes)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stock_warnings", methods=["GET"])
def stock_warnings():
    """ka10100 - 매수 대상 종목의 투자유의 여부 확인"""
    codes = [c for c in (request.args.get("codes") or "").split(",") if c]
    if not codes:
        return jsonify({"error": "codes 파라미터가 필요합니다."}), 400
    try:
        return jsonify(kiwoom_api.check_stock_warning(codes))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/realized_pl", methods=["GET"])
def realized_pl():
    """ka10072 - 종목별 실현손익·실제 수수료·세금 조회"""
    code = request.args.get("code", "")
    from_dt = request.args.get("from", "")
    if not code:
        return jsonify({"error": "code 파라미터가 필요합니다."}), 400
    try:
        return jsonify(kiwoom_api.get_realized_pl(code, from_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily_balance", methods=["GET"])
def daily_balance():
    """ka01690 - 일별잔고수익률. date 파라미터 없으면 당일."""
    qry_dt = request.args.get("date", "")
    try:
        return jsonify(kiwoom_api.get_daily_balance(qry_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order_possible_qty", methods=["GET"])
def order_possible_qty():
    """kt00010 - 종목/가격 기준 주문가능수량 (수수료 포함 100% 증거금 기준)."""
    stk_cd = request.args.get("stk_cd", "")
    price  = int(request.args.get("price", "0") or 0)
    trde_tp = request.args.get("trde_tp", "2")
    if not stk_cd or not price:
        return jsonify({"error": "stk_cd, price 필수"}), 400
    try:
        return jsonify(kiwoom_api.get_order_possible_qty(stk_cd, price, trde_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily_contract", methods=["GET"])
def daily_contract():
    """kt00009 - 당일 매도/매수/전체 약정금액 합계."""
    try:
        return jsonify(kiwoom_api.get_daily_contract_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transactions", methods=["GET"])
def transactions():
    """kt00015 - 위탁종합거래내역 (기간/구분 조회)."""
    strt_dt = request.args.get("strt_dt", "")
    end_dt  = request.args.get("end_dt", "")
    tp      = request.args.get("tp", "0")
    stk_cd  = request.args.get("stk_cd", "")
    if not strt_dt or not end_dt:
        return jsonify({"error": "strt_dt, end_dt 필수"}), 400
    try:
        rows = kiwoom_api.get_transaction_history(strt_dt, end_dt, tp, stk_cd)
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/net_deposit", methods=["GET"])
def net_deposit():
    """kt00015 기반 기간 순입금(입금-출금) + 세금/수수료 합계."""
    strt_dt = request.args.get("strt_dt", "")
    end_dt  = request.args.get("end_dt", "")
    if not strt_dt or not end_dt:
        return jsonify({"error": "strt_dt, end_dt 필수"}), 400
    try:
        return jsonify(kiwoom_api.get_net_deposit(strt_dt, end_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/today_deposit", methods=["GET"])
def today_deposit():
    """kt00017 - 당일현황 (입금/출금/매도/매수/수수료/세금/배당금)."""
    try:
        return jsonify(kiwoom_api.get_today_deposit())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/next_day_settlement", methods=["GET"])
def next_day_settlement():
    """kt00008 - 익일 결제 예정 내역 (매도/매수 정산합, 종목별 정산금액/수수료/세금)."""
    try:
        return jsonify(kiwoom_api.get_next_day_settlement())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/account_eval", methods=["GET"])
def account_eval():
    """kt00004 - 계좌평가현황 (당일/당월/누적 손익 + 종목별 금일 매수/매도 수량)."""
    try:
        return jsonify(kiwoom_api.get_account_eval())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/deposit", methods=["GET"])
def deposit_detail():
    """kt00001 - 예수금상세현황 (예수금/출금가능/주문가능/D+1~D+2 추정예수금).

    D+1/D+2 추정예수금 필드는 qry_tp="3"(추정조회)일 때만 채워지므로 기본값을 3으로 둔다.
    """
    qry_tp = request.args.get("qry_tp", "3")
    try:
        return jsonify(kiwoom_api.get_deposit_detail(qry_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settlement_balance", methods=["GET"])
def settlement_balance():
    """kt00005 - 주문가능현금(ord_alowa) + 종목별 결제잔고(setl_remn) 조회"""
    try:
        return jsonify(kiwoom_api.get_settlement_balance())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order_status", methods=["GET"])
def order_status():
    qry_tp = request.args.get("qry_tp", "1")
    try:
        return jsonify(kiwoom_api.get_order_status(qry_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open_orders", methods=["GET"])
def open_orders():
    """kt00007 - 미체결 주문만 직접 조회 (qry_tp=3)"""
    sell_tp = request.args.get("sell_tp", "0")
    try:
        return jsonify(kiwoom_api.get_open_orders(sell_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fills", methods=["GET"])
def fills():
    sell_tp = request.args.get("sell_tp", "0")
    try:
        return jsonify(kiwoom_api.get_fills(sell_tp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/quotes", methods=["GET"])
def quotes():
    codes = [c for c in (request.args.get("codes") or "").split(",") if c]
    if not codes:
        return jsonify({"error": "codes 파라미터가 필요합니다."}), 400
    try:
        return jsonify({"prices": kiwoom_api.get_stock_quotes(codes)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/year_high_ratio", methods=["GET"])
def year_high_ratio():
    codes = [c for c in (request.args.get("codes") or "").split(",") if c]
    if not codes:
        return jsonify({"error": "codes 파라미터가 필요합니다."}), 400
    try:
        return jsonify(kiwoom_api.get_year_high_ratios(codes))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/manual_holdings", methods=["GET"])
def get_manual_holdings():
    """API 미연동 증권계좌의 보유종목(매도 검토 탭)을 조회 - 종목코드/수량/평단가."""
    return jsonify(_load_manual_holdings())


@app.route("/api/manual_holdings", methods=["POST"])
def save_manual_holdings():
    """매도 검토 탭의 보유종목 목록(전체)을 저장."""
    data = request.get_json(force=True) or {}
    holdings = data.get("holdings") or []
    _save_manual_holdings(holdings)
    return jsonify({"ok": True, "saved": len(holdings)})


@app.route("/api/watchlist_quotes", methods=["GET"])
def watchlist_quotes():
    """매도 검토 탭용 - 종목코드별 시세·밸류에이션·수급·이동평균 조회."""
    codes = [c for c in (request.args.get("codes") or "").split(",") if c]
    if not codes:
        return jsonify({"error": "codes 파라미터가 필요합니다."}), 400
    try:
        return jsonify(kiwoom_api.get_watchlist_quotes(codes))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio/settings", methods=["GET"])
def get_portfolio_settings():
    """브라우저 추가투자 메뉴의 종목·비중 설정을 조회."""
    return jsonify(_load_portfolio_settings())


@app.route("/api/portfolio/settings", methods=["POST"])
def save_portfolio_settings():
    """브라우저에서 추가투자 메뉴 설정(종목·비중·총투자금)을 서버에 저장."""
    data = request.get_json(force=True) or {}
    stocks = data.get("stocks") or []
    if not stocks:
        return jsonify({"error": "stocks가 비어있습니다."}), 400
    settings = {
        "stocks": stocks,                              # [{code, name, weight}]
        "total_investment": data.get("total_investment", 0),
        "updated_at": datetime.now().isoformat(),
    }
    _save_portfolio_settings(settings)
    return jsonify({"ok": True, "saved": len(stocks)})


@app.route("/api/recommend_settings", methods=["POST"])
def save_recommend_settings():
    """index.html의 추천 메뉴 옵션(소형주 토글/표시개수/계절제외/체크한 전략)을 저장.

    브라우저 localStorage에만 있던 설정을 서버도 알게 해서, _compute_recommend_top50()이
    실제 화면과 동일한 기준으로 종합순위 top50을 계산할 수 있게 한다.
    """
    data = request.get_json(force=True) or {}
    settings = {
        "smallcapMode": data.get("smallcapMode", "none"),
        "momentumSmallcapMode": data.get("momentumSmallcapMode", "none"),
        "ultraSmallcapMode": data.get("ultraSmallcapMode", "none"),
        "displayCountMode": data.get("displayCountMode", "20"),
        "isExcludeSeasonMode": bool(data.get("isExcludeSeasonMode")),
        "recommendCheckedKeys": data.get("recommendCheckedKeys") or [],
    }
    with open(RECOMMEND_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)
    return jsonify({"ok": True})


@app.route("/api/vi_stocks", methods=["GET"])
def vi_stocks():
    """ka10054 - 현재 VI 발동 중인 종목 목록 + 실시간 캐시."""
    try:
        data = kiwoom_api.get_vi_stocks()
        # 실시간 캐시도 병합
        data["realtime_codes"] = list(_vi_active)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "realtime_codes": list(_vi_active)}), 500


@app.route("/api/daily_realized_pl", methods=["GET"])
def daily_realized_pl():
    """ka10074 - 일자별 실현손익 (당일 또는 date 파라미터 지정일)."""
    qry_dt = request.args.get("date", "")
    try:
        return jsonify(kiwoom_api.get_daily_realized_pl(qry_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/today_journal", methods=["GET"])
def today_journal():
    """ka10170 - 당일매매일지 (오늘 전체 매수/매도 체결 내역)."""
    try:
        return jsonify(kiwoom_api.get_today_trade_journal())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _positive_int(value):
    """양의 정수만 통과. 음수/0/NaN/문자열 쓰레기값 등은 None을 반환해 호출측에서 거부하게 한다."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _positive_number(value):
    """양수만 통과 (가격용). None/0/음수/변환불가는 None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@app.route("/api/order", methods=["POST"])
def order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    side = data.get("side")
    qty = _positive_int(data.get("qty"))
    # 가격은 시장가/최유리지정가 주문 시 생략 가능 — 값이 주어졌을 때만 양수인지 검증
    raw_price = data.get("price")
    price = _positive_number(raw_price) if raw_price not in (None, "") else None
    if not stk_cd or qty is None or side not in ("buy", "sell"):
        return jsonify({"error": "stk_cd, qty(양의 정수), side(buy/sell)는 필수입니다."}), 400
    if raw_price not in (None, "") and price is None:
        return jsonify({"error": "price는 양수여야 합니다."}), 400
    if side == "buy":
        # VI 발동(잠정·가역) — 해제되면 자동으로 다시 매수 가능
        if stk_cd in _vi_active:
            return jsonify({"error": f"VI 발동 중인 종목({stk_cd})은 매수할 수 없습니다. VI 해제 후 재시도하세요."}), 400
        # 확정·비가역 조치(관리종목/거래정지/정리매매/투자위험) — 개별/일괄 주문 어느 경로든 동일하게 차단
        try:
            block_reason = kiwoom_api.get_hard_block_reason(stk_cd)
        except Exception:
            block_reason = None  # 조회 실패 시엔 차단하지 않고 키움 서버 판단에 맡김
        if block_reason:
            return jsonify({"error": f"{block_reason} 종목({stk_cd})은 매수할 수 없습니다."}), 400
    try:
        result = kiwoom_api.place_order(stk_cd, qty, side, price)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/modify_order", methods=["POST"])
def modify_order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    orig_ord_no = data.get("orig_ord_no")
    mdfy_qty = _positive_int(data.get("mdfy_qty"))
    mdfy_uv = _positive_number(data.get("mdfy_uv"))
    if not stk_cd or not orig_ord_no or mdfy_qty is None or mdfy_uv is None:
        return jsonify({"error": "stk_cd, orig_ord_no, mdfy_qty(양의 정수), mdfy_uv(양수)는 필수입니다."}), 400
    try:
        result = kiwoom_api.modify_order(stk_cd, orig_ord_no, mdfy_qty, mdfy_uv)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cancel_order", methods=["POST"])
def cancel_order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    orig_ord_no = data.get("orig_ord_no")
    cncl_qty = _positive_int(data.get("cncl_qty"))
    if not stk_cd or not orig_ord_no or cncl_qty is None:
        return jsonify({"error": "stk_cd, orig_ord_no, cncl_qty(양의 정수)는 필수입니다."}), 400
    try:
        result = kiwoom_api.cancel_order(stk_cd, orig_ord_no, cncl_qty)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update_data", methods=["POST"])
def update_data():
    if update_state["running"]:
        return jsonify({"error": "이미 갱신이 진행 중입니다."}), 409
    threading.Thread(target=_run_update_data, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/update_status", methods=["GET"])
def update_status():
    return jsonify(update_state)


@app.route("/api/performance/baseline", methods=["POST"])
def performance_baseline():
    """① 기준점 기록 - 매수/리밸런싱 시점의 종목별 매수가/수량/비중 + 벤치마크 ETF 가격을 저장."""
    data = request.get_json(force=True) or {}
    stocks = data.get("stocks") or []
    if not stocks:
        return jsonify({"error": "stocks가 비어있습니다."}), 400
    try:
        codes = [s["code"] for s in stocks] + list(BENCHMARK_CODES.values())
        quotes = kiwoom_api.get_stock_quotes(codes)

        history = _load_performance()
        prev_baseline = history["baselines"][-1] if history["baselines"] else None

        baseline_stocks = []
        for s in stocks:
            price = quotes.get(s["code"]) or s.get("price")
            baseline_stocks.append({
                "name": s["name"], "code": s["code"],
                "price": price, "qty": s["qty"], "weight": s.get("weight"),
                "strategyRanks": s.get("strategyRanks") or {},
            })

        new_baseline = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "totalInvestment": data.get("totalInvestment"),
            "stocks": baseline_stocks,
            "benchmark": {k: quotes.get(v) for k, v in BENCHMARK_CODES.items()},
            "snapshots": [],
        }

        # ⑤ 직전 기준점과 비교 - 유지/제외된 종목과 그 시점까지의 수익률
        if prev_baseline:
            prev_codes = {s["code"]: s for s in prev_baseline["stocks"]}
            new_codes = {s["code"] for s in baseline_stocks}
            kept, removed = [], []
            for code, s in prev_codes.items():
                cur_price = quotes.get(code)
                ret = ((cur_price - s["price"]) / s["price"] * 100) if (cur_price and s.get("price")) else None
                entry = {"name": s["name"], "code": code, "entry_price": s["price"], "exit_price": cur_price, "return_pct": ret}
                (kept if code in new_codes else removed).append(entry)
            new_baseline["previousComparison"] = {
                "previousBaselineId": prev_baseline["id"],
                "kept": kept, "removed": removed,
                "added": [s for s in baseline_stocks if s["code"] not in prev_codes],
            }

        history["baselines"].append(new_baseline)
        _save_performance(history)
        return jsonify(new_baseline)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance/snapshot", methods=["POST"])
def performance_snapshot():
    """② 주기적 스냅샷 저장 - ka01690(일별잔고수익률)으로 브로커 기준 정확한 평가금액 사용."""
    history = _load_performance()
    if not history["baselines"]:
        return jsonify({"error": "기준점이 없습니다. 먼저 기준점을 기록해주세요."}), 400
    baseline = history["baselines"][-1]
    try:
        # ka01690으로 당일 브로커 기준 평가금액·종목별 현재가 조회
        daily = kiwoom_api.get_daily_balance()
        if not daily.get("error") and daily.get("tot_evlt_amt"):
            total_value = (int(daily.get("tot_evlt_amt") or 0) +
                           int(daily.get("dbst_bal") or 0))
            price_map = {
                (r.get("stk_cd") or "").replace("A", "").replace("*", ""): int(r.get("cur_prc") or 0)
                for r in (daily.get("day_bal_rt") or [])
            }
        else:
            # ka01690 실패 시 ka10001 폴백
            codes = [s["code"] for s in baseline["stocks"]]
            quotes_raw = kiwoom_api.get_stock_quotes(codes)
            price_map = {
                code: (v["price"] if isinstance(v, dict) else v)
                for code, v in quotes_raw.items() if v
            }
            total_value = sum(
                price_map.get(s["code"], 0) * s["qty"]
                for s in baseline["stocks"]
            )

        # total_value(ka01690 tot_evlt_amt+dbst_bal)는 수수료/미수정리 미반영 단순합이라
        # 다른 성과추적 화면(추정예탁자산 기준)과 어긋난다 — 같은 기준으로 통일한다.
        try:
            total_value = int(kiwoom_api.get_holdings().get("prsm_dpst_aset_amt") or 0) or total_value
        except Exception:
            pass

        stock_values = []
        for s in baseline["stocks"]:
            price = price_map.get(s["code"])
            value = (price * s["qty"]) if price else None
            entry_price = s.get("price")
            ret = ((price - entry_price) / entry_price * 100) if (price and entry_price) else None
            stock_values.append({"code": s["code"], "name": s["name"], "price": price, "value": value, "return_pct": ret})

        # 벤치마크는 기존 방식 유지
        bench_quotes = kiwoom_api.get_stock_quotes(list(BENCHMARK_CODES.values()))
        bench_prices = {
            k: (v["price"] if isinstance(v, dict) else v)
            for k, v in bench_quotes.items() if v
        }

        snapshot = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "totalValue": total_value,
            "stocks": stock_values,
            "benchmark": {k: bench_prices.get(v) for k, v in BENCHMARK_CODES.items()},
        }
        baseline["snapshots"].append(snapshot)
        _save_performance(history)

        # 텔레그램 성과 알림
        base_value = baseline.get("totalInvestment") or total_value
        ret_pct = ((total_value - base_value) / base_value * 100) if base_value else 0
        # 종목 수가 많아 전부 나열하면 알림만 길어지고 눈에 안 들어오므로,
        # 기준점(매수가) 대비 ±5% 이상 움직인 종목만 골라서 보여준다.
        SIGNIFICANT_MOVE_PCT = 5.0
        rated = [s for s in stock_values if s.get("return_pct") is not None]
        significant = [s for s in rated if abs(s["return_pct"]) >= SIGNIFICANT_MOVE_PCT]
        significant.sort(key=lambda s: s["return_pct"], reverse=True)
        quiet_count = len(rated) - len(significant)
        if significant:
            stock_lines = f"유의미한 변동 (±{SIGNIFICANT_MOVE_PCT:.0f}% 이상)\n" + "\n".join(
                f"  • {s['name']}: {s['return_pct']:+.1f}%" for s in significant
            )
            if quiet_count > 0:
                stock_lines += f"\n(나머지 {quiet_count}종목은 ±{SIGNIFICANT_MOVE_PCT:.0f}% 이내 보합)"
        else:
            stock_lines = f"±{SIGNIFICANT_MOVE_PCT:.0f}% 이상 변동한 종목 없음 (전체 {len(rated)}종목 보합)"
        bench = snapshot["benchmark"]
        kospi_str  = f"{bench['kospi']:,}원" if bench.get("kospi") else "-"
        kosdaq_str = f"{bench['kosdaq']:,}원" if bench.get("kosdaq") else "-"
        _send_telegram(
            f"📊 <b>성과 스냅샷</b> ({snapshot['datetime']})\n"
            f"총평가금액: <b>{total_value:,}원</b>  ({ret_pct:+.2f}%)\n"
            f"KOSPI ETF: {kospi_str} / KOSDAQ ETF: {kosdaq_str}\n\n"
            f"{stock_lines}"
        )
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notify", methods=["POST"])
def notify():
    """브라우저에서 직접 텔레그램 메시지 전송 (주문 완료, 성과 알림 등)."""
    data = request.get_json(force=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "text가 비어있습니다."}), 400
    try:
        _send_telegram(text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/realtime_balance", methods=["GET"])
def realtime_balance():
    """04 실시간 잔고 캐시 반환 — {종목코드: {필드코드: 값}}"""
    return jsonify(kiwoom_api.realtime_balance)


@app.route("/api/realtime_quotes", methods=["GET"])
def realtime_quotes():
    """0B 실시간 체결가 캐시 반환 — {종목코드: {price, rate, time}} — REST 호출 없이 즉시 조회"""
    return jsonify(kiwoom_api.realtime_quotes)


@app.route("/api/realtime_status", methods=["GET"])
def realtime_status():
    """00/04/0B 웹소켓 이벤트 발생 시마다 증가하는 카운터 — REST 호출 없이 변경 여부만 감지"""
    return jsonify({"seq": kiwoom_api.realtime_seq["n"]})


@app.route("/api/realtime_fills", methods=["GET"])
def realtime_fills():
    """최근 체결(00) 이벤트 목록 반환 — 미체결 현황을 즉시 갱신할지 판단하는 용도"""
    return jsonify(kiwoom_api.recent_fills)


@app.route("/api/performance/history", methods=["GET"])
def performance_history():
    return jsonify(_load_performance())


@app.route("/api/performance/summary", methods=["GET"])
def performance_summary():
    """총평가금액 + 누계순입금 + 수익금액/수익률 JSON 반환."""
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        history = _load_performance()
        base_dt = history.get("tracking_start_date", "")
        if not base_dt and history.get("baselines"):
            base_dt = history["baselines"][-1].get("date", "").replace("-", "")[:8]

        total_asset = 0
        net_in = 0
        evltv_prft = 0
        prft_rt = 0.0
        tax_cmsn = 0
        if base_dt:
            perf = kiwoom_api.get_period_eval(fr_dt=base_dt, to_dt=today_str)
            # kt00016의 tot_amt_to는 예수금+유가증권평가금액 단순합이라 수수료/미수정리 등이
            # 반영 안 된 총계다. 실제 순자산인 추정예탁자산(kt00018)을 총평가금액으로 쓴다.
            try:
                total_asset = int(kiwoom_api.get_holdings().get("prsm_dpst_aset_amt") or 0)
            except Exception:
                total_asset = int(perf.get("tot_amt_to") or 0)
            base_asset   = int(perf.get("tot_amt_fr") or 0)   # 기준일 순자산 (수익금액 계산용, 화면엔 미표시)
            invt_bsamt   = int(perf.get("invt_bsamt") or 0)   # 투자원금평잔
            tern_rt      = float(perf.get("tern_rt") or 0)    # 회전율
            # kt00016의 evltv_prft/prft_rt는 tot_amt_to(단순합) 기준으로 계산돼 있어,
            # total_asset을 추정예탁자산으로 바꾼 뒤에는 서로 안 맞는다. 같은 기준으로 재계산한다.
            # kt00016의 termin_tot_trns/termin_tot_pymn은 to_dt가 오늘이면 당일 입출금까지
            # 이미 실시간 반영되어 있다 (직접 확인함) — 여기에 kt00017 당일 입금을 또 더하면
            # 이중 계산되어 실제보다 크게(당일 입금분만큼) 잘못 표시된다.
            net_in = int(perf.get("termin_tot_trns") or 0) - int(perf.get("termin_tot_pymn") or 0)
            evltv_prft = total_asset - base_asset - net_in
            invested   = base_asset + net_in
            prft_rt    = (evltv_prft / invested * 100) if invested > 0 else 0.0
            try:
                tax_cmsn = kiwoom_api.get_net_deposit(base_dt, today_str).get("tax_cmsn", 0)
            except Exception:
                tax_cmsn = 0

        return jsonify({
            "total_asset": total_asset,
            "invt_bsamt":  invt_bsamt,
            "tax_cmsn":    tax_cmsn,
            "tern_rt":     tern_rt,
            "net_in": net_in,
            "profit": evltv_prft,
            "profit_pct": prft_rt,
            "base_dt": base_dt,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _build_stock_heatmap_text(pairs, cols: int = 3) -> str:
    """[(종목명, 등락률)] 목록을 이모지 히트맵 텍스트로 변환. 수익률 높은 순으로 정렬해서
    한눈에 잘된 종목/못된 종목을 구분할 수 있게 한다."""
    def color(pct):
        if pct >= 5:
            return "🟩"
        if pct <= -5:
            return "🟥"
        return "🟨"

    ordered = sorted(pairs, key=lambda p: p[1], reverse=True)
    cells = [f"{color(pct)}{name} {pct:+.1f}%" for name, pct in ordered]
    rows = ["  ".join(cells[i:i + cols]) for i in range(0, len(cells), cols)]
    return "\n".join(rows)


def _build_daily_summary(label: str) -> str:
    """kt00016(기간수익률) + ka01690(종목별 수익률)으로 텔레그램 요약 메시지 생성."""
    try:
        from datetime import timedelta
        today_str = datetime.now().strftime("%Y%m%d")

        # 총평가금액 (kt00018)
        holdings_data = kiwoom_api.get_holdings()
        total_asset = int(holdings_data.get("prsm_dpst_aset_amt") or 0)

        # 당월/누적 손익 (kt00004) - 일부 계좌에서 이 필드들이 항상 0으로 내려오는 경우가 있어
        # month_pl/cum_pl이 0이면 아래 "당월 손익/누적 손익" 줄 자체를 생략하도록 그대로 둔다.
        month_pl = month_rt = cum_pl = cum_rt = 0
        try:
            acnt = kiwoom_api.get_account_eval()
            month_pl = int(acnt.get("lspft2") or 0)
            month_rt = float(acnt.get("lspft_ratio") or 0)
            cum_pl   = int(acnt.get("lspft") or 0)
            cum_rt   = float(acnt.get("lspft_rt") or 0)
        except Exception:
            pass

        # 당일 손익 (kt00002 일별추정예탁자산 차이 기반) - kt00004의 tdy_lspft가 일부 계좌에서
        # 항상 0으로 내려오는 문제가 있어, 전일 대비 추정예탁자산 증감에서 당일 입출금을 뺀
        # 값으로 직접 계산한다.
        day_pl = day_rt = 0
        try:
            hist = kiwoom_api.get_daily_asset_history(
                (datetime.now() - timedelta(days=10)).strftime("%Y%m%d"), today_str
            )
            rows = sorted(hist.get("daly_prsm_dpst_aset_amt_prst") or [], key=lambda r: r.get("dt", ""))
            prev_rows = [r for r in rows if r.get("dt") != today_str]
            if prev_rows:
                prev_asset = int(prev_rows[-1].get("prsm_dpst_aset_amt") or 0)
                net_cash_in = 0
                try:
                    tdy = kiwoom_api.get_today_deposit()
                    net_cash_in = int(tdy.get("ina_amt") or 0) - int(tdy.get("outa") or 0)
                except Exception:
                    pass
                day_pl = total_asset - prev_asset - net_cash_in
                day_rt = (day_pl / prev_asset * 100) if prev_asset > 0 else 0
        except Exception:
            # fallback: 종목별 전일 대비 현재가로 직접 계산 (평가금액 변동만 반영, 입출금 미보정)
            for s in holdings_data.get("acnt_evlt_remn_indv_tot", []):
                cur = int(s.get("cur_prc") or 0)
                pred = int(s.get("pred_close_pric") or 0)
                qty = int(s.get("rmnd_qty") or 0)
                day_pl += (cur - pred) * qty
            total_eval = int(holdings_data.get("tot_evlt_amt") or 0)
            day_rt = (day_pl / (total_eval - day_pl) * 100) if (total_eval - day_pl) > 0 else 0

        # 누계 수익률 (kt00016, 기준일~오늘)
        cum_str = ""
        history = _load_performance()
        base_dt = history.get("tracking_start_date", "")
        if not base_dt and history.get("baselines"):
            base_dt = history["baselines"][-1].get("date", "").replace("-", "")[:8]
        if base_dt:
            try:
                perf = kiwoom_api.get_period_eval(fr_dt=base_dt, to_dt=today_str)
                # kt00016의 evltv_prft/prft_rt는 tot_amt_to(예수금+유가증권평가금액 단순합) 기준이라
                # 위에서 "총평가금액"으로 쓰는 kt00018 추정예탁자산(total_asset)과 기준이 달라서 서로
                # 안 맞는다. /api/performance/summary(성과추적 화면)와 동일하게, total_asset 기준으로
                # 재계산해서 화면·텔레그램이 항상 같은 숫자를 보여주게 한다.
                base_asset = int(perf.get("tot_amt_fr") or 0)
                # kt00016은 to_dt가 오늘이면 당일 입출금까지 이미 반영되어 있으므로 더 더하지 않는다.
                net_in = int(perf.get("termin_tot_trns") or 0) - int(perf.get("termin_tot_pymn") or 0)
                evltv_prft = total_asset - base_asset - net_in
                invested = base_asset + net_in
                prft_rt = (evltv_prft / invested * 100) if invested > 0 else 0.0
                tdy_sell = tdy_buy = tdy_cmsn = tdy_tax = tdy_dvida = 0
                try:
                    today_dep = kiwoom_api.get_today_deposit()
                    tdy_sell  = int(today_dep.get("sell_amt") or 0)
                    tdy_buy   = int(today_dep.get("buy_amt") or 0)
                    tdy_cmsn  = int(today_dep.get("cmsn") or 0)
                    tdy_tax   = int(today_dep.get("tax") or 0)
                    tdy_dvida = int(today_dep.get("dvida_amt") or 0)
                except Exception:
                    pass
                tdy_trade_str = ""
                if tdy_sell or tdy_buy:
                    tdy_trade_str = (
                        f"\n당일 매도: <b>{tdy_sell:,}원</b>  매수: <b>{tdy_buy:,}원</b>"
                        f"\n수수료: <b>{tdy_cmsn:,}원</b>  세금: <b>{tdy_tax:,}원</b>"
                    )
                dvida_str = f"\n💰 배당금: <b>{tdy_dvida:,}원</b>" if tdy_dvida > 0 else ""
                cum_str = (
                    f"\n누계 순입금: <b>{net_in:,}원</b>\n"
                    f"누계 수익률: <b>{evltv_prft:+,.0f}원</b> ({prft_rt:+.2f}%)"
                    f"{tdy_trade_str}"
                    f"{dvida_str}"
                )
            except Exception:
                pass

        # 종목별 수익률 최고/최저 (kt00018 prft_rt 재사용 — 별도 API 호출 불필요)
        stock_lines = ""
        indv = [s for s in holdings_data.get("acnt_evlt_remn_indv_tot", []) if s.get("prft_rt")]
        if indv:
            best  = max(indv, key=lambda s: float(s["prft_rt"]))
            worst = min(indv, key=lambda s: float(s["prft_rt"]))
            stock_lines = (
                f"\n📈 최고: {best['stk_nm']} ({float(best['prft_rt']):+.2f}%)"
                f"\n📉 최저: {worst['stk_nm']} ({float(worst['prft_rt']):+.2f}%)"
            )

        # 당일 약정금액 (kt00009)
        contract_str = ""
        try:
            ct = kiwoom_api.get_daily_contract_summary()
            if ct["total"] > 0:
                contract_str = (
                    f"\n당일 약정금액: <b>{ct['total']:,}원</b>"
                    f" (매도 {ct['sell_amt']:,} / 매수 {ct['buy_amt']:,})"
                )
        except Exception:
            pass

        # 익일 결제 예정 (kt00008)
        setl_str = ""
        try:
            setl = kiwoom_api.get_next_day_settlement()
            sell_sum = int(setl.get("sell_amt_sum") or 0)
            buy_sum  = int(setl.get("buy_amt_sum") or 0)
            rows = setl.get("acnt_nxdy_setl_frcs_prps_array") or []
            tot_cmsn = sum(int(r.get("cmsn") or 0) + int(r.get("trde_tax") or 0)
                           + int(r.get("incm_tax") or 0) + int(r.get("rstx") or 0)
                           + int(r.get("resi_tax") or 0) for r in rows)
            if sell_sum or buy_sum:
                net_settle = sell_sum - buy_sum - tot_cmsn
                cash_note = (
                    " (출금 준비 필요)" if net_settle < 0 else ""
                )
                setl_str = (
                    f"\n📅 익일 결제 예정"
                    f"\n순정산: <b>{net_settle:+,}원</b>{cash_note}"
                    f" (매도 {sell_sum:,} - 매수 {buy_sum:,} - 비용 {tot_cmsn:,})"
                )
                # 부분체결 등으로 같은 종목이 여러 행에 나뉘어 오므로 종목별로 합산해서 한 줄씩만 보여준다.
                net_by_stock = {}
                for r in rows:
                    nm = r.get("stk_nm", "")
                    tp = "매도" if "매도" in r.get("sell_tp", "") else "매수"
                    # exct_amt는 키움이 부호 없는 절대값으로 내려주므로, 매도=입금(+)/매수=출금(-)을 직접 적용한다.
                    exct = int(r.get("exct_amt") or 0)
                    signed_exct = exct if tp == "매도" else -exct
                    net_by_stock[nm] = net_by_stock.get(nm, 0) + signed_exct
                for nm, amt in sorted(net_by_stock.items(), key=lambda kv: kv[1]):
                    setl_str += f"\n  {nm} {amt:+,}원"
        except Exception:
            pass

        # 예수금 상세 (kt00001) - D+1/D+2 추정예수금은 qry_tp="3"(추정조회)이어야 채워짐
        dep_str = ""
        try:
            dep = kiwoom_api.get_deposit_detail(qry_tp="3")
            entr       = int(dep.get("entr") or 0)
            pymn_alow  = int(dep.get("pymn_alow_amt") or 0)
            ord_alow   = int(dep.get("ord_alow_amt") or 0)
            d1_entr    = int(dep.get("d1_entra") or 0)
            d2_entr    = int(dep.get("d2_entra") or 0)
            dep_str = (
                f"\n예수금: <b>{entr:,}원</b>  출금가능: <b>{pymn_alow:,}원</b>"
                f"\n주문가능: <b>{ord_alow:,}원</b>"
                f"\nD+1 추정예수금: <b>{d1_entr:,}원</b>  D+2: <b>{d2_entr:,}원</b>"
            )
        except Exception:
            pass

        month_cum_str = ""
        if month_pl or cum_pl:
            month_cum_str = (
                f"\n당월 손익: <b>{month_pl:+,}원</b> ({month_rt:+.2f}%)"
                f"\n누적 손익: <b>{cum_pl:+,}원</b> ({cum_rt:+.2f}%)"
            )

        # 실현손익 (ka10074) - 15:30 장마감 시에만 포함
        rlzt_str = ""
        heatmap_str = ""
        if label == "장 마감":
            if indv:
                pairs = [(s["stk_nm"], float(s["prft_rt"])) for s in indv]
                heatmap_str = f"\n📊 <b>보유종목 히트맵</b>\n{_build_stock_heatmap_text(pairs)}"
            try:
                rlzt = kiwoom_api.get_daily_realized_pl()
                rlzt_rows = rlzt.get("daly_rlzt_pl_prps_array") or []
                rlzt_sum = int(rlzt.get("sum_sel_pl") or 0)
                rlzt_cmsn = int(rlzt.get("sum_cmsn") or 0)
                if rlzt_rows or rlzt_sum:
                    rlzt_lines = "\n".join(
                        f"  {r.get('stk_nm','')}: {int(r.get('sel_pl') or 0):+,}원 ({float(r.get('sel_pl_rt') or 0):+.2f}%)"
                        for r in rlzt_rows[:5]
                    )
                    rlzt_str = (
                        f"\n💹 실현손익 합계: <b>{rlzt_sum:+,}원</b>  수수료: {rlzt_cmsn:,}원"
                        + (f"\n{rlzt_lines}" if rlzt_lines else "")
                    )
            except Exception:
                pass

        return (
            f"📈 <b>[{label}] 잔고 요약</b> ({datetime.now().strftime('%m/%d %H:%M')})\n"
            f"총평가금액: <b>{total_asset:,}원</b>\n"
            f"당일 손익: <b>{day_pl:+,.0f}원</b> ({day_rt:+.2f}%)"
            f"{month_cum_str}"
            f"{cum_str}"
            f"{contract_str}"
            f"{setl_str}"
            f"{rlzt_str}"
            f"{dep_str}"
            f"{stock_lines}"
            f"{heatmap_str}"
        )
    except Exception as e:
        return f"⚠️ 잔고 요약 조회 실패: {e}"


def _build_performance_summary() -> str:
    """텔레그램 봇 응답용: 총평가금액 + 누계 수익률."""
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        history = _load_performance()
        base_dt = history.get("tracking_start_date", "")
        if not base_dt and history.get("baselines"):
            base_dt = history["baselines"][-1].get("date", "").replace("-", "")[:8]
        if not base_dt:
            return "⚠️ tracking_start_date 미설정"
        perf = kiwoom_api.get_period_eval(fr_dt=base_dt, to_dt=today_str)
        # kt00016의 tot_amt_to는 수수료/미수정리 미반영 단순합이라, 실제 순자산인
        # 추정예탁자산(kt00018)을 총평가금액으로 쓴다.
        try:
            total_asset = int(kiwoom_api.get_holdings().get("prsm_dpst_aset_amt") or 0)
        except Exception:
            total_asset = int(perf.get("tot_amt_to") or 0)
        base_asset  = int(perf.get("tot_amt_fr") or 0)
        invt_bsamt  = int(perf.get("invt_bsamt") or 0)
        tern_rt     = float(perf.get("tern_rt") or 0)
        # kt00016은 to_dt가 오늘이면 당일 입출금까지 이미 반영되어 있으므로 더 더하지 않는다.
        net_in = int(perf.get("termin_tot_trns") or 0) - int(perf.get("termin_tot_pymn") or 0)
        # evltv_prft/prft_rt는 kt00016 자체 tot_amt_to(단순합) 기준이라 total_asset을
        # 추정예탁자산으로 바꾼 뒤에는 안 맞는다 — 같은 기준으로 재계산한다.
        evltv_prft = total_asset - base_asset - net_in
        invested   = base_asset + net_in
        prft_rt    = (evltv_prft / invested * 100) if invested > 0 else 0.0
        try:
            tax_cmsn = kiwoom_api.get_net_deposit(base_dt, today_str).get("tax_cmsn", 0)
        except Exception:
            tax_cmsn = 0
        dep_str = ""
        try:
            dep = kiwoom_api.get_deposit_detail(qry_tp="3")
            ord_alow  = int(dep.get("ord_alow_amt") or 0)
            d1_entr   = int(dep.get("d1_entra") or 0)
            dep_str = f"\n주문가능: <b>{ord_alow:,}원</b>  D+1 예수금: <b>{d1_entr:,}원</b>"
        except Exception:
            pass
        return (
            f"💰 <b>성과 요약</b> ({datetime.now().strftime('%m/%d %H:%M')})\n"
            f"현재 총평가: <b>{total_asset:,}원</b>\n"
            f"총순입금: <b>{net_in:,}원</b>\n"
            f"수익금액: <b>{evltv_prft:+,}원</b> ({prft_rt:+.2f}%)\n"
            f"누적 수수료/세금: <b>{tax_cmsn:,}원</b>\n"
            f"투자원금평잔: <b>{invt_bsamt:,}원</b>  회전율: <b>{tern_rt:.2f}%</b>"
            f"{dep_str}"
        )
    except Exception as e:
        return f"⚠️ 성과 조회 실패: {e}"


def _build_holdings_summary() -> str:
    """텔레그램 봇 응답용: 보유 종목 목록."""
    try:
        h = kiwoom_api.get_holdings()
        stocks = h.get("acnt_evlt_remn_indv_tot", [])
        if not stocks:
            return "보유 종목이 없습니다."
        lines = [f"📋 <b>보유 종목</b> ({datetime.now().strftime('%m/%d %H:%M')})"]
        for s in stocks:
            name = s.get("stk_nm", "")
            qty = int(s.get("rmnd_qty") or 0)
            cur = int(s.get("cur_prc") or 0)
            pred = int(s.get("pred_close_pric") or 0)
            day_rt = ((cur - pred) / pred * 100) if pred > 0 else 0
            sign = "+" if day_rt >= 0 else ""
            lines.append(f"  {name}: {qty}주 {cur:,}원 ({sign}{day_rt:.2f}%)")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 보유종목 조회 실패: {e}"


def _handle_telegram_command(text: str) -> str | None:
    """봇 수신 메시지를 파싱해 응답 문자열 반환. 인식 못하면 None."""
    from datetime import timedelta
    t = text.strip()
    if t in ("잔고", "수익률", "성과", "요약"):
        return _build_performance_summary()
    if t in ("보유", "보유종목", "종목"):
        return _build_holdings_summary()
    if t in ("거래내역", "입출금", "거래"):
        try:
            today = datetime.now()
            strt  = (today - timedelta(days=30)).strftime("%Y%m%d")
            end   = today.strftime("%Y%m%d")
            nd = kiwoom_api.get_net_deposit(strt, end)
            return (
                f"💳 <b>최근 30일 거래 요약</b>\n"
                f"입금: <b>{nd['in_amt']:,}원</b>\n"
                f"출금: <b>{nd['out_amt']:,}원</b>\n"
                f"순입금: <b>{nd['net']:+,}원</b>\n"
                f"수수료/세금: <b>{nd['tax_cmsn']:,}원</b>"
            )
        except Exception as e:
            return f"⚠️ 거래내역 조회 실패: {e}"
    if t in ("실현손익", "매도손익"):
        try:
            data = kiwoom_api.get_daily_realized_pl()
            rows = data.get("daly_rlzt_pl_prps_array") or []
            total_pl = int(data.get("sum_sel_pl") or 0)
            total_cmsn = int(data.get("sum_cmsn") or 0)
            if not rows and total_pl == 0:
                return "📊 오늘 매도 실현손익이 없습니다."
            lines = [f"💹 <b>당일 실현손익</b> ({datetime.now().strftime('%m/%d')})\n합계: <b>{total_pl:+,}원</b>  수수료: {total_cmsn:,}원"]
            for r in rows[:10]:
                nm  = r.get("stk_nm", "")
                pl  = int(r.get("sel_pl") or 0)
                prt = float(r.get("sel_pl_rt") or 0)
                lines.append(f"  {nm}: {pl:+,}원 ({prt:+.2f}%)")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ 실현손익 조회 실패: {e}"
    if t in ("매매일지", "일지", "오늘거래"):
        try:
            data = kiwoom_api.get_today_trade_journal()
            rows = data.get("tdy_trde_jrnl_array") or []
            if not rows:
                return "📋 오늘 매매 내역이 없습니다."
            lines = [f"📋 <b>당일매매일지</b> ({datetime.now().strftime('%m/%d')})  {len(rows)}건"]
            for r in rows[:15]:
                tm  = r.get("cntr_tm", "")
                nm  = r.get("stk_nm", "")
                tp  = r.get("sell_tp_nm", "")
                qty = r.get("cntr_qty", "")
                prc = int(r.get("cntr_pric") or 0)
                pl  = int(r.get("rlzt_pl") or 0)
                pl_str = f" | 손익: {pl:+,}" if pl else ""
                t_fmt = f"{tm[:2]}:{tm[2:4]}" if len(tm) >= 4 else tm
                lines.append(f"  {t_fmt} {tp} {nm} {qty}주@{prc:,}{pl_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ 매매일지 조회 실패: {e}"
    if t in ("VI", "vi발동"):
        try:
            data = kiwoom_api.get_vi_stocks()
            items = data.get("items", [])
            rt = list(_vi_active)
            if not items and not rt:
                return "✅ 현재 VI 발동 종목 없음"
            lines = [f"⚡ <b>VI 발동 종목</b> ({datetime.now().strftime('%H:%M')})"]
            for it in items:
                lines.append(f"  {it['stk_nm']}({it['stk_cd']}) {it['vi_gubun']} @{it['vi_pric']}")
            if rt:
                lines.append(f"실시간 발동 코드: {', '.join(rt)}")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ VI 조회 실패: {e}"
    if t in ("도움말", "help", "?"):
        return (
            "📌 <b>사용 가능한 명령어</b>\n"
            "\n"
            "💰 <b>계좌 현황</b>\n"
            "  잔고 · 수익률 · 성과 → 누계 수익률·순입금·투자원금평잔·회전율\n"
            "  보유 · 보유종목 → 보유 종목별 현재가·당일 등락률\n"
            "\n"
            "📊 <b>거래 내역</b>\n"
            "  거래내역 · 입출금 → 최근 30일 입금·출금·순입금·수수료\n"
            "  실현손익 · 매도손익 → 당일 종목별 매도 실현손익 합계\n"
            "  매매일지 · 일지 → 당일 전체 체결 내역 (시간·종목·가격·손익)\n"
            "\n"
            "⚡ <b>시장 모니터링</b>\n"
            "  VI · vi발동 → 현재 VI 발동 중인 종목 목록\n"
            "               (VI 발동 종목은 매수 자동 차단됨)\n"
            "\n"
            "🚨 <b>긴급 매도 알림 (자동)</b>\n"
            "  보유 종목이 아래 조건에 해당하면 자동 알림 발송:\n"
            "  · 거래대금 일평균 5억 미만\n"
            "  · 관리종목 · 관리우려 지정\n"
            "  · 투자경고 · 투자위험 · 단기과열\n"
            "  · 거래정지\n"
            "  알림 메시지에 <b>매도</b> 회신 → 전량 매도 주문 즉시 실행\n"
            "  알림 메시지에 <b>아니</b> 회신 → 당일 재알림 없음\n"
            "  5분 내 미응답 시 자동 재발송\n"
            "\n"
            "📅 <b>자동 알림 (설정 시 자동 전송)</b>\n"
            "  07:00 → 새벽 데이터 업데이트 요약 (시도 내역·전략별 반영 결과·경고)\n"
            "  08:00 → 잔고 요약·포트폴리오 순위 점검·배당금 수령 여부\n"
            "  09~15시(매시) · 15:30 → 잔고 요약 (15:30엔 실현손익 포함)\n"
            "  월요일 08:00 → 직전 주 주간 자산 요약\n"
            "  매월 1일 08:00 → 전월 월간 자산 요약\n"
            "  체결 시 → 종목·체결가·수량·당일실현손익 즉시 알림\n"
            "  VI 발동/해제 시 → 보유 종목 실시간 알림\n"
            "  15:20 동시호가 시작 · 15:30 정규장 마감 시 → 장운영 상태 알림\n"
            "\n"
            "  도움말 · help · ? → 이 안내"
        )
    return None


def _telegram_polling():
    """텔레그램 getUpdates 롱폴링으로 봇 메시지 수신 및 응답."""
    try:
        from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except ImportError:
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{base_url}/getUpdates", params=params, timeout=40)
            updates = resp.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                    continue
                text = msg.get("text", "").strip()

                reply_to_id = (msg.get("reply_to_message") or {}).get("message_id")

                # ── 추가 투자 알림 회신 처리 ─────────────────────────────────
                if reply_to_id and reply_to_id in _pending_invest_orders:
                    info = _pending_invest_orders[reply_to_id]
                    if "즉시" in text:
                        info["mode"] = "immediate"
                        info["confirmed"] = True
                        del _pending_invest_orders[reply_to_id]
                        _send_telegram("✅ 즉시 주문을 진행합니다.")
                        threading.Thread(target=_execute_invest_plan,
                                         args=(info["plan"], "immediate"), daemon=True).start()
                    elif any(w in text for w in ("동시호가", "장마감", "15:20")):
                        info["mode"] = "auction"
                        info["confirmed"] = True
                        del _pending_invest_orders[reply_to_id]
                        _send_telegram("✅ 15:20 동시호가로 주문을 예약했습니다.")
                        # 스케줄러가 15:20에 실행 — confirmed 목록에 보존
                        _pending_invest_orders[f"auction_{reply_to_id}"] = {**info, "mode": "auction"}
                    elif any(w in text for w in ("취소", "아니", "no")):
                        del _pending_invest_orders[reply_to_id]
                        _send_telegram("✅ 이번 추가 투자를 취소했습니다.")
                    continue

                # ── 긴급 매도 알림에 대한 회신 처리 ──────────────────────────
                if reply_to_id and reply_to_id in _pending_sell_alerts:
                    info = _pending_sell_alerts[reply_to_id]
                    if "매도" in text:
                        try:
                            kiwoom_api.place_order(info["stk_cd"], info["qty"], "sell")
                            _send_telegram(
                                f"✅ <b>매도 주문 완료</b>\n"
                                f"{info['name']} ({info['stk_cd']}) {info['qty']:,}주"
                            )
                        except Exception as e:
                            _send_telegram(f"❌ 매도 주문 실패: {e}")
                        del _pending_sell_alerts[reply_to_id]
                    elif any(w in text for w in ("아니", "취소", "no", "NO")):
                        _dismissed_sell_alerts.add(info["stk_cd"])
                        del _pending_sell_alerts[reply_to_id]
                        _send_telegram(f"✅ {info['name']} 매도 취소\n(오늘 하루 재알림 없음)")
                    continue  # 회신 처리 완료 — 일반 명령어 파싱 스킵
                # ─────────────────────────────────────────────────────────────

                reply = _handle_telegram_command(text)
                if reply:
                    requests.post(f"{base_url}/sendMessage", json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": reply,
                        "parse_mode": "HTML",
                    }, timeout=10)
        except Exception:
            threading.Event().wait(5)


def _build_period_asset_summary(label: str, start_dt: str, end_dt: str) -> str:
    """kt00002로 기간 자산 변화 요약 메시지 생성."""
    try:
        hist = kiwoom_api.get_daily_asset_history(start_dt, end_dt)
        rows = hist.get("daly_prsm_dpst_aset_amt_prst") or []
        if not rows:
            return f"⚠️ [{label}] 기간 자산 데이터 없음"
        rows_sorted = sorted(rows, key=lambda r: r.get("dt", ""))
        first = rows_sorted[0]
        last  = rows_sorted[-1]
        start_amt = int(first.get("prsm_dpst_aset_amt") or 0)
        end_amt   = int(last.get("prsm_dpst_aset_amt") or 0)
        change    = end_amt - start_amt
        change_rt = (change / start_amt * 100) if start_amt > 0 else 0
        color_sign = "📈" if change >= 0 else "📉"
        # 기간 내 최고/최저
        amts = [int(r.get("prsm_dpst_aset_amt") or 0) for r in rows_sorted if r.get("prsm_dpst_aset_amt")]
        high_amt = max(amts) if amts else 0
        low_amt  = min(amts) if amts else 0
        # 기간 순입금/수수료 (kt00015)
        nd_str = ""
        try:
            nd = kiwoom_api.get_net_deposit(start_dt, end_dt)
            nd_str = (
                f"\n순입금: <b>{nd['net']:+,}원</b>"
                f" (입금 {nd['in_amt']:,} / 출금 {nd['out_amt']:,})"
                f"\n수수료/세금: <b>{nd['tax_cmsn']:,}원</b>"
            )
        except Exception:
            pass

        return (
            f"{color_sign} <b>[{label} 요약]</b> ({first['dt'][:4]}-{first['dt'][4:6]}-{first['dt'][6:]} ~ "
            f"{last['dt'][:4]}-{last['dt'][4:6]}-{last['dt'][6:]})\n"
            f"시작 자산: <b>{start_amt:,}원</b>\n"
            f"종료 자산: <b>{end_amt:,}원</b>\n"
            f"변화: <b>{change:+,}원</b> ({change_rt:+.2f}%)\n"
            f"기간 최고: {high_amt:,}원 / 최저: {low_amt:,}원"
            f"{nd_str}"
        )
    except Exception as e:
        return f"⚠️ [{label}] 자산 요약 실패: {e}"


def _daily_alert_scheduler():
    """평일 08:00/09:00/12:00/15:30 일별 요약, 월요일 주간 요약, 매월 1일 월간 요약 전송.
    장중 매 10분마다 보유 종목 긴급 매도 조건 점검 (관리/경고/정지).
    대기 중인 긴급 매도 알림은 5분 경과 시 자동 재발송.
    """
    from datetime import timedelta
    ALERT_TIMES = [
        ("08:00", "개장 준비"), ("15:30", "장 마감"),
    ]
    sent_today = set()
    last_condition_check = datetime.min  # 마지막 조건 점검 시각

    while True:
        now = datetime.now()
        key = now.strftime("%Y%m%d")
        hm  = now.strftime("%H:%M")

        # ── 07:00 — 새벽 데이터 업데이트 요약 (요일 무관, 배치가 도는 날은 매일 확인) ──
        slot_update = f"{key}_0700_update"
        if slot_update not in sent_today and hm == "07:00":
            _send_telegram(_build_update_summary_telegram())
            sent_today.add(slot_update)

        if now.weekday() < 5 and not _is_market_holiday(now):  # 평일이면서 휴장일이 아닐 때
            # ── 정해진 시각 요약 알림 ─────────────────────────────────────
            for t, label in ALERT_TIMES:
                slot = f"{key}_{t}"
                if slot not in sent_today and hm == t:
                    _send_telegram(_build_daily_summary(label))
                    if t == "15:30":
                        # 장 마감 시 미체결 주문 현황 포함 알림
                        try:
                            open_orders = kiwoom_api.get_open_orders()
                            rows = open_orders.get("acnt_ord_cntr_prps_dtl") or []
                            unfilled = [r for r in rows if int(r.get("ord_remnq") or 0) > 0]
                            if unfilled:
                                lines = "\n".join(
                                    f"  {r.get('stk_nm','')} {r.get('io_tp_nm','').replace('현금','').strip()}"
                                    f" {int(r.get('ord_remnq') or 0)}주 @ {int(r.get('ord_uv') or 0):,}원"
                                    for r in unfilled
                                )
                                _send_telegram(f"⚠️ <b>미체결 잔량 ({len(unfilled)}건)</b>\n{lines}\n\n동시호가 미체결 주문은 자동 취소됩니다.")
                        except Exception:
                            pass
                    if t == "08:00":
                        _send_telegram(_check_portfolio_ranks())
                        halt_msg = _check_upcoming_halts()
                        if halt_msg:
                            _send_telegram(halt_msg)
                        # 배당금 수령 여부 (kt00017)
                        try:
                            dep = kiwoom_api.get_today_deposit()
                            dvida = int(dep.get("dvida_amt") or 0)
                            if dvida > 0:
                                _send_telegram(f"💰 <b>배당금 수령</b>\n금액: <b>{dvida:,}원</b>")
                        except Exception:
                            pass
                        # 08:00 — 거래대금 5억 포함 전체 조건 점검
                        _check_holding_conditions(check_volume=True)
                        if now.weekday() == 0:
                            prev_mon = (now - timedelta(days=7)).strftime("%Y%m%d")
                            prev_fri = (now - timedelta(days=3)).strftime("%Y%m%d")
                            _send_telegram(_build_period_asset_summary("주간", prev_mon, prev_fri))
                        if now.day == 1:
                            first_this = now.replace(day=1)
                            last_prev  = first_this - timedelta(days=1)
                            first_prev = last_prev.replace(day=1)
                            _send_telegram(_build_period_asset_summary(
                                "월간",
                                first_prev.strftime("%Y%m%d"),
                                last_prev.strftime("%Y%m%d"),
                            ))
                    sent_today.add(slot)

            # ── 장중(09:00~15:30) 매 10분 조건 점검 ─────────────────────
            t_obj        = now.time()
            market_open  = datetime.strptime("09:00", "%H:%M").time()
            market_close = datetime.strptime("15:30", "%H:%M").time()
            t_1500       = datetime.strptime("15:00", "%H:%M").time()
            t_1520       = datetime.strptime("15:20", "%H:%M").time()

            elapsed = (now - last_condition_check).total_seconds()
            if elapsed >= 600:  # 10분마다
                _check_new_deposit()              # 입금 감지 (장외 포함)
                if market_open <= t_obj <= market_close:
                    _check_holding_conditions(check_volume=False)
                    _check_weight_deviation()      # 목표비중 이탈 감지
                last_condition_check = now

            # ── 15:00 — 무응답 추가 투자 알림 자동 동시호가 확정 ─────────
            slot_1500 = f"{key}_1500_invest"
            if t_obj >= t_1500 and slot_1500 not in sent_today:
                for mid, info in list(_pending_invest_orders.items()):
                    if not str(mid).startswith("auction_") and not info.get("confirmed"):
                        _send_telegram(
                            "⏰ 15:00 응답 없음 — 추가 투자를 <b>15:20 동시호가</b>로 자동 예약했습니다."
                        )
                        info["mode"]      = "auction"
                        info["confirmed"] = True
                        _pending_invest_orders[f"auction_{mid}"] = {**info}
                        del _pending_invest_orders[mid]
                sent_today.add(slot_1500)

            # ── 15:20 — 동시호가 예약 주문 실행 ─────────────────────────
            slot_1520 = f"{key}_1520_invest"
            if t_obj >= t_1520 and slot_1520 not in sent_today:
                auction_keys = [k for k in list(_pending_invest_orders) if str(k).startswith("auction_")]
                for k in auction_keys:
                    info = _pending_invest_orders.pop(k)
                    threading.Thread(target=_execute_invest_plan,
                                     args=(info["plan"], "auction"), daemon=True).start()
                if auction_keys:
                    sent_today.add(slot_1520)

        # ── 자정: 당일 상태 초기화 ────────────────────────────────────────
        if now.hour == 0 and now.minute == 0:
            sent_today.clear()
            _dismissed_sell_alerts.clear()
            last_condition_check = datetime.min

        # ── 수동 "가격 데이터 갱신" 완료 감지 (파일 기반 - order_server.py 재시작에도 안 끊김) ──
        # 새벽 3시 자동 배치는 정상적으로 빠르게 끝나는 게 보통이라 진행상황 알림이 필요 없고,
        # 이 마커는 수동 트리거(버튼) 때만 생기므로 자동 배치는 애초에 아래 로직을 안 탄다.
        if os.path.exists(MANUAL_UPDATE_MARKER_FILE):
            try:
                with open(MANUAL_UPDATE_MARKER_FILE, encoding="utf-8") as f:
                    marker = json.load(f)
                triggered_at = datetime.fromisoformat(marker["triggered_at"])
                if os.path.exists(UPDATE_SUMMARY_FILE):
                    summary_mtime = datetime.fromtimestamp(os.path.getmtime(UPDATE_SUMMARY_FILE))
                else:
                    summary_mtime = None
                if summary_mtime and summary_mtime > triggered_at:
                    _send_telegram(f"🔄 <b>수동 데이터 갱신 완료</b>\n\n{_build_update_summary_telegram(label='수동 데이터 갱신')}")
                    os.remove(MANUAL_UPDATE_MARKER_FILE)
                else:
                    # 아직 완료 전 - 30분 경과 시마다 진행상황만 짧게 보고
                    last_notified_str = marker.get("last_progress_notified_at")
                    last_notified = datetime.fromisoformat(last_notified_str) if last_notified_str else triggered_at
                    if (now - last_notified).total_seconds() >= 1800:
                        progress_line = "진행상황 파일 없음(아직 초기 단계이거나 확인 불가)"
                        if os.path.exists(UPDATE_PROGRESS_FILE):
                            try:
                                with open(UPDATE_PROGRESS_FILE, encoding="utf-8") as f:
                                    p = json.load(f)
                                stage_no = p.get("stage_no")
                                stage_total = p.get("stage_total")
                                stage_prefix = f"({stage_no}/{stage_total}단계) " if stage_no and stage_total else ""
                                progress_line = f"{stage_prefix}{p.get('stage', '진행 중')}: {p.get('done', 0)}/{p.get('total', 0)}"
                            except Exception:
                                pass
                        elapsed_min = int((now - triggered_at).total_seconds() / 60)
                        _send_telegram(
                            f"⏳ <b>수동 데이터 갱신 진행 중</b> (시작 후 {elapsed_min}분 경과)\n{progress_line}"
                        )
                        marker["last_progress_notified_at"] = now.isoformat()
                        with open(MANUAL_UPDATE_MARKER_FILE, "w", encoding="utf-8") as f:
                            json.dump(marker, f)
            except Exception:
                pass

        threading.Event().wait(30)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    # 서버 재시작 후 대기 중인 긴급 매도 알림 복원
    _pending_sell_alerts.update(_load_pending_alerts())
    threading.Thread(target=_daily_alert_scheduler, daemon=True).start()
    threading.Thread(target=_telegram_polling, daemon=True).start()

    def _on_fill(vals):
        side  = "매수" if vals.get("907") == "2" else "매도"
        name  = vals.get("302", "")
        qty   = vals.get("911", "")
        price = vals.get("910", "")
        total = vals.get("903", "")
        t     = vals.get("908", "")
        # 잔고(04) 캐시에서 당일실현손익 보완
        code = (vals.get("9001") or "").lstrip("A")
        bal  = kiwoom_api.realtime_balance.get(code, {})
        day_pl = bal.get("990", "")
        try:
            t_fmt     = f"{t[:2]}:{t[2:4]}:{t[4:]}" if len(t) >= 6 else t
            price_fmt = f"{int(price):,}" if price else price
            total_fmt = f"{int(total):,}" if total else total
            # 당일실현손익(990)은 매도로 실현된 손익 캐시라 매수 체결에는 붙이지 않는다.
            # (같은 종목을 오늘 먼저 팔았다가 다시 사면, 매수 체결에 아까 매도 손익이 잘못 따라붙는 것 방지)
            pl_line   = f"\n당일실현손익: <b>{int(day_pl):+,}원</b>" if (day_pl and side == "매도") else ""
        except Exception:
            t_fmt, price_fmt, total_fmt, pl_line = t, price, total, ""
        _send_telegram(
            f"✅ <b>주문 체결</b> ({t_fmt})\n"
            f"{side} | {name}\n"
            f"체결가: <b>{price_fmt}원</b> × {qty}주\n"
            f"누계금액: {total_fmt}원"
            f"{pl_line}"
        )

    # 0g 종목정보(370) 축약 코드 → 설명 (알려진 것만 매핑, 모르는 코드는 원문 그대로 표기)
    _STOCK_INFO_LABELS = {
        "관리": "관리종목",
        "정리": "정리매매",
        "불성실": "불성실공시법인",
        "환기": "시장경보(투자주의환기종목)",
        "투자유의": "투자유의종목",
        "투자경고": "투자경고종목",
        "투자위험": "투자위험종목",
        "단기과열": "단기과열종목",
        "거정": "거래정지",
        "정지": "거래정지",
    }

    def _describe_stock_info(raw: str) -> str:
        m = re.match(r"^증(\d+)$", raw)
        if m:
            return f"증거금 {m.group(1)}%"
        return _STOCK_INFO_LABELS.get(raw, raw)

    _last_stock_info: dict = {}  # {코드: 마지막으로 알린 370 값} — 동적VI 등 다른 필드 변화로
                                  # 0g가 재발동돼도 370 자체가 그대로면 중복 알림을 보내지 않는다

    def _on_stock_info(code, vals):
        info = vals.get("370", "")
        if not info:
            _last_stock_info.pop(code, None)
            return
        if _last_stock_info.get(code) == info:
            return  # 같은 내용이 이미 알림 발송됨 — VI 깜빡임 등으로 0g만 재발동된 경우
        _last_stock_info[code] = info
        stk_nm = kiwoom_api.stock_name_cache.get(code) or code
        desc = _describe_stock_info(info)
        _send_telegram(
            f"⚠️ <b>종목 조치 알림</b>\n"
            f"종목: {stk_nm} ({code})\n"
            f"내용: {desc}"
        )

    def _on_vi(code, vals):
        """1h: VI 발동/해제 실시간 이벤트 — 보유 종목만 텔레그램 알림."""
        vi_type = str(vals.get("215") or "")
        stk_nm  = vals.get("302") or code
        if vi_type == "1":
            _vi_active.add(code)
            _send_telegram(
                f"⚡ <b>VI 발동</b>\n"
                f"종목: {stk_nm} ({code})\n"
                f"⛔ 해당 종목 매수가 일시 차단됩니다."
            )
        elif vi_type == "2":
            _vi_active.discard(code)
            _send_telegram(f"✅ <b>VI 해제</b>\n종목: {stk_nm} ({code})")

    def _on_price_limit(code, kind, vals):
        """0B 전일대비기호(25) 상한가/하한가 진입·이탈 — 0B는 보유 종목에만 REG하므로 별도 필터 불필요."""
        stk_nm = kiwoom_api.stock_name_cache.get(code) or code
        if kind == "상한가":
            _send_telegram(f"🔺 <b>상한가 진입</b>\n종목: {stk_nm} ({code})\n💰 매도 타이밍을 검토해보세요.")
        elif kind == "하한가":
            _send_telegram(f"🔻 <b>하한가 진입</b>\n종목: {stk_nm} ({code})")
        else:
            _send_telegram(f"↩️ <b>상/하한가 이탈</b>\n종목: {stk_nm} ({code})")

    # 장운영구분(215) 실제 값 — 키움 0s 문서 기준 (기존 코드에 2/3이 뒤바뀌어 있었음)
    # 장 시작 전(0)/정규장 개장(3)/마감 확정(8)/시간외 종가매매·단일가(a,b,c,d)/전체 시장 종료(9)는
    # 일상적으로 계속 발생해 알림 가치가 낮아 제외 — 퀀트 매매에 직접 영향 있는 두 시점만 남긴다.
    _MARKET_LABELS = {
        "2": "🔔 동시호가 시작 (15:20~)",
        "4": "🔔 정규장 마감 (15:30)",
    }
    _last_market_op_tp = [None]  # 같은 상태가 반복 전송돼도 한 번만 알리기 위한 추적

    def _on_market_open(vals):
        """0s: 장운영 이벤트 — 상태가 실제로 바뀔 때만 텔레그램 알림."""
        op_tp = str(vals.get("215") or "")
        if op_tp == _last_market_op_tp[0]:
            return  # 15:20~15:30처럼 같은 상태가 반복 수신되는 구간 — 중복 알림 방지
        _last_market_op_tp[0] = op_tp
        label = _MARKET_LABELS.get(op_tp)
        if not label:
            return
        tm = vals.get("20", "")
        # 일부 이벤트는 체결시간 필드가 "999999"/"888888" 같은 더미값으로 오므로 그런 경우는 시각을 생략한다
        t_fmt = f" ({tm[:2]}:{tm[2:4]}:{tm[4:]})" if len(tm) >= 6 and tm not in ("999999", "888888") else ""
        _send_telegram(f"{label}{t_fmt}")

    _update_watched = kiwoom_api.start_order_realtime(
        on_fill=_on_fill,
        on_stock_info=_on_stock_info,
        on_vi=_on_vi,
        on_market_open=_on_market_open,
        # on_price_limit=_on_price_limit,  # 상한가/하한가 진입·이탈 알림 — 꺼둠
    )
    print(f"[시스템] 주문 서버 시작 (모의투자: {kiwoom_api.KIWOOM_IS_MOCK}) - http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
