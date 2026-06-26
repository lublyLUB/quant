"""주문 실행 메뉴용 로컬 전용 서버.

키움 실거래 키는 이 서버(내 PC) 안에서만 사용되고 브라우저로는 절대 전달되지 않습니다.
배포된(GitHub Pages) index.html에서는 이 서버에 접근할 수 없으므로 주문 실행 메뉴는
이 서버를 직접 실행한 로컬 환경에서만 동작합니다.

사용법: python order_server.py  (5050 포트로 실행, index.html을 열어둔 채로 켜두면 됩니다)
"""

import sys

from flask import Flask, jsonify, request
from flask_cors import CORS

import kiwoom_api

app = Flask(__name__)
CORS(app)  # 로컬 전용 서버이므로 file:// 출처(브라우저)도 허용


@app.route("/api/holdings", methods=["GET"])
def holdings():
    try:
        return jsonify(kiwoom_api.get_holdings())
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


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"[시스템] 주문 서버 시작 (모의투자: {kiwoom_api.KIWOOM_IS_MOCK}) - http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
