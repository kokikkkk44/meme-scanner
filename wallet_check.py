"""ウォレット審査: 指定したウォレットの損益サマリーを Solana Tracker /pnl から取得して
data/wallet_check/ に CSV と生データを書き出す(手動実行用)"""
import csv, json, os, sys, time
import requests

ST = "https://data.solanatracker.io"
KEY = os.environ.get("SOLANATRACKER_API_KEY", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wallet_check")
os.makedirs(OUT, exist_ok=True)


def get(path):
    for i in range(3):
        try:
            r = requests.get(ST + path, headers={"x-api-key": KEY}, timeout=60)
            if r.status_code == 429:
                time.sleep(3); continue
            if r.status_code != 200:
                return {"_error": f"HTTP {r.status_code} {r.text[:150]}"}
            return r.json()
        except requests.RequestException as e:
            err = str(e); time.sleep(2)
    return {"_error": err}


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def main():
    pairs = [x.split(":", 1) for x in sys.argv[1].split(",") if ":" in x]
    if not KEY:
        print("SOLANATRACKER_API_KEY が未設定"); return
    rows, raw = [], {}
    for label, w in pairs:
        label, w = label.strip(), w.strip()
        d = get(f"/pnl/{w}?showHistoricPnL=true&hideDetails=true")
        if isinstance(d, dict):
            d.pop("tokens", None)
        raw[w] = {"label": label, "data": d}
        s = pick(d, "summary") or {}
        hist = (pick(d, "historic") or {}).get("summary", {}) if isinstance(pick(d, "historic"), dict) else {}
        row = {"ラベル": label, "ウォレット": w, "エラー": pick(d, "_error") or "",
               "確定損益": pick(s, "realized"), "含み損益": pick(s, "unrealized"), "合計損益": pick(s, "total"),
               "投入額": pick(s, "totalInvested"), "勝ち": pick(s, "totalWins"), "負け": pick(s, "totalLosses"),
               "勝率%": pick(s, "winPercentage"), "平均購入額": pick(s, "averageBuyAmount")}
        for p in ("1d", "7d", "30d"):
            h = hist.get(p) or {}
            row[f"{p}損益"] = pick(h, "total", "realizedChange", "totalChange", "value")
            row[f"{p}%"] = pick(h, "percentageChange", "pct")
        row["GMGN"] = f"https://gmgn.ai/sol/address/{w}"
        rows.append(row)
        print(label, w[:6], row["エラー"] or f'realized={row["確定損益"]} win%={row["勝率%"]}')
        time.sleep(0.6)
    json.dump(raw, open(os.path.join(OUT, "raw.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "wallets.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader(); wr.writerows(rows)


if __name__ == "__main__":
    main()
