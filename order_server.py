"""주문 실행 메뉴용 로컬 전용 서버.

키움 실거래 키는 이 서버(내 PC) 안에서만 사용되고 브라우저로는 절대 전달되지 않습니다.
배포된(GitHub Pages) index.html에서는 이 서버에 접근할 수 없으므로 주문 실행 메뉴는
이 서버를 직접 실행한 로컬 환경에서만 동작합니다.

사용법: python order_server.py  (5050 포트로 실행, index.html을 열어둔 채로 켜두면 됩니다)
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS

import kiwoom_api

app = Flask(__name__)
CORS(app)  # 로컬 전용 서버이므로 file:// 출처(브라우저)도 허용

# 성과추적 데이터 (기준점/스냅샷) - 이 PC 안에만 저장, 깃허브에는 올리지 않음
PERFORMANCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "performance_history.json")
BENCHMARK_CODES = {"kospi": "069500", "kosdaq": "229200"}  # KODEX 200 / KODEX 코스닥150 (지수 추종 ETF로 근사)


def _load_performance():
    if os.path.exists(PERFORMANCE_FILE):
        with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"baselines": []}


def _save_performance(data):
    with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 가격(KRX 시세) 갱신 작업 상태. DART는 캐시를 그대로 쓰므로 보통 수 분 내 끝남.
update_state = {"running": False, "log": "", "done": False, "success": None}


def _run_update_data():
    update_state.update(running=True, log="", done=False, success=None)
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
        return jsonify(kiwoom_api.get_holdings())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order_status", methods=["GET"])
def order_status():
    try:
        return jsonify(kiwoom_api.get_order_status())
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


@app.route("/api/order", methods=["POST"])
def order():
    data = request.get_json(force=True) or {}
    stk_cd = data.get("stk_cd")
    qty = data.get("qty")
    side = data.get("side")
    price = data.get("price")
    if not stk_cd or not qty or side not in ("buy", "sell"):
        return jsonify({"error": "stk_cd, qty, side(buy/sell)는 필수입니다."}), 400
    try:
        result = kiwoom_api.place_order(stk_cd, qty, side, price)
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
    """② 주기적 스냅샷 저장 - 최신 기준점 종목들의 현재가/평가금액/벤치마크를 기록 (수동 버튼 트리거)."""
    history = _load_performance()
    if not history["baselines"]:
        return jsonify({"error": "기준점이 없습니다. 먼저 기준점을 기록해주세요."}), 400
    baseline = history["baselines"][-1]
    try:
        codes = [s["code"] for s in baseline["stocks"]] + list(BENCHMARK_CODES.values())
        quotes = kiwoom_api.get_stock_quotes(codes)

        total_value = 0
        stock_values = []
        for s in baseline["stocks"]:
            price = quotes.get(s["code"])
            value = (price * s["qty"]) if price else None
            if value:
                total_value += value
            stock_values.append({"code": s["code"], "name": s["name"], "price": price, "value": value})

        snapshot = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "totalValue": total_value,
            "stocks": stock_values,
            "benchmark": {k: quotes.get(v) for k, v in BENCHMARK_CODES.items()},
        }
        baseline["snapshots"].append(snapshot)
        _save_performance(history)
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance/history", methods=["GET"])
def performance_history():
    return jsonify(_load_performance())


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"[시스템] 주문 서버 시작 (모의투자: {kiwoom_api.KIWOOM_IS_MOCK}) - http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
