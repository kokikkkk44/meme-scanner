"""取引した銘柄の「その後」を追跡する (10/20〜。銘柄探しのスキャナーの代わり)

data/tracked.json に入っている取引について、GeckoTerminal(無料・キー不要)の
ろうそく足から次を計算し、data/tracking_results.json と data/tracking_report.md に保存する。

- 買う前: 買う30分前・60分前からの値動き、買う前60分の最高値
- 売った後: 1時間後・4時間後・24時間後の値段(売値比・買値比)、24時間の最高値・最安値
- 24時間たって全部そろった取引は done にして、それ以上は問い合わせない

実際の売買は一切しない。APIキーも使わない。
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))
API = "https://api.geckoterminal.com/api/v2/networks/solana"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TRACKED = os.path.join(DATA, "tracked.json")
RESULTS = os.path.join(DATA, "tracking_results.json")
REPORT = os.path.join(DATA, "tracking_report.md")

CHECKPOINTS_H = [1, 4, 24]
REQ_INTERVAL = 2.3          # 無料枠は1分30回まで
MAX_TRADES_PER_RUN = 30     # 1回の実行で処理する取引の上限
DRY_RUN = os.environ.get("DRY_RUN", "").lower() == "true"

_last_req = [0.0]


def log(msg):
    print(f"[{datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def get(path, params=None):
    wait = REQ_INTERVAL - (time.time() - _last_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_req[0] = time.time()
    for attempt in range(3):
        try:
            r = requests.get(API + path, params=params, timeout=20,
                             headers={"Accept": "application/json;version=20230302"})
            if r.status_code == 429:
                time.sleep(20)
                continue
            if r.status_code != 200:
                log(f"  APIエラー {r.status_code}: {path}")
                return None
            return r.json()
        except requests.RequestException as e:
            log(f"  通信エラー: {e}")
            time.sleep(5)
    return None


def ts(iso):
    return int(datetime.fromisoformat(iso).timestamp())


def find_pool(address):
    j = get(f"/tokens/{address}/pools", {"page": 1})
    if not j or not j.get("data"):
        return None
    best = max(j["data"], key=lambda p: float(p["attributes"].get("reserve_in_usd") or 0))
    a = best["attributes"]
    price = float(a.get("base_token_price_usd") or 0)
    fdv = float(a.get("fdv_usd") or 0)
    base_id = (best.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id", "")
    supply = fdv / price if price > 0 and fdv > 0 else None
    return {"pool": a["address"], "supply": supply, "base_is_token": base_id.endswith(address)}


def candles(pool, address, agg_min, before, limit):
    """古い順の [ts, o, h, l, c] を返す"""
    tf, agg = ("minute", agg_min) if agg_min < 60 else ("hour", agg_min // 60)
    j = get(f"/pools/{pool}/ohlcv/{tf}", {"aggregate": agg, "before_timestamp": before,
                                          "limit": limit, "currency": "usd", "token": address})
    if not j:
        return []
    rows = (((j.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    return sorted([r[:5] for r in rows], key=lambda r: r[0])


def price_at(cs, t):
    """時刻 t を含む(なければ直前の)ろうそく足の終値"""
    best = None
    for c in cs:
        if c[0] <= t:
            best = c
        else:
            break
    return best[4] if best else None


def pct(a, b):
    return round((a / b - 1) * 100, 1) if a and b else None


def process(tr, res, pools, now):
    addr = tr.get("address")
    if not addr:
        res["note"] = "CAなし"
        res["done"] = True
        return
    buy_t, sell_t = ts(tr["buy"]), ts(tr["sell"])

    if addr not in pools:
        p = find_pool(addr)
        if not p:
            res["note"] = "GeckoTerminalにプールが見つからない"
            res["tries"] = res.get("tries", 0) + 1
            if res["tries"] >= 3:
                res["done"] = True
            return
        pools[addr] = p
    pool = pools[addr]["pool"]
    supply = pools[addr].get("supply")
    mc = (lambda p: round(p * supply / 1000, 1) if p and supply else None)

    # 基準の値段: 日誌の時価総額(GMGNの画面の値)があればそれを使う。
    # 15分足の終値だと、急落した足では実際の売値とずれるため。
    def ref(mc_k):
        return mc_k * 1000 / supply if mc_k and supply else None

    # 買う前(1回だけ)。5分足が取れなければ15分足で代用
    if "pre" not in res or res["pre"].get("note"):
        cs = candles(pool, addr, 5, buy_t + 300, 40) or candles(pool, addr, 15, buy_t + 900, 12)
        if cs:
            pb_c = price_at(cs, buy_t)
            pb0 = ref(tr.get("mcIn")) or pb_c
            p30, p60 = price_at(cs, buy_t - 1800), price_at(cs, buy_t - 3600)
            hi60 = max([c[2] for c in cs if buy_t - 3600 <= c[0] <= buy_t] or [0]) or None
            res["pre"] = {"buyPrice": pb0, "chg30m": pct(pb_c, p30), "chg60m": pct(pb_c, p60),
                          "fromHigh60m": pct(pb_c, hi60), "mcBuyEst": mc(pb_c)}
        else:
            res["pre"] = {"buyPrice": ref(tr.get("mcIn")), "note": "買う前のデータなし"}

    # 売った後
    end = min(now, sell_t + 24 * 3600)
    if end <= sell_t + 3600:
        return  # まだ1時間たっていない
    cs = candles(pool, addr, 15, end + 900, 110)
    if not cs:
        res["note"] = "売った後のデータなし"
        if now > sell_t + 26 * 3600:
            res["done"] = True
        return
    pb = (res.get("pre") or {}).get("buyPrice") or ref(tr.get("mcIn"))
    ps = ref(tr.get("mcOut"))
    if not ps and pb and tr.get("pnlPct") is not None:
        ps = pb * (1 + tr["pnlPct"] / 100)
    ps = ps or price_at(cs, sell_t)
    post = res.setdefault("post", {})
    post["sellPrice"] = ps
    for h in CHECKPOINTS_H:
        t = sell_t + h * 3600
        if t <= now:
            p = price_at(cs, t)
            post[f"{h}h"] = {"vsSell": pct(p, ps), "vsBuy": pct(p, pb), "mcEst": mc(p)}
    win = [c for c in cs if sell_t <= c[0] <= sell_t + 24 * 3600]
    if win:
        hi, lo = max(c[2] for c in win), min(c[3] for c in win)
        post["max24h"] = {"vsSell": pct(hi, ps), "vsBuy": pct(hi, pb), "mcEst": mc(hi)}
        post["min24h"] = {"vsSell": pct(lo, ps), "vsBuy": pct(lo, pb), "mcEst": mc(lo)}
        # 買値から見て、先に+25%と−20%のどちらに届いたか(売った後から24時間)
        if pb:
            first = None
            for c in win:
                if c[3] <= pb * 0.8:
                    first = "−20%(損切り)"
                    break
                if c[2] >= pb * 1.25:
                    first = "+25%(利確)"
                    break
            post["firstHitAfterSell"] = first or "どちらにも届かず"
    if now >= sell_t + 24 * 3600:
        res["done"] = True
    res["updated"] = datetime.now(JST).isoformat(timespec="minutes")


def fmt(v):
    return "—" if v is None else (f"{v:+.1f}%" if isinstance(v, (int, float)) else str(v))


def write_report(tracked, results):
    lines = ["# 取引した銘柄のその後", "",
             "売値比 = 売った値段から何%動いたか(プラスなら、もっと持っていれば儲かった)",
             "買値比 = 買った値段から見て何%か(24時間ずっと持っていたらどうだったか)", "",
             "| 日付 | 銘柄 | 実際の損益 | 1時間後(売値比) | 4時間後(売値比) | 24時間後(売値比/買値比) | 24時間の最高値(売値比) | 買う前30分の値動き |",
             "|---|---|---|---|---|---|---|---|"]
    for tr in sorted(tracked, key=lambda t: t["sell"], reverse=True):
        r = results.get(tr["id"], {})
        post, pre = r.get("post", {}), r.get("pre", {})
        h24 = post.get("24h", {})
        lines.append("| {} | {} | {} | {} | {} | {} / {} | {} | {} |".format(
            tr["sell"][:10], tr.get("token", "?"), fmt(tr.get("pnlPct")),
            fmt(post.get("1h", {}).get("vsSell")), fmt(post.get("4h", {}).get("vsSell")),
            fmt(h24.get("vsSell")), fmt(h24.get("vsBuy")),
            fmt(post.get("max24h", {}).get("vsSell")), fmt(pre.get("chg30m"))))
    lines.append("")
    lines.append(f"更新: {datetime.now(JST):%Y-%m-%d %H:%M} (日本時間)")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    tracked = load(TRACKED, {"trades": []}).get("trades", [])
    results = load(RESULTS, {})
    pools = results.pop("_pools", {})
    now = int(time.time())
    todo = [t for t in tracked if not results.get(t["id"], {}).get("done")]
    log(f"追跡中 {len(todo)} 件 / 全 {len(tracked)} 件")
    for tr in todo[:MAX_TRADES_PER_RUN]:
        res = results.setdefault(tr["id"], {})
        log(f"- {tr.get('token')} ({tr['id']})")
        try:
            process(tr, res, pools, now)
        except Exception as e:  # 1件の失敗で全体を止めない
            log(f"  エラー: {e}")
        log(f"  → {json.dumps(res, ensure_ascii=False)[:300]}")
    results["_pools"] = pools
    if DRY_RUN:
        log("テスト実行のため保存しません")
        return
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    write_report(tracked, {k: v for k, v in results.items() if not k.startswith("_")})


if __name__ == "__main__":
    main()
