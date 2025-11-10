from flask import Flask, jsonify, request
import os
import math
from binance.um_futures import UMFutures
from binance.error import ClientError

app = Flask(__name__)

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
DEFAULT_LEVERAGE = int(os.environ.get("DEFAULT_LEVERAGE", "1"))

client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "Cloud Run Flask server is working with Binance!"})

@app.route("/futures/balance")
def balance():
    try:
        balances = client.balance()
        usdt = next((float(b["availableBalance"]) for b in balances if b["asset"] == "USDT"), 0.0)
        return jsonify({"ok": True, "usdt_available": usdt})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
