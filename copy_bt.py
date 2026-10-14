"""コピートレードの過去検証用データ集め(手動実行)。
  python copy_bt.py fb [上限]     : data/copy_bt/coins.csv の各銘柄の「早く買った人」(Solana Tracker /first-buyers)を保存
  python copy_bt.py chart [上限]  : data/copy_bt/chart_req.csv (mint,from_ts,to_ts) の1分足を保存
使った回数は data/copy_bt/usage.json に記録し、スキャナーの月間上限の計算に含める(無料枠を超えないため)"""
import csv, datetime as dt, json, os, sys, time
import requests

ST = "https://data.solanatracker.io"
KEY = os.environ.get("SOLANATRACKER_API_KEY", "").strip()
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "copy_bt")
USAGE = os.path.join(BASE, "usage.json")
JST = dt.timezone(dt.timedelta(hours=9))


def load_usage():
    month = dt.datetime.now(JST).strftime("%Y-%m")
    try:
        u = json.load(open(USAGE))
    except (OSError, ValueError):
        u = {}
    if u.get("month") != month:
        u = {"month": month, "count": 0}
    return u


def get(path, u, params=None):
    for i in range(3):
        try:
            u["count"] += 1
            r = requests.get(ST + path, headers={"x-api-key": KEY}, params=params, timeout=25)
            if r.status_code == 429:
                time.sleep(5); continue
            if r.status_code != 200:
                print(f"  {path}: HTTP {r.status_code} {r.text[:120]}")
                u.setdefault("errors", []).append(f"{path[:40]} HTTP {r.status_code} {r.text[:160]}")
                u["errors"] = u["errors"][-10:]
                return None
            return r.json()
        except requests.RequestException as e:
            print(f"  {path}: {e}"); time.sleep(2)
    return None


def as_list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "traders", "buyers", "items", "oclhv", "ohlcv"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def slim(it):
    fb = it.get("first_buy") or {}
    keep = ("wallet", "first_buy_time", "first_sell_time", "last_transaction_time", "realized", "unrealized",
            "total", "total_invested", "buy_transactions", "sell_transactions", "holding")
    o = {k: it.get(k) for k in keep}
    o["fb_amount"], o["fb_usd"] = fb.get("amount"), fb.get("volume_usd")
    return o


def main():
    mode = sys.argv[1]
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    if not KEY:
        print("SOLANATRACKER_API_KEY が未設定"); return
    u = load_usage()
    start = u["count"]
    if mode == "fb":
        out = os.path.join(BASE, "fb"); os.makedirs(out, exist_ok=True)
        for row in csv.DictReader(open(os.path.join(BASE, "coins.csv"))):
            m = row["mint"]; p = os.path.join(out, m + ".json")
            if os.path.exists(p):
                continue
            if u["count"] - start >= cap:
                break
            d = get(f"/first-buyers/{m}", u)
            if d is None:
                continue
            if not as_list(d):
                u.setdefault("errors", []).append(f"empty {m[:8]} {json.dumps(d)[:200]}")
            json.dump([slim(x) for x in as_list(d) if isinstance(x, dict)], open(p, "w"), separators=(",", ":"))
            time.sleep(1.1)
    elif mode == "chart":
        out = os.path.join(BASE, "chart"); os.makedirs(out, exist_ok=True)
        for row in csv.DictReader(open(os.path.join(BASE, "chart_req.csv"))):
            m = row["mint"]; p = os.path.join(out, m + ".json")
            if os.path.exists(p):
                continue
            if u["count"] - start >= cap:
                break
            d = get(f"/chart/{m}", u, params={"type": "1m", "time_from": row["from_ts"], "time_to": row["to_ts"]})
            if d is None:
                continue
            json.dump(as_list(d), open(p, "w"), separators=(",", ":"))
            time.sleep(1.1)
    json.dump(u, open(USAGE, "w"))
    print(f"使った回数 {u['count'] - start}(今月合計 {u['count']})")


if __name__ == "__main__":
    main()
