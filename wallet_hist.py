"""コピートレードの過去検証用: 候補ウォレットの売買履歴を Solana 公開ノード(無料・キー不要)から集める(手動実行)。
  python wallet_hist.py [1ウォレットあたりの取引上限] [開始日 YYYY-MM-DD]
候補 = data/wallet_hunt/wallets.csv(2銘柄以上に出現) + data/wallet_check/wallets.csv
出力 = data/copy_bt/wallets/<wallet>.json : {"bot": 高頻度か, "tx": [[時刻, 銘柄, トークン増減, SOL増減], ...]}
1日に数百回以上売買する高頻度bot(人が真似できない)は、取引を取らずに "bot": true だけ記録する"""
import csv, datetime as dt, json, os, sys, time
import requests

RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "data", "copy_bt", "wallets")
WSOL = "So11111111111111111111111111111111111111112"
os.makedirs(OUT, exist_ok=True)
S = requests.Session()
last = [0.0]


def rpc(method, params):
    for i in range(6):
        wait = 0.3 - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.time()
        try:
            r = S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 3 * i); continue
            j = r.json()
            if "error" in j:
                if j["error"].get("code") in (-32429, 429, -32005):
                    time.sleep(2 + 3 * i); continue
                print("  ", method, j["error"]); return None
            return j.get("result")
        except (requests.RequestException, ValueError) as e:
            print("  ", method, e); time.sleep(2)
    return None


def candidates():
    out = []
    p = os.path.join(BASE, "data", "wallet_hunt", "wallets.csv")
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        if int(r["出現銘柄数"] or 0) >= 2:
            out.append(r["ウォレット"])
    p = os.path.join(BASE, "data", "wallet_check", "wallets.csv")
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        if r.get("ウォレット"):
            out.append(r["ウォレット"])
    return list(dict.fromkeys(out))


def parse(tx, w):
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    if w not in keys:
        return None
    i = keys.index(w)
    sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    tok = {}
    for sign, arr in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") != w:
                continue
            a = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            tok[b["mint"]] = tok.get(b["mint"], 0) + sign * a
    sol += tok.pop(WSOL, 0)
    tok = {m: d for m, d in tok.items() if abs(d) > 0}
    if len(tok) != 1:
        return None
    m, d = next(iter(tok.items()))
    if (d > 0 and sol >= 0) or (d < 0 and sol <= 0):
        return None
    return [tx.get("blockTime"), m, d, round(sol, 6)]


def main():
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    since = dt.datetime.strptime(sys.argv[2] if len(sys.argv) > 2 else "2026-10-01", "%Y-%m-%d").replace(
        tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()
    t_end = time.time() + 60 * float(os.environ.get("MAX_MINUTES", "300"))
    for n, w in enumerate(candidates(), 1):
        p = os.path.join(OUT, w + ".json")
        if os.path.exists(p):
            continue
        if time.time() > t_end:
            print("時間切れ(次回の実行で続きから)"); break
        sigs, before = [], None
        while len(sigs) < 3000:
            opt = {"limit": 1000}
            if before:
                opt["before"] = before
            page = rpc("getSignaturesForAddress", [w, opt])
            if not page:
                break
            sigs += page
            before = page[-1]["signature"]
            if (page[-1].get("blockTime") or 0) < since or len(page) < 1000:
                break
        if not sigs:
            json.dump({"bot": None, "tx": []}, open(p, "w")); continue
        span = (sigs[0].get("blockTime") or 0) - (sigs[-1].get("blockTime") or 0)
        per_day = len(sigs) / max(span / 86400, 0.01)
        sigs = [s for s in sigs if (s.get("blockTime") or 0) >= since and not s.get("err")]
        if per_day > 300:
            json.dump({"bot": True, "per_day": round(per_day), "tx": []}, open(p, "w"))
            print(f"[{n}] {w[:6]} 高頻度bot ({per_day:.0f}回/日) → 対象外"); continue
        rows = []
        for s in sigs[:cap]:
            tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if tx:
                r = parse(tx, w)
                if r:
                    rows.append(r)
        json.dump({"bot": False, "per_day": round(per_day, 1), "n_sigs": len(sigs), "tx": rows}, open(p, "w"),
                  separators=(",", ":"))
        print(f"[{n}] {w[:6]} {per_day:.0f}回/日 署名{len(sigs)} 売買{len(rows)}")


if __name__ == "__main__":
    main()
