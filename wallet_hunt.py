"""ウォレット探し: 指定した銘柄を「早く買った人」「一番儲けた人」を Solana Tracker から取得し、
複数の銘柄に共通して出てくるウォレットを data/wallet_hunt/ に CSV で書き出す(手動実行用)"""
import csv, json, os, sys, time
import requests

ST = "https://data.solanatracker.io"
KEY = os.environ.get("SOLANATRACKER_API_KEY", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wallet_hunt")
os.makedirs(OUT, exist_ok=True)


def get(path):
    for i in range(3):
        try:
            r = requests.get(ST + path, headers={"x-api-key": KEY}, timeout=20)
            if r.status_code == 429:
                time.sleep(3); continue
            if r.status_code != 200:
                print(f"  {path}: HTTP {r.status_code} {r.text[:150]}")
                return None
            return r.json()
        except requests.RequestException as e:
            print(f"  {path}: {e}"); time.sleep(2)
    return None


def as_list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "traders", "buyers", "items", "wallets"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    pairs = [x.split(":", 1) for x in sys.argv[1].split(",") if x.strip()]
    if not KEY:
        print("SOLANATRACKER_API_KEY が未設定"); return
    agg = {}
    for label, mint in pairs:
        label, mint = label.strip(), mint.strip()
        print(f"== {label} {mint}")
        for kind, path in (("early", f"/first-buyers/{mint}"), ("top", f"/top-traders/{mint}")):
            d = get(path)
            json.dump(d, open(os.path.join(OUT, f"raw_{label}_{kind}.json"), "w"), ensure_ascii=False)
            rows = as_list(d)
            print(f"  {kind}: {len(rows)}件")
            for rank, it in enumerate(rows, 1):
                if not isinstance(it, dict):
                    continue
                w = it.get("wallet") or it.get("owner") or it.get("address")
                if not w:
                    continue
                a = agg.setdefault(w, {"tokens": {}, "realized": 0.0, "total": 0.0})
                t = a["tokens"].setdefault(label, set())
                t.add(f"{kind}{rank}")
                r = num(it.get("realized"))
                tt = num(it.get("total"))
                if kind == "top" or label not in a.get("_counted", set()):
                    a["realized"] += r or 0
                    a["total"] += tt or 0
                    a.setdefault("_counted", set()).add(label)
            time.sleep(0.5)
    rows = []
    for w, a in agg.items():
        n = len(a["tokens"])
        rows.append({"ウォレット": w, "出現銘柄数": n,
                     "銘柄(順位)": " / ".join(f"{k}:{','.join(sorted(v))}" for k, v in sorted(a["tokens"].items())),
                     "確定損益USD(合計)": round(a["realized"], 2), "損益USD(合計)": round(a["total"], 2),
                     "GMGN": f"https://gmgn.ai/sol/address/{w}"})
    rows.sort(key=lambda r: (-r["出現銘柄数"], -r["確定損益USD(合計)"]))
    with open(os.path.join(OUT, "wallets.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["ウォレット"])
        wr.writeheader(); wr.writerows(rows)
    print(f"ウォレット {len(rows)} 件 / 2銘柄以上: {sum(1 for r in rows if r['出現銘柄数'] >= 2)} 件")


if __name__ == "__main__":
    main()
