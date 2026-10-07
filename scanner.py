#!/usr/bin/env python3
"""
Solana ミームコイン候補スキャナー(読み取り専用)

- 候補を集める : DEX Screener / Solana Tracker(pump.fun 卒業前)
- 審査する     : RugCheck / Solana Tracker(バンドル・スナイパー・開発者保有) / DEX Screener orders
- 5段階で判定  : 除外 / 見送り / 候補(リスク小・中・大)
- 通知する     : Slack(候補は毎回、見送りは1日1回まとめ)
- 記録する     : data/ フォルダの CSV

売買の機能は一切ありません。ウォレットの秘密鍵も使いません。

使い方:
  python scanner.py              通常実行
  python scanner.py --dry-run    Slack に送らず、ファイルも保存しない(画面に表示するだけ)
  python scanner.py --test-slack Slack にテストメッセージを1通送る
  python scanner.py --report     段階(小/中/大)ごとの成績を表示
"""
import argparse
import calendar
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter
from zoneinfo import ZoneInfo

import requests
import yaml
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATE_PATH = os.path.join(DATA, "state.json")
CANDIDATES_CSV = os.path.join(DATA, "candidates.csv")
JST = ZoneInfo("Asia/Tokyo")

DS = "https://api.dexscreener.com"
RC = "https://api.rugcheck.xyz/v1"
ST = "https://data.solanatracker.io"

TIER_STYLE = {
    "小": {"emoji": "🟢", "color": "#2EB67D"},
    "中": {"emoji": "🟡", "color": "#ECB22E"},
    "大": {"emoji": "🔴", "color": "#E01E5A"},
    "🎯": {"emoji": "🎯", "color": "#7C3AED"},   # 勝ちパターン(急騰→急落→横ばい)の通知・スキャナー枠
    "🎯生": {"emoji": "🎯", "color": "#0EA5E9"},  # 生まれたて枠
    "🎯隙": {"emoji": "🎯", "color": "#F97316"},  # すき間枠
    "📉": {"emoji": "📉", "color": "#64748B"},    # 高値から-40%以上の急落(5分足で横ばいを確認する候補)
}
SOL_MINT = "So11111111111111111111111111111111111111112"
PUMPFUN_MARKETS = {"pumpfun", "pump-fun", "pumpfun-bonding", "pump.fun"}
NO_LP_MINTS = {None, "", "11111111111111111111111111111111"}
API_NAMES = {"api.dexscreener.com": "DEX Screener", "api.rugcheck.xyz": "RugCheck", "data.solanatracker.io": "Solana Tracker"}

START = time.time()


# ---------------------------------------------------------------------------
# 小さな道具
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{dt.datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def now_ts():
    return time.time()


def jst(ts=None):
    return dt.datetime.fromtimestamp(ts or now_ts(), JST).strftime("%Y-%m-%d %H:%M")


def fnum(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def band_points(value, bands):
    """bands = [{min, max, points}] 上から順に、min <= value <= max の最初のものを返す"""
    if value is None:
        return 0, None
    for b in bands or []:
        if b["min"] <= value <= b["max"]:
            return b["points"], b
    return 0, None


def fmt_usd(v):
    if v is None:
        return "不明"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def fmt_pct(v, sign=False):
    if v is None:
        return "不明"
    return f"{v:+.1f}%" if sign else f"{v:.1f}%"


def fmt_age(minutes):
    if minutes is None:
        return "不明"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes}分"
    if minutes % 60 == 0 and minutes < 60 * 48:
        return f"{minutes // 60}時間"
    if minutes < 60 * 48:
        return f"{minutes // 60}時間{minutes % 60}分"
    return f"{minutes // 1440}日"


def fmt_price(v):
    if v is None:
        return ""
    return f"{v:.12g}"


def time_left():
    return CFG["runtime"]["max_runtime_sec"] - (time.time() - START)


# ---------------------------------------------------------------------------
# 通信
# ---------------------------------------------------------------------------
class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "meme-scanner/1.0 (read-only)"
        self.last = {}
        self.counts = Counter()

    def call(self, url, method="GET", params=None, body=None, headers=None, min_interval=0.0, retries=2):
        host = url.split("/")[2]
        for attempt in range(retries + 1):
            wait = self.last.get(host, 0) + min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                self.counts[API_NAMES.get(host, host)] += 1
                r = self.s.request(method, url, params=params, json=body, headers=headers, timeout=20)
                self.last[host] = time.time()
                if r.status_code == 429:
                    log(f"  混雑のため待機({host})")
                    time.sleep(4 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(2)
                    continue
                if r.status_code != 200:
                    log(f"  HTTP {r.status_code}: {url[:120]}")
                    return None
                return r.json()
            except (requests.RequestException, ValueError) as e:
                log(f"  通信エラー {host}: {e}")
                time.sleep(2)
        return None


def as_list(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "items", "results", "pairs"):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return []


# ---------------------------------------------------------------------------
# DEX Screener
# ---------------------------------------------------------------------------
def ds_collect(http):
    """戻り値: (mint -> 検出元, mint -> 説明文)"""
    c = CFG["sources"]["dexscreener"]
    found, descs = {}, {}
    endpoints = []
    if c.get("token_profiles"):
        endpoints.append(("/token-profiles/latest/v1", "DEX新着"))
    if c.get("community_takeovers"):
        endpoints.append(("/community-takeovers/latest/v1", "DEX CTO"))
    if c.get("token_boosts"):
        endpoints.append(("/token-boosts/latest/v1", "DEXブースト"))
    for ep, label in endpoints:
        for it in as_list(http.call(DS + ep, min_interval=1.1)):
            if isinstance(it, dict) and it.get("chainId") == "solana" and it.get("tokenAddress"):
                found.setdefault(it["tokenAddress"], label)
                if it.get("description"):
                    descs.setdefault(it["tokenAddress"], str(it["description"])[:300])
    max_age_ms = c.get("search_max_age_hours", 24) * 3600 * 1000
    for q in c.get("search_queries") or []:
        data = http.call(DS + "/latest/dex/search", params={"q": q}, min_interval=1.1)
        for p in as_list(data):
            if p.get("chainId") != "solana":
                continue
            created = p.get("pairCreatedAt") or 0
            if created and now_ts() * 1000 - created <= max_age_ms:
                addr = (p.get("baseToken") or {}).get("address")
                if addr:
                    found.setdefault(addr, f"DEX検索:{q}")
    return found, descs


def ds_pairs(http, mints):
    """mint -> 一番流動性の大きい Solana ペア"""
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = http.call(f"{DS}/tokens/v1/solana/{','.join(chunk)}", min_interval=0.25)
        for p in as_list(data):
            if p.get("chainId") != "solana":
                continue
            addr = (p.get("baseToken") or {}).get("address")
            if addr not in chunk:
                continue
            liq = fnum((p.get("liquidity") or {}).get("usd")) or 0
            best = out.get(addr)
            if best is None or liq > (fnum((best.get("liquidity") or {}).get("usd")) or 0):
                out[addr] = p
    return out


def summarize_pair(p):
    if not p:
        return None
    info = p.get("info") or {}
    pc = p.get("priceChange") or {}
    vol = p.get("volume") or {}
    return {
        "name": (p.get("baseToken") or {}).get("name"),
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "dex_id": p.get("dexId"),
        "liquidity": fnum((p.get("liquidity") or {}).get("usd")),
        "mcap": first(fnum(p.get("marketCap")), fnum(p.get("fdv"))),
        "price": fnum(p.get("priceUsd")),
        "created_ms": p.get("pairCreatedAt"),
        "pc_m5": fnum(pc.get("m5")),
        "pc_h1": fnum(pc.get("h1")),
        "pc_h6": fnum(pc.get("h6")),
        "pc_h24": fnum(pc.get("h24")),
        "vol_h1": fnum(vol.get("h1")),
        "vol_h6": fnum(vol.get("h6")),
        "vol_h24": fnum(vol.get("h24")),
        "txns_h6": sum(int(((p.get("txns") or {}).get("h6") or {}).get(k) or 0) for k in ("buys", "sells")) or None,
        "boosts": int((p.get("boosts") or {}).get("active") or 0),
        "socials": bool(info.get("websites") or info.get("socials")),
        "social_types": sorted({str(x.get("type") or "").lower() for x in (info.get("socials") or []) if isinstance(x, dict)}
                               | ({"web"} if info.get("websites") else set())),
    }


def ds_orders(http, mint):
    data = http.call(f"{DS}/orders/v1/solana/{mint}", min_interval=1.1, retries=1)
    types = []
    for o in as_list(data):
        if isinstance(o, dict) and str(o.get("status", "approved")).lower() in ("approved", "processing", "active"):
            types.append(o.get("type"))
    return types


# ---------------------------------------------------------------------------
# RugCheck
# ---------------------------------------------------------------------------
EXT_KEYS = {
    "permanentdelegate": "permanent delegate",
    "transferhook": "transfer hook",
    "nontransferable": "送金不可(non-transferable)",
    "pausable": "一時停止(pausable)",
}
RISK_EXT_WORDS = {
    "permanent delegate": "permanent delegate",
    "transfer hook": "transfer hook",
    "non-transferable": "送金不可(non-transferable)",
    "non transferable": "送金不可(non-transferable)",
    "pausable": "一時停止(pausable)",
    "paused": "一時停止(pausable)",
}


def _ext_active(key, v):
    if not isinstance(v, dict):
        return bool(v) or v == {}
    if key == "transferhook":
        return bool(v.get("programId") or v.get("program_id") or v.get("program"))
    if key == "permanentdelegate":
        return bool(v.get("delegate") or v.get("permanentDelegate") or v.get("authority"))
    if key == "pausable":
        return bool(v.get("authority") or v.get("paused"))
    return True


def find_dangerous_extensions(ext):
    found = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower().replace("_", "")
                for key, label in EXT_KEYS.items():
                    if kl.startswith(key) and _ext_active(key, v):
                        found.add(label)
                    if isinstance(v, str) and v.lower().replace("_", "") == key:
                        found.add(label)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, str):
                    vl = v.lower().replace("_", "")
                    for key, label in EXT_KEYS.items():
                        if vl == key:
                            found.add(label)
                else:
                    walk(v)

    walk(ext)
    return found


def split_pool_holders(rep):
    """RugCheck の topHolders を「普通の保有者」と「流動性プールの口座」に分ける。
    同じレポート(同じ時点のデータ)の中だけで照合する。DEX Screener の数値は取得時点がずれるので使わない。
      ① knownAccounts で AMM と判定された口座(口座そのもの・持ち主)
      ② markets のプール本体・プールのトークン口座・その持ち主
      ③ 保有数が、どれかのプールのトークン残高とほぼ一致(±0.5%)
      ④ 保有数×価格が、どれかのプールのトークン側の金額とほぼ一致(±20%・設定可、$1,000以上のプールのみ)
         (= 総流動性 − SOL・ステーブル側の流動性)"""
    mint = rep.get("mint")
    price = fnum(rep.get("price"))
    pd = CFG.get("pool_detection") or {}
    amt_tol = pd.get("amount_tolerance_pct", 0.5) / 100
    val_tol = pd.get("value_tolerance_pct", 20) / 100
    val_min = pd.get("value_min_pool_usd", 1000)
    pool_addrs = set()
    for addr, info in (rep.get("knownAccounts") or {}).items():
        if str((info or {}).get("type", "")).upper() == "AMM":
            pool_addrs.add(addr)
    reserves = []   # (トークン残高, トークン側の金額USD)
    for m in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if m.get(k):
                pool_addrs.add(m[k])
        for k in ("liquidityAAccount", "liquidityBAccount"):
            owner = (m.get(k) or {}).get("owner")
            if owner:
                pool_addrs.add(owner)
        lp = m.get("lp") or {}
        if lp.get("quoteMint") == mint and lp.get("baseMint") != mint:
            reserves.append((fnum(lp.get("quote")), fnum(lp.get("quoteUSD"))))
        else:
            reserves.append((fnum(lp.get("base")), fnum(lp.get("baseUSD"))))
    kept, pools = [], []
    for h in rep.get("topHolders") or []:
        amt = fnum(h.get("uiAmount")) or fnum(h.get("uiAmountString"))
        how = None
        if h.get("address") in pool_addrs or h.get("owner") in pool_addrs:
            how = "口座一致"
        elif amt and any(r and abs(amt - r) <= r * amt_tol for r, _ in reserves):
            how = "残高一致"
        elif (pd.get("use_value_match", True) and amt and price
              and any(usd and usd >= val_min and abs(amt * price - usd) <= usd * val_tol for _, usd in reserves)):
            how = "金額一致"
        if how:
            pools.append((fnum(h.get("pct")) or 0, how))
        else:
            kept.append(h)
    return kept, pools


def summarize_rugcheck(rep):
    if not rep or not isinstance(rep, dict):
        return None
    tok = rep.get("token") or {}
    supply = fnum(tok.get("supply"))
    risks = [r for r in (rep.get("risks") or []) if isinstance(r, dict)]
    risk_text = [(str(r.get("name", "")) + " " + str(r.get("description", ""))).lower() for r in risks]

    danger = find_dangerous_extensions(rep.get("token_extensions"))
    for t in risk_text:
        for w, label in RISK_EXT_WORDS.items():
            if w in t:
                danger.add(label)

    # 上位10件(流動性プールの口座を除く)
    holders, pools = split_pool_holders(rep)
    top10 = sum(fnum(h.get("pct")) or 0 for h in holders[:10]) if rep.get("topHolders") else None

    # インサイダー
    nets = rep.get("insiderNetworks") or []
    ins_count = max(sum(int(n.get("size") or 0) for n in nets if isinstance(n, dict)),
                    int(rep.get("graphInsidersDetected") or 0))
    ins_pcts = []
    amt = sum(fnum(n.get("tokenAmount")) or 0 for n in nets if isinstance(n, dict))
    if supply and 0 < amt <= supply:
        ins_pcts.append(amt / supply * 100)
    hp = sum(fnum(h.get("pct")) or 0 for h in holders if h.get("insider"))
    if hp:
        ins_pcts.append(hp)
        ins_count = max(ins_count, sum(1 for h in holders if h.get("insider")))

    # LP(一番大きい市場)
    main, main_usd = None, -1
    for m in rep.get("markets") or []:
        lp = m.get("lp") or {}
        usd = (fnum(lp.get("quoteUSD")) or 0) + (fnum(lp.get("baseUSD")) or 0)
        if usd > main_usd:
            main, main_usd = m, usd
    lp_locked = fnum(((main or {}).get("lp") or {}).get("lpLockedPct")) if main else None
    # LP トークンがない仕組みのプール(Meteora DLMM・CLMM など)は LP 項目の対象外
    lp_applicable = None
    if main:
        mtype = str(main.get("marketType", "")).lower().replace("_", "").replace("-", "")
        no_lp_types = [t.lower().replace("_", "").replace("-", "") for t in CFG["scoring"]["lp"]["no_lp_market_types"]]
        no_lp_mint = "mintLP" in main and main.get("mintLP") in NO_LP_MINTS
        lp_applicable = not (no_lp_mint or any(t in mtype for t in no_lp_types))

    creator_bal = fnum(rep.get("creatorBalance"))
    tf = rep.get("transferFee") or {}

    return {
        "mint_renounced": rep.get("mintAuthority", tok.get("mintAuthority")) in (None, ""),
        "freeze_renounced": rep.get("freezeAuthority", tok.get("freezeAuthority")) in (None, ""),
        "danger_ext": sorted(danger),
        "rugged": bool(rep.get("rugged")),
        "top10": top10,
        "top1": max((fnum(h.get("pct")) or 0 for h in holders), default=None) if rep.get("topHolders") else None,
        "pool_holders": [[round(p, 2), how] for p, how in pools],
        "launchpad": [str((rep.get("launchpad") or {}).get("name") or ""), str((rep.get("launchpad") or {}).get("platform") or "")]
        if isinstance(rep.get("launchpad"), dict) else [str(rep.get("launchpad") or ""), ""],
        "insider_count": ins_count,
        "insider_pct": max(ins_pcts) if ins_pcts else None,
        "lp_locked_pct": lp_locked,
        "lp_applicable": lp_applicable,
        "lp_market": (main or {}).get("marketType"),
        "has_markets": bool(rep.get("markets")),
        "dev_pct": (creator_bal / supply * 100) if (supply and creator_bal is not None and creator_bal <= supply) else None,
        "transfer_fee_pct": fnum(tf.get("pct")) if isinstance(tf, dict) else None,
        "name": (rep.get("tokenMeta") or {}).get("name"),
        "symbol": (rep.get("tokenMeta") or {}).get("symbol"),
        "risks": [[str(r.get("name", ""))[:80], str(r.get("description", ""))[:160]] for r in risks][:15],
        "description": str((rep.get("fileMeta") or {}).get("description") or "")[:300],
    }


# ---------------------------------------------------------------------------
# Solana Tracker(pump.fun 卒業前・バンドル・スナイパー)
# ---------------------------------------------------------------------------
class SolanaTracker:
    def __init__(self, http, state):
        self.http = http
        self.key = os.environ.get("SOLANATRACKER_API_KEY", "").strip()
        month = dt.datetime.now(JST).strftime("%Y-%m")
        u = state.setdefault("st_usage", {})
        if u.get("month") != month:
            u.clear()
            u.update({"month": month, "count": 0})
        self.usage = u
        self.budget = CFG["solana_tracker"]["monthly_budget"]

    def limit_now(self):
        """月の上限を日割りして、月初に使い切らないようにする(1日分の余裕つき)"""
        if not CFG["solana_tracker"].get("daily_pacing", True):
            return self.budget
        now = dt.datetime.now(JST)
        days = calendar.monthrange(now.year, now.month)[1]
        return min(self.budget, self.budget * (now.day + now.hour / 24) / days + self.budget / days)

    def can(self, n=1):
        return bool(self.key) and self.usage["count"] + n <= self.limit_now()

    def _call(self, path, **kw):
        if not self.can():
            return None
        self.usage["count"] += 1
        return self.http.call(ST + path, headers={"x-api-key": self.key}, min_interval=0.4, **kw)

    def graduating(self):
        c = CFG["sources"]["pumpfun"]
        params = {"minCurve": c["min_curve"], "maxCurve": c["max_curve"], "minHolders": 0, "limit": c["limit"]}
        return as_list(self._call("/tokens/multi/graduating", params=params))

    def token_list(self, path):
        """トレンド・卒業済みなどの一覧(token info の配列)"""
        return [x for x in as_list(self._call(path)) if isinstance(x, dict)]

    def multi(self, mints):
        out = {}
        mints = list(mints)
        for i in range(0, len(mints), 20):
            data = self._call("/tokens/multi", method="POST", body={"tokens": mints[i:i + 20]})
            if isinstance(data, dict):
                toks = data.get("tokens", data)
                if isinstance(toks, dict):
                    out.update({k: v for k, v in toks.items() if isinstance(v, dict)})
        return out


def summarize_st(ti):
    if not ti or not isinstance(ti, dict):
        return None
    tok = ti.get("token") or {}
    pools = [p for p in (ti.get("pools") or []) if isinstance(p, dict)]
    risk = ti.get("risk") or {}

    def liq(p):
        return fnum((p.get("liquidity") or {}).get("usd")) or 0

    main = max(pools, key=liq) if pools else {}
    pump = next((p for p in pools if str(p.get("market", "")).lower() in PUMPFUN_MARKETS), None)
    curve = None
    if pump:
        curve = first(fnum(pump.get("curvePercentage")), fnum((pump.get("curve") or {}).get("percentage"))
                      if isinstance(pump.get("curve"), dict) else None)
    others = [p for p in pools if p is not pump and liq(p) > 0]
    pre_grad = bool(pump) and (curve is None or curve < 100) and not others
    ref = pump if pre_grad else main

    bund = risk.get("bundlers") or {}
    bundle = fnum(bund.get("totalInitialPercentage") if CFG.get("bundle_metric") == "initial"
                  else bund.get("totalPercentage"))
    events = ti.get("events") or {}

    def ev(k):
        return fnum((events.get(k) or {}).get("priceChangePercentage"))

    created = (tok.get("creation") or {}).get("created_time")
    created_ms = created * 1000 if created and created < 1e12 else created
    created_ms = created_ms or ref.get("createdAt")
    sec = ref.get("security") or {}
    insiders = risk.get("insiders") or {}
    return {
        "name": tok.get("name"),
        "symbol": tok.get("symbol"),
        "description": str(tok.get("description") or "")[:300],
        "pre_grad": pre_grad,
        "curve": curve,
        "market": ref.get("market"),
        "liquidity": liq(ref) if ref else None,
        "mcap": fnum((ref.get("marketCap") or {}).get("usd")),
        "price": fnum((ref.get("price") or {}).get("usd")),
        "created_ms": created_ms,
        "pc_m5": ev("5m"), "pc_h1": ev("1h"), "pc_h6": ev("6h"),
        "vol_h24": fnum((ref.get("txns") or {}).get("volume24h")),
        "holders": ti.get("holders"),
        "bundle": bundle,
        "sniper": fnum((risk.get("snipers") or {}).get("totalPercentage")),
        "insider_count": int(insiders.get("count") or 0),
        "insider_pct": fnum(insiders.get("totalPercentage")),
        "top10": fnum(risk.get("top10")),
        "dev_pct": fnum((risk.get("dev") or {}).get("percentage")),
        "rugged": bool(risk.get("rugged")),
        "mint_renounced": ("mintAuthority" in sec and sec.get("mintAuthority") in (None, "")) if sec else None,
        "freeze_renounced": ("freezeAuthority" in sec and sec.get("freezeAuthority") in (None, "")) if sec else None,
        "lp_burn": fnum(main.get("lpBurn")) if main else None,
        "socials": bool(tok.get("strictSocials")) or any(tok.get(k) for k in ("twitter", "website", "telegram")),
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def build_view(mint, source, pair, rc, st, ad_types, st_unavailable=False, out_of_grad_list=False, desc=""):
    ps = summarize_pair(pair) or {}
    category = "B" if (st and st.get("pre_grad")) or (not st and ps.get("dex_id") == "pumpfun") else "A"
    main, sub = (st or {}, ps) if category == "B" else (ps, st or {})

    def pick(k):
        return first(main.get(k), sub.get(k))

    created = pick("created_ms")
    age = (now_ts() * 1000 - created) / 60000 if created else None
    rc = rc or {}
    st = st or {}
    ins_pcts = [x for x in (rc.get("insider_pct"), st.get("insider_pct")) if x]
    return {
        "mint": mint,
        "source": source,
        "category": category,
        "name": first(pick("name"), rc.get("name"), "?"),
        "symbol": first(pick("symbol"), rc.get("symbol"), "?"),
        "age_min": age,
        # DEX Screener が流動性 0 を返すプール(新しい種類など)があるため、0 は「不明」として他の取得元を使う
        "liquidity": first(main.get("liquidity") or None, sub.get("liquidity") or None),
        "mcap": pick("mcap"),
        "price": pick("price"),
        "pc_m5": pick("pc_m5"), "pc_h1": pick("pc_h1"), "pc_h6": pick("pc_h6"), "pc_h24": ps.get("pc_h24"),
        "vol_h1": ps.get("vol_h1"),
        "avg_trade_h6": (ps["vol_h6"] / ps["txns_h6"]) if ps.get("vol_h6") and ps.get("txns_h6") else None,
        "vol_h24": first(ps.get("vol_h24"), st.get("vol_h24")),
        "rc_checked": bool(rc),
        "st_checked": bool(st),
        "st_unavailable": st_unavailable,
        "mint_renounced": first(rc.get("mint_renounced"), st.get("mint_renounced")),
        "freeze_renounced": first(rc.get("freeze_renounced"), st.get("freeze_renounced")),
        "danger_ext": rc.get("danger_ext") or [],
        "rugged": bool(rc.get("rugged") or st.get("rugged")),
        "bundle": st.get("bundle"),
        "sniper": st.get("sniper"),
        "top10": first(rc.get("top10"), st.get("top10")),
        "top1": rc.get("top1"),
        "dev_pct": first(st.get("dev_pct"), rc.get("dev_pct")),
        "holders": st.get("holders"),
        "curve": st.get("curve"),
        "curve_from_api": source == "pump.fun卒業前",
        "out_of_grad_list": out_of_grad_list and not st,
        "insider_count": max(rc.get("insider_count") or 0, st.get("insider_count") or 0),
        "insider_pct": max(ins_pcts) if ins_pcts else None,
        "risks": rc.get("risks") or [],
        "launchpad": rc.get("launchpad") or [],
        "st_market": st.get("market"),
        "pool_holders": rc.get("pool_holders") or [],
        "description": first(desc or None, st.get("description") or None, rc.get("description") or None, ""),
        "lp_locked_pct": rc.get("lp_locked_pct"),
        "lp_applicable": rc.get("lp_applicable"),
        "lp_market": rc.get("lp_market"),
        "lp_status": "",
        "lp_burn": st.get("lp_burn"),
        "has_markets": rc.get("has_markets"),
        "has_ads": (ps.get("boosts") or 0) > 0 or any(t in CFG["scoring"]["ad_order_types"] for t in (ad_types or [])),
        "socials": bool(ps.get("socials") or st.get("socials")),
        "social_types": ps.get("social_types") or [],
        "transfer_fee_pct": rc.get("transfer_fee_pct"),
    }


def classify(v):
    """戻り値: dict(verdict, tier, score, breakdown, reasons, need)"""
    m = CFG["mandatory"]
    ex = []
    suffix = m.get("require_address_suffix") or ""
    if suffix and not v["mint"].endswith(suffix):
        # 末尾が pump でなくても、RugCheck の発行元(launchpad)が Pump.Fun なら通過
        if not v["rc_checked"]:
            return {"verdict": "見送り", "tier": None, "score": None, "breakdown": [],
                    "reasons": ["発行元の確認待ち(RugCheck)"], "need": {"rc"}}
        allowed = {str(x).lower() for x in m.get("allowed_launchpads") or []}
        lp = [str(x).lower() for x in v.get("launchpad") or [] if x]
        st_pump = str(v.get("st_market") or "").lower() in PUMPFUN_MARKETS or \
            str(v.get("st_market") or "").lower().startswith("pumpfun")
        if not any(x in allowed for x in lp) and not st_pump:
            name = (v.get("launchpad") or [""])[0] or "不明"
            return {"verdict": "除外", "tier": None, "score": None, "breakdown": [], "permanent": True,
                    "reasons": [f"アドレス末尾が「{suffix}」でなく、発行元も Pump.Fun でない(発行元: {name})"], "need": set()}
    risk_texts = [(n + " " + d).lower() for n, d in v.get("risks") or []]
    for item in m.get("exclude_risk_keywords") or []:
        if any(item["keyword"].lower() in t for t in risk_texts):
            ex.append(item["label"])
    if m["require_mint_renounced"] and v["mint_renounced"] is False:
        ex.append("追加発行の権限が残っている")
    if m["require_freeze_renounced"] and v["freeze_renounced"] is False:
        ex.append("凍結権限が残っている")
    if m["reject_dangerous_extensions"] and v["danger_ext"]:
        ex.append("危険な拡張機能: " + "・".join(v["danger_ext"]))
    if m["reject_rugged"] and v["rugged"]:
        ex.append("rugged 判定")
    if ex:
        return {"verdict": "除外", "tier": None, "score": None, "breakdown": [], "reasons": ex, "need": set()}

    fails, unknowns = [], []

    def unknown(label, what):
        unknowns.append(f"{label}が取得できない")

    if v["category"] == "A":
        c = CFG["criteria_a"]
        if v["age_min"] is None:
            fails.append("経過時間が不明")
        elif v["age_min"] < c["min_age_minutes"]:
            fails.append(f"発行から{c['min_age_minutes']}分未満")
        if not v["liquidity"]:
            fails.append("流動性が取得できない(0表示)")
        elif v["liquidity"] < c["min_liquidity_usd"]:
            fails.append(f"流動性{fmt_usd(c['min_liquidity_usd'])}未満")
        if v["top10"] is None:
            if v["rc_checked"]:
                fails.append("上位10件の保有率が不明")
        elif v["top10"] > c["max_top10_pct"]:
            fails.append(f"上位10件の保有{c['max_top10_pct']}%超")
        if v["bundle"] is None:
            unknown("バンドル率", "st")
        elif v["bundle"] > c["max_bundle_pct"]:
            fails.append(f"バンドル{c['max_bundle_pct']}%超")
        # --- C基準の追加項目(config.yaml で項目ごとに無効化できる) ---
        if c.get("min_mcap_usd") is not None or c.get("max_mcap_usd") is not None:
            lo, hi = c.get("min_mcap_usd") or 0, c.get("max_mcap_usd") or float("inf")
            if v["mcap"] is None:
                fails.append("時価総額が不明")
            elif not (lo <= v["mcap"] <= hi):
                fails.append(f"時価総額が{fmt_usd(lo)}〜{fmt_usd(hi)}の範囲外")
        if c.get("min_holders"):
            if v["holders"] is None:
                unknown("保有者数", "st")
            elif v["holders"] < c["min_holders"]:
                fails.append(f"保有者{c['min_holders']}人未満")
        if c.get("max_insider_pct") is not None and v["insider_pct"] is not None \
                and v["insider_pct"] > c["max_insider_pct"]:
            fails.append(f"インサイダー{c['max_insider_pct']}%超")
        if c.get("max_dev_pct") is not None and v["dev_pct"] is not None and v["dev_pct"] > c["max_dev_pct"]:
            fails.append(f"開発者の保有{c['max_dev_pct']}%超")
        if c.get("min_avg_trade_usd") and v.get("avg_trade_h6") is not None \
                and v["avg_trade_h6"] < c["min_avg_trade_usd"]:
            fails.append(f"平均取引額${c['min_avg_trade_usd']}未満(Bot水増しの疑い)")
        if c.get("max_change_h6_pct") is not None and v["pc_h6"] is not None and v["pc_h6"] > c["max_change_h6_pct"]:
            fails.append(f"6時間で+{c['max_change_h6_pct']}%超の急騰中(押し目待ち)")
    elif not CFG["criteria_b"].get("enabled", True):
        fails.append("pump.fun卒業前(C基準では対象外)")
    else:
        c = CFG["criteria_b"]
        if v["curve"] is None:
            if v.get("out_of_grad_list"):
                fails.append(f"卒業進捗が{c['min_curve_pct']}〜{c['max_curve_pct']}%の範囲外(pump.fun一覧になし)")
            elif not v["curve_from_api"]:
                unknown("卒業までの進み具合", "st")
        elif not (c["min_curve_pct"] <= v["curve"] <= c["max_curve_pct"]):
            fails.append(f"卒業進捗が{c['min_curve_pct']}〜{c['max_curve_pct']}%の範囲外")
        if v["mcap"] is None or not (c["min_mcap_usd"] <= v["mcap"] <= c["max_mcap_usd"]):
            fails.append(f"時価総額が{fmt_usd(c['min_mcap_usd'])}〜{fmt_usd(c['max_mcap_usd'])}の範囲外")
        if v["holders"] is None:
            unknown("保有者数", "st")
        elif v["holders"] < c["min_holders"]:
            fails.append(f"保有者{c['min_holders']}人未満")
        if v["dev_pct"] is None:
            unknown("開発者の保有率", "st")
        elif v["dev_pct"] > c["max_dev_pct"]:
            fails.append(f"開発者の保有{c['max_dev_pct']}%超")
        if v["bundle"] is None:
            unknown("バンドル率", "st")
        elif v["bundle"] > c["max_bundle_pct"]:
            fails.append(f"バンドル{c['max_bundle_pct']}%超")

    if fails:
        # 他の基準で見送りが決まっている銘柄には、追加の API を使わない
        if not v["rc_checked"]:
            fails.append("(拡張機能などの安全性は未確認)")
        return {"verdict": "見送り", "tier": None, "score": None, "breakdown": [], "reasons": fails, "need": set()}
    if not v["rc_checked"]:
        return {"verdict": "見送り", "tier": None, "score": None, "breakdown": [], "reasons": ["RugCheck未取得"], "need": {"rc"}}
    if unknowns:
        if not v["st_checked"] and not v.get("st_unavailable"):
            return {"verdict": "見送り", "tier": None, "score": None, "breakdown": [], "reasons": ["審査データ待ち"], "need": {"st"}}
        if CFG.get("on_missing_bundle", "hold") == "hold":
            return {"verdict": "見送り", "tier": None, "score": None, "breakdown": [], "reasons": unknowns, "need": set()}

    score, br = score_token(v)
    t = CFG["tiers"]
    tier = "小" if score <= t["small_max"] else ("中" if score <= t["medium_max"] else "大")
    return {"verdict": f"候補(リスク{tier})", "tier": tier, "score": score, "breakdown": br, "reasons": [], "need": set()}


def is_copycat(v):
    words = [w.lower() for w in CFG["scoring"]["copycat"]["keywords"]]
    return any(w in (n + " " + d).lower() for n, d in v.get("risks") or [] for w in words)


def detect_themes(v):
    """銘柄名・シンボル・説明文からテーマを判定(点数には使わない)"""
    short = f"{v.get('name') or ''} {v.get('symbol') or ''}".lower()
    long = (v.get("description") or "").lower()
    found = []
    for theme, words in (CFG.get("themes") or {}).items():
        for w in words:
            w = str(w).lower()
            exact = w.startswith("=")
            w = w.lstrip("=")
            if exact or (w.isascii() and len(w) <= 3):
                pat = r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])"
                hit = re.search(pat, short) or re.search(pat, long)
                if not hit and not exact and w.isascii():
                    hit = w in short   # 短い英単語でも名前・シンボルの中なら一致(例: POPCAT)
            else:
                hit = w in short or w in long
            if hit:
                found.append(theme)
                break
    return found


def score_token(v):
    s = CFG["scoring"]
    br = []

    def add(label, pts):
        if pts:
            br.append((label, pts))

    p, b = band_points(v["bundle"], s["bundle"])
    add(f"バンドル{b['min']}〜{b['max']}%" if b else "", p)
    p, b = band_points(v["top10"], s["top10"])
    add(f"上位10件の保有{b['min']}〜{b['max']}%" if b else "", p)

    ins = s["insider"]
    if v["insider_count"] or (v["insider_pct"] or 0) > 0:
        large = v["insider_count"] >= ins["large_min_wallets"] or (v["insider_pct"] or 0) > ins["large_min_pct"]
        add(f"インサイダー群あり({v['insider_count']}個・{fmt_pct(v['insider_pct'])})",
            ins["large_points"] if large else ins["small_points"])
    if is_copycat(v):
        add("便乗の疑い(有名銘柄と同じシンボル)", s["copycat"]["points"])

    lp = s["lp"]
    burn, lock = v["lp_burn"], v["lp_locked_pct"]
    if v["category"] == "B":
        v["lp_status"] = "対象外(ボンディングカーブ)"
    elif v["lp_applicable"] is False:
        v["lp_status"] = f"対象外(LPトークンなし: {v['lp_market']})"
    elif v["lp_applicable"] is None:
        v["lp_status"] = "不明(市場情報なし)"
        add("LPの状態が不明", lp["unknown"])
    elif burn is not None and burn >= lp["burn_threshold_pct"]:
        v["lp_status"] = f"焼却済み({burn:.0f}%)"
    elif lock is not None and lock >= lp["partial_threshold_pct"]:
        v["lp_status"] = f"ロックのみ({lock:.0f}%)"
        add("LPはロックのみ(焼却なし)", lp["lock_only"])
    elif lock:
        v["lp_status"] = f"一部ロック({lock:.0f}%)"
        add(f"LPロック率{lock:.0f}%", lp["partial_lock"])
    else:
        v["lp_status"] = "ロック・焼却なし"
        add("LPのロック・焼却なし", lp["no_lock"])

    p, b = band_points(v["age_min"], s["age_minutes"])
    add(f"発行から{fmt_age(b['min'])}〜{fmt_age(b['max'])}" if b else "", p)
    if v["category"] == "B":
        add("pump.fun卒業前", s["pre_graduation"])
    p, b = band_points(v["liquidity"], s["liquidity_usd"])
    add(f"流動性{fmt_usd(b['min'])}〜{fmt_usd(b['max'])}" if b else "", p)

    pm = s["price_move_1h"]
    if v["pc_h1"] is not None and abs(v["pc_h1"]) > pm["abs_pct"]:
        add(f"1時間で{fmt_pct(v['pc_h1'], True)}の値動き", pm["points"])
    vl = s["volume_to_liquidity"]
    vol = v["vol_h1"] if vl.get("window") == "h1" else v["vol_h24"]
    if vol and v["liquidity"] and vol / v["liquidity"] >= vl["ratio"]:
        add(f"出来高が流動性の{vol / v['liquidity']:.0f}倍", vl["points"])
    if v["has_ads"]:
        add("広告(ブースト等)を購入", s["ads"])
    if not v["socials"]:
        add("公式サイト・SNSなし", s["no_socials"])
    tf = s["transfer_fee"]
    if v["transfer_fee_pct"] is not None and v["transfer_fee_pct"] > tf["over_pct"]:
        add(f"送金手数料{v['transfer_fee_pct']:.1f}%", tf["points"])
    return sum(p for _, p in br), br


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def slack_send(payload, dry):
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not dry and not url:
        log("  ⚠ SLACK_WEBHOOK_URL が未設定のため通知できません")
        return False
    if dry:
        log("  [Slack送信(テスト表示)]\n" + (payload.get("text") or "") + "\n" +
            "\n".join(b.get("text", {}).get("text", "") for a in payload.get("attachments", [])
                      for b in a.get("blocks", []) if b.get("type") == "section"))
        return True
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log(f"  Slack送信エラー {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except requests.RequestException as e:
        log(f"  Slack送信エラー: {e}")
        return False


# ---------------------------------------------------------------------------
# ナラティブ(参考・点数外):Googleニュース(RSS・無料)と公式SNSの有無
# ---------------------------------------------------------------------------
CRYPTO_WORDS = ("meme coin", "memecoin", "token", "crypto", "solana", "pump.fun", "airdrop", "market cap", "コイン", "仮想通貨")
GENERIC_NAMES = {"ai", "si", "the", "coin", "token", "meme", "agent", "agency", "cat", "dog", "pepe", "inu"}


def news_query(v):
    """検索語。短すぎる・一般的すぎる名前は検索しない"""
    name = re.sub(r"\s+", " ", str(v.get("name") or "")).strip()
    if len(name) < 4 or name.lower() in GENERIC_NAMES:
        return None
    # 1単語の名前(Build・Life など)は普通の英単語と区別できないため検索しない
    if len(name.split(" ")) < 2:
        return None
    return name


def fetch_news(query):
    """Googleニュースの直近24時間の記事。戻り値: (一般記事数, 仮想通貨記事数, 代表見出し) / 失敗時 None"""
    try:
        r = requests.get("https://news.google.com/rss/search",
                         params={"q": f'"{query}" when:1d', "hl": "en-US", "gl": "US", "ceid": "US:en"},
                         timeout=10, headers={"User-Agent": "Mozilla/5.0 (meme-scanner)"})
        if r.status_code != 200:
            return None
        return parse_news(r.text, query=query)
    except (requests.RequestException, ET.ParseError):
        return None


def parse_news(xml_text, now=None, query=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    root = ET.fromstring(xml_text)
    general, crypto, top = 0, 0, ""
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            if (now - pub).total_seconds() > 86400:
                continue
        except (TypeError, ValueError):
            pass
        # 見出しに銘柄名がそのまま含まれる記事だけ数える(本文だけの一致は無関係な記事が多いため)
        if query and re.sub(r"\s+", " ", query.lower()) not in re.sub(r"\s+", " ", title.lower()):
            continue
        if any(w in title.lower() for w in CRYPTO_WORDS):
            crypto += 1
        else:
            general += 1
            top = top or title
        if not top:
            top = title
    return general, crypto, top


def narrative(v, state):
    """(星の数0〜3, 表示文, CSV用の短い文)。点数(リスク)には含めない"""
    nc = CFG.get("narrative") or {}
    if not nc.get("enabled", True):
        return None
    cache = state.setdefault("news_cache", {})
    key = v["mint"]
    c = cache.get(key)
    if not (c and now_ts() - c["ts"] < nc.get("cache_hours", 3) * 3600):
        q = news_query(v)
        res = None
        if q and state.setdefault("_news_calls", 0) < nc.get("max_per_run", 10):
            state["_news_calls"] += 1
            res = fetch_news(q)
        c = {"ts": now_ts(), "q": q, "res": list(res) if res else None}
        cache[key] = c
    stars, parts = 0, []
    if c["q"] is None:
        parts.append("ニュース: 名前が1単語・短い・一般的なため検索せず")
    elif c["res"] is None:
        parts.append("ニュース: 取得できず")
    else:
        g, k, top = c["res"]
        if g >= 5:
            stars += 2
        elif g >= 1:
            stars += 1
        parts.append(f"ニュース24h: 一般{g}件・仮想通貨{k}件" + (f"「{top[:60]}」" if top else ""))
    st = v.get("social_types") or []
    names = {"twitter": "X", "web": "Web", "telegram": "TG", "discord": "Discord"}
    shown = [names.get(t, t) for t in st]
    if ("twitter" in st and "web" in st) or len(st) >= 3:
        stars += 1
    parts.append("SNS: " + ("・".join(shown) if shown else ("あり" if v.get("socials") else "なし")))
    stars = min(stars, 3)
    text = "★" * stars + "☆" * (3 - stars) + " ｜ " + " ｜ ".join(parts)
    return stars, text, f"{stars}/3 " + " / ".join(parts)


def candidate_payload(v, res, prev_tier=None, paper_msg="", narr=None):
    tier = res["tier"]
    style = TIER_STYLE[tier]
    cat = "A 卒業済み" if v["category"] == "A" else "B pump.fun卒業前"
    change = f"(段階変更: {prev_tier}→{tier})" if prev_tier and prev_tier != tier else ""
    lines = "\n".join(f"• {lab}: +{pts}" for lab, pts in res["breakdown"]) or "• 加点なし"
    curve = f" ｜ 卒業進捗: {fmt_pct(v['curve'])}" if v["category"] == "B" else ""
    holders = f" ｜ 保有者: {v['holders']}人" if v.get("holders") is not None else ""
    mint = v["mint"]
    links = (f"<https://dexscreener.com/solana/{mint}|DEX Screener> ｜ "
             f"<https://gmgn.ai/sol/token/{mint}|GMGN> ｜ "
             f"<https://rugcheck.xyz/tokens/{mint}|RugCheck>")
    if v["category"] == "B":
        links += f" ｜ <https://pump.fun/coin/{mint}|pump.fun>"
    themes = detect_themes(v)
    warn = "⚠️ *便乗の疑い*(有名銘柄と同じシンボル)\n" if is_copycat(v) else ""
    paper = f"🧪 仮想売買: {paper_msg}(実際の売買はしません)\n" if paper_msg else ""
    body = (
        f"*{v['name']} ({v['symbol']})*  ｜ {cat}{curve}\n"
        f"`{mint}`\n"
        f"{warn}"
        f"テーマ(参考・点数外): {'・'.join(themes) if themes else '判定なし'}\n"
        f"{('ナラティブ(参考・点数外): ' + narr[1] + chr(10)) if narr else ''}"
        f"経過: {fmt_age(v['age_min'])} ｜ 時価総額: {fmt_usd(v['mcap'])} ｜ 流動性: {fmt_usd(v['liquidity'])}\n"
        f"値動き: 5分 {fmt_pct(v['pc_m5'], True)} ｜ 1時間 {fmt_pct(v['pc_h1'], True)} ｜ 6時間 {fmt_pct(v['pc_h6'], True)}\n"
        f"バンドル: {fmt_pct(v['bundle'])} ｜ 上位10件: {fmt_pct(v['top10'])} ｜ 開発者: {fmt_pct(v['dev_pct'])}{holders}\n"
        f"LP: {v.get('lp_status') or '不明'}\n"
        f"\n*点数の内訳*\n{lines}\n\n{paper}{links}"
    )
    title = f"{style['emoji']} 候補(リスク{tier}) 合計{res['score']}点 — {v['symbol']} {change}"
    return {
        "text": f"*{title}*",
        "attachments": [{
            "color": style["color"],
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"検出元: {v['source']} ｜ {jst()} JST ｜ 投資判断はご自身で"}]},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# 🎯 勝ちパターン(急騰→急落→横ばい)の検出
# ---------------------------------------------------------------------------
def lp_unsafe(v):
    """流動性が時価総額より大きいのに、LP が焼却もロック(90%以上)もされていない = 持ち逃げの典型パターン"""
    liq, mc = v.get("liquidity"), v.get("mcap")
    if not liq or not mc or liq <= mc or v.get("lp_applicable") is False:
        return False
    burn, lock = v.get("lp_burn"), v.get("lp_locked_pct")
    return not ((burn or 0) >= 90 or (lock or 0) >= 90)


def record_hist(w, v, t, pc, mcap=None, liq=None):
    """値動きの記録 [時刻, 時価総額, 保有者数, 流動性] を残す(約15〜30分ごと)"""
    mcap = mcap or v["mcap"]
    liq = liq if liq is not None else (v.get("liquidity") or 0)
    h = w.setdefault("h", [])
    h.append([int(t), round(mcap), v.get("holders"), round(liq or 0)])
    keep = (pc.get("lookback_hours", 6) + 1) * 3600
    w["h"] = [x for x in h if t - x[0] <= keep][-48:]
    # 生まれたて枠・すき間枠も含め、ある程度の規模に一度でも届いた銘柄は、しばらく短い間隔で記録する
    if mcap >= pc.get("fast_watch_min_mcap_usd", 15000) and (liq or 0) >= pc.get("fast_watch_min_liquidity_usd", 8000):
        w["pw"] = int(t + pc.get("lookback_hours", 6) * 3600)


def confirmed_peak_index(H, pc):
    """一瞬だけの急騰(前後の記録が大きく下)は高値として扱わない。
    前後30分以内に、高値の85%以上の記録がもう1つある最も高い記録を高値とする"""
    win = pc.get("peak_confirm_minutes", 30) * 60
    ratio = pc.get("peak_confirm_ratio", 0.85)
    for i in sorted(range(len(H)), key=lambda i: -H[i][1]):
        if any(j != i and abs(H[j][0] - H[i][0]) <= win and H[j][1] >= H[i][1] * ratio for j in range(len(H))):
            return i
    return max(range(len(H)), key=lambda i: H[i][1])


def crashed_before(v, pc):
    """記録を始める前に大暴落していた銘柄(全期間の高値から大きく下)を、6時間・24時間の値動きで見分ける"""
    lim = -pc.get("max_drop_from_peak_pct", 75)
    for k in ("pc_h6", "pc_h24"):
        if v.get(k) is not None and v[k] <= lim:
            return f"{k[3:]}で{v[k]:.0f}%(記録前の暴落)"
    return ""


def dip_eval(hist, v, pc, now=None):
    """📉 急落の検出: 高値から-40%以上(上限あり)下がった安全な銘柄。横ばいの判定は5分足で人が行う"""
    now = now or now_ts()
    H = [x for x in hist if now - x[0] <= pc["lookback_hours"] * 3600 and x[1]]
    if len(H) < 2:
        return None, "記録が少ない"
    pi = confirmed_peak_index(H, pc)
    peak, cur = H[pi], H[-1]
    if pi == len(H) - 1:
        return None, "高値を更新中"
    drop = (1 - cur[1] / peak[1]) * 100
    if drop < pc["drop_from_peak_pct"]:
        return None, f"高値からの下落が{drop:.0f}%"
    if drop > pc.get("max_drop_from_peak_pct", 75):
        return None, f"高値から{drop:.0f}%の暴落(ラグの疑い)"
    cb = crashed_before(v, pc)
    if cb:
        return None, cb
    steps = [H[i][1] / H[i - 1][1] - 1 for i in range(pi + 1, len(H)) if H[i - 1][1]]
    if steps and min(steps) * 100 <= -pc.get("rug_step_drop_pct", 70):
        return None, "1回で-70%以上の暴落(ラグの疑い)"
    after = H[pi + 1:]
    tail = H[-pc.get("frozen_records", 4):]
    if len(tail) >= pc.get("frozen_records", 4) and (max(x[1] for x in tail) - min(x[1] for x in tail)) / max(x[1] for x in tail) * 100 < pc.get("frozen_band_pct", 1):
        return None, "値段が止まっている"
    hs = [x[2] for x in after if x[2]]
    if len(hs) >= 2 and hs[-1] < max(hs) * (1 - pc["holders_drop_tolerance_pct"] / 100):
        return None, f"保有者が減少({max(hs)}→{hs[-1]}人)"
    post_low = min(x[1] for x in after)
    return {"peak": peak[1], "peak_ts": peak[0], "cur": cur[1], "drop": drop, "post_low": post_low,
            "holders_note": (f"{hs[0]}→{hs[-1]}人" if len(hs) >= 2 else (f"{hs[-1]}人" if hs else "不明")),
            "zone_hi": peak[1] * (1 - pc["drop_from_peak_pct"] / 100), "zone_lo": peak[1] * 0.5}, ""


def dip_payload(v, info, pc, section):
    mint = v["mint"]
    links = (f"<https://gmgn.ai/sol/token/{mint}|GMGN> ｜ <https://dexscreener.com/solana/{mint}|DEX Screener> ｜ "
             f"<https://rugcheck.xyz/tokens/{mint}|RugCheck>")
    body = (
        f"*{v['name']} ({v['symbol']})*\n`{mint}`\n"
        f"高値 {fmt_usd(info['peak'])}({jst(info['peak_ts'])[11:16]}) → 今 {fmt_usd(info['cur'])}(−{info['drop']:.0f}%) ｜ 高値後の最安値 {fmt_usd(info['post_low'])}\n"
        f"買い場の目安(高値から−40〜50%): {fmt_usd(info['zone_lo'])}〜{fmt_usd(info['zone_hi'])}\n"
        f"流動性: {fmt_usd(v['liquidity'])} ｜ 保有者: {info['holders_note']} ｜ 発行から: {fmt_age(v['age_min'])}\n"
        f"値動き: 5分 {fmt_pct(v['pc_m5'], True)} ｜ 1時間 {fmt_pct(v['pc_h1'], True)}\n"
        f"安全: 上位10件 {fmt_pct(v['top10'])} ｜ 最大1人 {fmt_pct(v.get('top1'))} ｜ インサイダー {fmt_pct(v['insider_pct'])} ｜ "
        f"バンドル {fmt_pct(v['bundle'])} ｜ 開発者 {fmt_pct(v['dev_pct'])} ｜ LP {v.get('lp_status') or '不明'}\n\n"
        f"👀 *GMGNの5分足で2〜3本、安値を更新せず止まっていたらチャートと保有者タブ(フィッシング・バンドル)を送ってください*\n\n{links}"
    )
    return {
        "text": f"*📉 急落・横ばい待ち【{section}】 — {v['symbol']}*",
        "attachments": [{
            "color": TIER_STYLE["📉"]["color"],
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{jst()} JST ｜ 投資判断はご自身で"}]},
            ],
        }],
    }


def pattern_eval(hist, v, pc, now=None):
    """戻り値: (情報dict, "") / (None, 理由)
    急騰→急落(短時間で高値から-40%以上)→横ばい の形かを、約15分ごとの記録で判定する"""
    now = now or now_ts()
    H = [x for x in hist if now - x[0] <= pc["lookback_hours"] * 3600 and x[1]]
    if len(H) < 3:
        return None, "記録が少ない"
    pi = confirmed_peak_index(H, pc)
    peak, cur = H[pi], H[-1]
    if pi == len(H) - 1:
        return None, "高値を更新中"
    drop = (1 - cur[1] / peak[1]) * 100
    if drop < pc["drop_from_peak_pct"]:
        return None, f"高値からの下落が{drop:.0f}%(基準{pc['drop_from_peak_pct']}%)"
    cb = crashed_before(v, pc)
    if cb:
        return None, cb
    after = H[pi + 1:]
    if pc.get("max_drop_from_peak_pct") and drop > pc["max_drop_from_peak_pct"]:
        return None, f"高値から{drop:.0f}%の暴落(ラグの疑い)"
    # ラグ型の暴落: 1回の記録(約15分)で-70%以上
    steps = [H[i][1] / H[i - 1][1] - 1 for i in range(pi + 1, len(H)) if H[i - 1][1]]
    if steps and min(steps) * 100 <= -pc.get("rug_step_drop_pct", 70):
        return None, "1回で-70%以上の暴落(ラグの疑い)"
    # 値段が止まっている(売買がほぼない)
    tail = H[-pc.get("frozen_records", 4):]
    if len(tail) >= pc.get("frozen_records", 4):
        tl, th = min(x[1] for x in tail), max(x[1] for x in tail)
        if th and (th - tl) / th * 100 < pc.get("frozen_band_pct", 1):
            return None, "値段が止まっている(売買がほぼない)"
    # ① 階段状の下落を除外: 高値から-40%に届くまでの時間が長いもの(じわじわ下げ続けている)は対象外
    target = peak[1] * (1 - pc["drop_from_peak_pct"] / 100)
    reach = next((x for x in after if x[1] <= target), None)
    mins_to_drop = (reach[0] - peak[0]) / 60 if reach else 0
    if reach and mins_to_drop > pc.get("max_drop_minutes", 90):
        return None, f"階段状の下落(高値から{pc['drop_from_peak_pct']}%下落に{mins_to_drop:.0f}分)"
    win = [x for x in after if cur[0] - x[0] <= pc["range_window_minutes"] * 60]
    span = (cur[0] - win[0][0]) / 60 if win else 0
    # 実行間隔のずれ(数分)を見込んで、3分短くても可
    if len(win) < 2 or span < pc["min_range_minutes"] - 3:
        return None, "横ばいの時間が足りない"
    lo, hi = min(x[1] for x in win), max(x[1] for x in win)
    mid = (lo + hi) / 2
    band = (hi - lo) / 2 / mid * 100
    if band > pc["band_pct"]:
        return None, f"値幅が±{band:.0f}%(基準±{pc['band_pct']}%)"
    prev_lo = min(x[1] for x in win[:-1])
    if cur[1] < prev_lo * (1 - pc["new_low_tolerance_pct"] / 100):
        return None, "横ばいの安値を更新"
    if v.get("pc_m5") is not None and v["pc_m5"] <= -pc["max_m5_drop_pct"]:
        return None, f"直近5分が{v['pc_m5']:.0f}%の大きな下落"
    hs = [x[2] for x in after if x[2]]
    if len(hs) >= 2:
        if hs[-1] < max(hs) * (1 - pc["holders_drop_tolerance_pct"] / 100):
            return None, f"保有者が減少({max(hs)}→{hs[-1]}人)"
        holders_note = f"{hs[0]}→{hs[-1]}人(減っていない)"
    else:
        holders_note = (f"{hs[-1]}人(推移は記録不足で不明)" if hs else "不明")
    # ② 指値はルール④に合わせる: 高値後の最安値(前の安値)から+10%以上、かつ今の値段以下
    post_low = min(x[1] for x in after)
    floor = post_low * (1 + pc.get("limit_above_low_pct", 10) / 100)
    if cur[1] < floor:
        return None, f"前の安値に近すぎる(安値から+{(cur[1] / post_low - 1) * 100:.0f}%・基準+{pc.get('limit_above_low_pct', 10)}%)"
    limit = min(max(lo + (hi - lo) * pc["limit_position"], floor), cur[1])
    sl = min(lo, limit) * (1 - pc["sl_below_range_pct"] / 100)
    return {
        "peak": peak[1], "peak_ts": peak[0], "cur": cur[1], "drop": drop, "mins_to_drop": mins_to_drop,
        "lo": lo, "hi": hi, "band": band, "span": span, "n": len(win),
        "post_low": post_low, "holders_note": holders_note,
        "limit": limit, "sl": sl, "tp": limit * (1 + pc["tp_pct"] / 100),
    }, ""


SECTION_TIER = {"スキャナー枠": "🎯", "生まれたて枠": "🎯生", "すき間枠": "🎯隙"}


def section_of(v, check_lp=True):
    """3つの区分。戻り値: (区分名 or None, 理由)"""
    c = CFG["criteria_a"]
    mc, liq, age = v.get("mcap"), v.get("liquidity") or 0, v.get("age_min")
    if not mc:
        return None, "時価総額が不明"
    if mc > (c.get("max_mcap_usd") or float("inf")):
        return None, "時価総額$250K超"
    if check_lp and lp_unsafe(v):
        return None, "🚨流動性>時価総額でLP未ロック"
    young = age is not None and age <= (CFG.get("sections") or {}).get("newborn_max_age_hours", 24) * 60
    if v["category"] == "A" and mc >= (c.get("min_mcap_usd") or 0) and liq >= c["min_liquidity_usd"]:
        return "スキャナー枠", ""
    if v["category"] == "B" or mc < (c.get("min_mcap_usd") or 0):
        return ("生まれたて枠" if young else "すき間枠"), ""
    return "すき間枠", ""


def section_pc(pc, section):
    """区分ごとの🎯設定(基本設定に上書き)"""
    return dict(pc, **(((CFG.get("sections") or {}).get(section) or {}).get("pattern") or {}))


def section_safety(v, section):
    """生まれたて枠・すき間枠の安全チェック。戻り値: 不合格の理由リスト"""
    sc = ((CFG.get("sections") or {}).get(section) or {}).get("safety") or {}
    fails = []
    if not v["rc_checked"]:
        return ["RugCheck未取得"]
    if v["top10"] is not None and v["top10"] > sc.get("max_top10_pct", 40):
        fails.append(f"上位10件{v['top10']:.0f}%")
    if v.get("top1") is not None and v["top1"] > sc.get("max_single_holder_pct", 10):
        fails.append(f"1人で{v['top1']:.0f}%")
    if v["insider_pct"] is not None and v["insider_pct"] > sc.get("max_insider_pct", 10):
        fails.append(f"インサイダー{v['insider_pct']:.0f}%")
    if v["bundle"] is not None and v["bundle"] > sc.get("max_bundle_pct", 20):
        fails.append(f"バンドル{v['bundle']:.0f}%")
    if v["dev_pct"] is not None and v["dev_pct"] > sc.get("max_dev_pct", 5):
        fails.append(f"開発者{v['dev_pct']:.0f}%")
    if v.get("holders") is not None and v["holders"] < sc.get("min_holders", 100):
        fails.append(f"保有者{v['holders']}人")
    if v.get("transfer_fee_pct"):
        fails.append(f"送金手数料{v['transfer_fee_pct']:.1f}%")
    return fails


def pattern_can_notify(prev, info, pc, now=None):
    """同じ銘柄の再通知: 間隔があいたとき、または1回目の横ばいが崩れて新しい安値で横ばいになったとき(2回目の横ばい)"""
    now = now or now_ts()
    if not prev:
        return True, ""
    if isinstance(prev, (int, float)):
        prev = {"ts": prev}
    if now - prev.get("ts", 0) >= pc["renotify_hours"] * 3600:
        return True, ""
    plo = prev.get("lo")
    if plo and info["post_low"] < plo * (1 - pc.get("second_range_drop_pct", 5) / 100) \
            and now - prev.get("ts", 0) >= pc.get("second_range_min_minutes", 30) * 60:
        return True, "2回目の横ばい"
    return False, ""


def pattern_payload(v, info, pc, section="スキャナー枠"):
    mint = v["mint"]
    tier = SECTION_TIER.get(section, "🎯")
    if pc.get("tp_steps"):
        exits = " ｜ ".join(f"利確{m}倍 {fmt_usd(info['limit'] * m)}({what})" for m, what in pc["tp_steps"])
    else:
        exits = f"利確 {fmt_usd(info['tp'])}(+{pc['tp_pct']}%)"
    if pc.get("sl_from_entry_pct"):
        info["sl"] = info["limit"] * (1 - pc["sl_from_entry_pct"] / 100)
    extra = pc.get("note") or ""
    links = (f"<https://gmgn.ai/sol/token/{mint}|GMGN> ｜ <https://dexscreener.com/solana/{mint}|DEX Screener> ｜ "
             f"<https://rugcheck.xyz/tokens/{mint}|RugCheck>")
    body = (
        f"*{v['name']} ({v['symbol']})*\n`{mint}`\n"
        f"高値 {fmt_usd(info['peak'])}({jst(info['peak_ts'])[11:16]}) → 今 {fmt_usd(info['cur'])}(−{info['drop']:.0f}%)\n"
        f"横ばい: {fmt_usd(info['lo'])}〜{fmt_usd(info['hi'])}(±{info['band']:.0f}%・約{info['span']:.0f}分・記録{info['n']}回) ｜ 高値後の最安値: {fmt_usd(info['post_low'])}\n"
        f"流動性: {fmt_usd(v['liquidity'])} ｜ 保有者: {info['holders_note']}\n"
        f"値動き: 5分 {fmt_pct(v['pc_m5'], True)} ｜ 1時間 {fmt_pct(v['pc_h1'], True)} ｜ "
        f"上位10件: {fmt_pct(v['top10'])} ｜ 開発者: {fmt_pct(v['dev_pct'])} ｜ LP: {v.get('lp_status') or '不明'}\n\n"
        f"*目安*({(CFG.get('paper_trading') or {}).get('amount_sol', 0.05)} SOL・期限1時間): 指値 {fmt_usd(info['limit'])}(最安値+{(info['limit'] / info['post_low'] - 1) * 100:.0f}%) ｜ 損切り {fmt_usd(info['sl'])} ｜ {exits}\n"
        f"{extra}"
        f"⚠️ *発注前チェック*: GMGNの5分足で ②実体が安値を更新していない ③直前の足が−15%以上の大陰線でない "
        f"④指値が前の安値(ヒゲ)から5%以上上 ⑤滑っても耐えられる を確認。記録は約15分ごとでヒゲは見えません\n\n{links}"
    )
    return {
        "text": f"*🎯 横ばい中【{section}】{'・' + info['label'] if info.get('label') else ''} — {v['symbol']}*",
        "attachments": [{
            "color": TIER_STYLE[tier]["color"],
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{jst()} JST ｜ 投資判断はご自身で"}]},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# 記録(CSV)
# ---------------------------------------------------------------------------
LOG_FIELDS = ["日時(JST)", "判定", "合計点", "点数の内訳", "理由", "区分", "シンボル", "名前", "アドレス", "検出元",
              "経過(分)", "時価総額USD", "流動性USD", "価格USD", "5分%", "1時間%", "6時間%",
              "バンドル%", "スナイパー%", "上位10%", "開発者%", "保有者数", "卒業進捗%", "インサイダー数", "LP状態", "テーマ", "便乗の疑い", "除外したプール口座"]
CAND_FIELDS = ["通知日時(JST)", "通知ts", "段階", "合計点", "区分", "シンボル", "名前", "アドレス",
               "通知時価格", "通知時時価総額", "通知時流動性", "テーマ", "ナラティブ",
               "1時間後価格", "1時間後%", "6時間後価格", "6時間後%", "24時間後価格", "24時間後%", "DEX Screener"]
PAPER_CSV = os.path.join(DATA, "paper_trades.csv")
PAPER_FIELDS = ["購入日時(JST)", "購入ts", "段階", "合計点", "区分", "シンボル", "名前", "アドレス", "テーマ",
                "購入価格", "購入額USD", "状態", "残り割合", "利確済み段階", "売却済みUSD", "最終確認価格",
                "最高騰落%", "最低騰落%", "売却履歴", "決済日時(JST)", "決済ts", "決済理由", "損益USD", "損益%", "損益SOL"]
CHECKPOINTS = [("1時間後", 3600), ("6時間後", 6 * 3600), ("24時間後", 24 * 3600)]


def r1(x):
    return "" if x is None else round(x, 2)


def append_log(rows, dry):
    if not rows:
        return
    path = os.path.join(DATA, f"screening_{dt.datetime.now(JST):%Y-%m}.csv")
    if dry:
        log(f"  (テスト実行のため {len(rows)} 行の記録は保存しません)")
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def log_row(v, res):
    return {
        "日時(JST)": jst(), "判定": res["verdict"], "合計点": "" if res["score"] is None else res["score"],
        "点数の内訳": " / ".join(f"{l}+{p}" for l, p in res["breakdown"]),
        "理由": " / ".join(res["reasons"]), "区分": v["category"], "シンボル": v["symbol"], "名前": v["name"],
        "アドレス": v["mint"], "検出元": v["source"], "経過(分)": r1(v["age_min"]),
        "時価総額USD": r1(v["mcap"]), "流動性USD": r1(v["liquidity"]), "価格USD": fmt_price(v["price"]),
        "5分%": r1(v["pc_m5"]), "1時間%": r1(v["pc_h1"]), "6時間%": r1(v["pc_h6"]),
        "バンドル%": r1(v["bundle"]), "スナイパー%": r1(v["sniper"]), "上位10%": r1(v["top10"]),
        "開発者%": r1(v["dev_pct"]), "保有者数": v["holders"] if v["holders"] is not None else "",
        "卒業進捗%": r1(v["curve"]), "インサイダー数": v["insider_count"],
        "LP状態": v.get("lp_status", ""),
        "テーマ": "・".join(detect_themes(v)),
        "便乗の疑い": "あり" if is_copycat(v) else "",
        "除外したプール口座": " / ".join(f"{p}%({how})" for p, how in v.get("pool_holders") or []),
    }


def read_candidates():
    if not os.path.exists(CANDIDATES_CSV):
        return []
    with open(CANDIDATES_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_candidates(rows, dry):
    if dry:
        return
    with open(CANDIDATES_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CAND_FIELDS)
        w.writeheader()
        w.writerows(rows)


def update_followups(http, rows):
    """通知から1時間・6時間・24時間たった候補の価格を記録(大きく遅れた場合は「取得漏れ」)"""
    due = {}
    for r in rows:
        ts = fnum(r.get("通知ts"))
        if not ts:
            continue
        for label, sec in CHECKPOINTS:
            if r.get(f"{label}価格"):
                continue
            late = now_ts() - ts - sec
            if late < 0:
                continue
            if late > max(2700, sec * 0.25):
                r[f"{label}価格"] = "取得漏れ"
                continue
            due.setdefault(r["アドレス"], []).append((r, label))
    if not due:
        return 0
    pairs = ds_pairs(http, due.keys())
    n = 0
    for mint, items in due.items():
        price = (summarize_pair(pairs.get(mint)) or {}).get("price")
        if price is None:
            continue
        for r, label in items:
            base = fnum(r.get("通知時価格"))
            r[f"{label}価格"] = fmt_price(price)
            r[f"{label}%"] = r1((price / base - 1) * 100) if base else ""
            n += 1
    return n


def performance_text(rows):
    lines = []
    for tier in ("小", "中", "大", "🎯", "🎯生", "🎯隙", "📉"):
        rs = [r for r in rows if r.get("段階") == tier]
        if not rs:
            continue
        parts = []
        for label, _ in CHECKPOINTS:
            vals = [fnum(r.get(f"{label}%")) for r in rs]
            vals = [x for x in vals if x is not None]
            if vals:
                win = sum(1 for x in vals if x > 0) / len(vals) * 100
                parts.append(f"{label} 平均{sum(vals) / len(vals):+.1f}%(上昇{win:.0f}%・{len(vals)}件)")
        label = {"🎯": "横ばい・スキャナー枠", "🎯生": "横ばい・生まれたて枠", "🎯隙": "横ばい・すき間枠",
                 "📉": "急落(5分足で確認)"}.get(tier, f"リスク{tier}")
        lines.append(f"{TIER_STYLE[tier]['emoji']} {label}: 通知{len(rs)}件 ｜ " + (" ／ ".join(parts) or "まだ集計なし"))
    return "\n".join(lines) or "まだ候補の記録がありません"


# ---------------------------------------------------------------------------
# 仮想売買(ペーパートレード)。記録と損益計算だけで、実際の売買は一切しない
# ---------------------------------------------------------------------------
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows, dry):
    if dry:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def sol_price(http, state):
    """SOL の米ドル価格(1日の損失上限を SOL で判定するため)。15分間は使い回す"""
    c = state.get("sol_usd") or {}
    if c and now_ts() - c.get("ts", 0) < 900:
        return c.get("p")
    p = (summarize_pair(ds_pairs(http, [SOL_MINT]).get(SOL_MINT)) or {}).get("price")
    if p:
        state["sol_usd"] = {"ts": now_ts(), "p": p}
    return p or c.get("p")


def paper_today(rows):
    """今日(日本時間)の仮想エントリー回数と、今日決済した分の損益(USD)"""
    today = dt.datetime.now(JST).strftime("%Y-%m-%d")
    entries = sum(1 for r in rows if (r.get("購入日時(JST)") or "").startswith(today))
    pnl = sum(float(r.get("損益USD") or 0) for r in rows
              if r.get("状態") == "決済済み" and (r.get("決済日時(JST)") or "").startswith(today))
    return entries, pnl


def paper_limit():
    pc = CFG["paper_trading"]
    lim = pc.get("max_entries_per_day") or {}
    weekend = dt.datetime.now(JST).weekday() >= 5
    return lim.get("weekend" if weekend else "weekday"), ("土日" if weekend else "平日")


def paper_amount_usd(sol_usd):
    """仮想購入額(USD)。amount_sol があれば SOL 価格で換算、なければ amount_usd"""
    pc = CFG["paper_trading"]
    if pc.get("amount_sol") and sol_usd:
        return round(pc["amount_sol"] * sol_usd, 2)
    return pc.get("amount_usd", 20)


def paper_check(rows, v, res, sol_usd):
    """仮想購入するかどうか。戻り値: (買うか, 通知に出す一言)"""
    pc = CFG.get("paper_trading") or {}
    if not pc.get("enabled") or res["tier"] not in pc.get("tiers", []):
        return False, ""
    if not v.get("price"):
        return False, "価格が取れないため見送り"
    if any(r["アドレス"] == v["mint"] and r["状態"] == "保有中" for r in rows):
        return False, "同じ銘柄を仮想保有中のため見送り(同じ銘柄は同時に1つまで)"
    entries, pnl = paper_today(rows)
    cap, kind = paper_limit()
    if cap is not None and entries >= cap:
        return False, f"本日の上限({kind}{cap}回)に達したため見送り"
    limit_sol = pc.get("daily_loss_limit_sol")
    if limit_sol and pnl < 0:
        if sol_usd and -pnl >= limit_sol * sol_usd:
            return False, f"本日の損失上限({limit_sol} SOL)に達したため見送り"
    cap_txt = f"・本日{entries + 1}/{cap}回目" if cap is not None else ""
    amt = paper_amount_usd(sol_usd)
    v["_paper_usd"] = amt
    sol_txt = f"({pc['amount_sol']} SOL相当)" if pc.get("amount_sol") and sol_usd else ""
    return True, f"${amt} 分{sol_txt}を仮想購入{cap_txt}"


def paper_open(rows, v, res):
    """候補の通知時に仮想購入(paper_check を通ったものだけ)"""
    pc = CFG["paper_trading"]
    rows.append({
        "購入日時(JST)": jst(), "購入ts": int(now_ts()), "段階": res["tier"], "合計点": res["score"],
        "区分": v["category"], "シンボル": v["symbol"], "名前": v["name"], "アドレス": v["mint"],
        "テーマ": "・".join(detect_themes(v)), "購入価格": fmt_price(v["price"]), "購入額USD": v.get("_paper_usd") or pc.get("amount_usd", 20),
        "状態": "保有中", "残り割合": 1, "利確済み段階": 0, "売却済みUSD": 0, "最終確認価格": fmt_price(v["price"]),
        "最高騰落%": 0, "最低騰落%": 0, "売却履歴": "",
    })
    return True


def paper_step(r, price, now, sol_usd=None):
    """1件の仮想ポジションを、今の価格でルールどおりに処理する"""
    pc = CFG["paper_trading"]
    fee = pc.get("fee_pct", 0) / 100
    entry, amount = float(r["購入価格"]), float(r["購入額USD"])
    rest, stage = float(r["残り割合"]), int(float(r["利確済み段階"] or 0))
    proceeds = float(r["売却済みUSD"] or 0)
    hist = [h for h in (r["売却履歴"] or "").split(" → ") if h]
    minutes = (now - float(r["購入ts"])) / 60
    chg = (price / entry - 1) * 100
    r["最終確認価格"] = fmt_price(price)
    r["最高騰落%"] = r1(max(float(r["最高騰落%"] or 0), chg))
    r["最低騰落%"] = r1(min(float(r["最低騰落%"] or 0), chg))
    reasons = []

    def sell(frac, label):
        nonlocal rest, proceeds
        frac = min(frac, rest)
        if frac <= 0:
            return
        proceeds += frac * amount * (1 - fee) * (price / entry) * (1 - fee)
        rest -= frac
        hist.append(f"{fmt_age(minutes)}後 {chg:+.0f}% {label} {frac * 100:.0f}%")
        reasons.append(label)

    if chg <= pc["stop_loss_pct"]:
        sell(rest, "損切り")
    else:
        for i, tp in enumerate(pc["take_profit"]):
            if stage <= i and chg >= tp["pct"]:
                sell(max(0.0, tp["sell_ratio"] - (1 - rest)), f"+{tp['pct']}%利確")
                stage = i + 1
    if rest > 1e-9 and minutes >= pc["time_exit_minutes"]:
        sell(rest, "時間撤退")
    r.update({"残り割合": round(rest, 6), "利確済み段階": stage, "売却済みUSD": round(proceeds, 4),
              "売却履歴": " → ".join(hist)})
    if rest <= 1e-9:
        pnl = proceeds - amount
        r.update({"状態": "決済済み", "決済日時(JST)": jst(now), "決済ts": int(now),
                  "決済理由": reasons[-1] if reasons else "", "損益USD": round(pnl, 2),
                  "損益%": round(pnl / amount * 100, 1),
                  "損益SOL": round(pnl / sol_usd, 4) if sol_usd else ""})


def paper_update(http, rows, started, state):
    pc = CFG.get("paper_trading") or {}
    if not pc.get("enabled"):
        return 0
    open_rows = [r for r in rows if r["状態"] == "保有中" and float(r["購入ts"]) < started]
    if not open_rows:
        return 0
    pairs = ds_pairs(http, {r["アドレス"] for r in open_rows})
    now, closed = now_ts(), 0
    sol = sol_price(http, state)
    for r in open_rows:
        price = (summarize_pair(pairs.get(r["アドレス"])) or {}).get("price")
        if price is None:
            price = ((state.get("st_cache", {}).get(r["アドレス"]) or {}).get("d") or {}).get("price")
        if price is None:
            # 価格が取れないまま期限を大きく過ぎたら、最後に確認できた価格で決済
            if (now - float(r["購入ts"])) / 60 >= pc["time_exit_minutes"] + 60:
                paper_step(r, float(r["最終確認価格"]), now, sol)
                r["決済理由"] = "価格取得不可(最終価格で決済)"
            continue
        paper_step(r, price, now, sol)
        closed += r["状態"] == "決済済み"
    return closed


def paper_text(rows, since):
    pc = CFG.get("paper_trading") or {}
    if not pc.get("enabled"):
        return ""
    done = [r for r in rows if r["状態"] == "決済済み"]
    recent = [r for r in done if float(r.get("決済ts") or 0) >= since]
    opened = sum(1 for r in rows if r["状態"] == "保有中")

    def stat(rs):
        if not rs:
            return "なし"
        pnl = [float(r["損益USD"]) for r in rs]
        win = sum(1 for x in pnl if x > 0)
        return (f"{len(rs)}件 ｜ 勝ち{win}・負け{len(rs) - win}(勝率{win / len(rs) * 100:.0f}%) ｜ "
                f"損益合計 ${sum(pnl):+.2f} ｜ 1件平均 {sum(float(r['損益%']) for r in rs) / len(rs):+.1f}%")

    lines = [f"今回の期間: {stat(recent)}", f"累計: {stat(done)}"]
    for tier in ("小", "中", "大"):
        rs = [r for r in done if r["段階"] == tier]
        if rs:
            lines.append(f"  {TIER_STYLE[tier]['emoji']} リスク{tier}: {stat(rs)}")
    reasons = Counter(r["決済理由"] for r in done)
    if reasons:
        lines.append("決済理由(累計): " + "、".join(f"{k} {n}件" for k, n in reasons.most_common()))
    entries, pnl = paper_today(rows)
    cap, kind = paper_limit()
    lines.append(f"本日の仮想エントリー: {entries}/{cap}回({kind}) ｜ 本日の確定損益 ${pnl:+.2f}"
                 f"(損失上限 {pc.get('daily_loss_limit_sol')} SOL)")
    lines.append(f"保有中の仮想ポジション: {opened}件")
    return (f"*🧪 仮想売買の成績*({(str(pc['amount_sol']) + ' SOL相当') if pc.get('amount_sol') else ('$' + str(pc.get('amount_usd', 20)))}ずつ・手数料等{pc.get('fee_pct', 0)}%/回を差し引き・実際の売買なし)\n"
            + "\n".join(lines))


# ---------------------------------------------------------------------------
# 状態(state.json)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, dry):
    if dry:
        return
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))


def prune_state(state):
    t = now_ts()
    rt = CFG["runtime"]
    watch = state.setdefault("watch", {})
    for k in [k for k, w in watch.items() if t - w.get("first", t) > rt["watch_hours"] * 3600]:
        watch.pop(k)
    if len(watch) > rt["max_watch"]:
        for k in sorted(watch, key=lambda k: watch[k].get("first", 0))[:len(watch) - rt["max_watch"]]:
            watch.pop(k)
    state.pop("_news_calls", None)
    limits = {"rc_cache": rt["watch_hours"] * 3600, "st_cache": rt["st_cache_minutes"] * 60,
              "news_cache": 24 * 3600}
    for key, limit in limits.items():
        c = state.setdefault(key, {})
        for k in [k for k, v in c.items() if t - v.get("ts", 0) > limit or k not in watch]:
            c.pop(k)
    notified = state.setdefault("notified", {})
    for k in [k for k, v in notified.items() if t - v.get("ts", 0) > 7 * 86400]:
        notified.pop(k)
    perm = state.setdefault("perm_excluded", {})
    for k in [k for k, ts in perm.items() if t - ts > 7 * 86400]:
        perm.pop(k)


def daily_summary(state, cand_rows, dry, paper_rows=()):
    s = state.setdefault("summary", {"since": now_ts(), "skip": {}, "excluded": {}, "cands": {}})
    today = dt.datetime.now(JST)
    if today.hour < CFG["notify"]["daily_summary_hour_jst"] or s.get("last_date") == today.strftime("%Y-%m-%d"):
        return
    skip_reasons = Counter(s["skip"].values())
    ex_reasons = Counter(s["excluded"].values())
    top = "\n".join(f"• {r}: {n}件" for r, n in skip_reasons.most_common(6)) or "• なし"
    top_ex = "、".join(f"{r}({n})" for r, n in ex_reasons.most_common(3)) or "なし"
    cands = Counter(s["cands"].values())
    usage = state.get("st_usage", {})
    au = state.get("api_usage", {})
    runs = max(1, au.get("runs", 0))
    api_line = " ／ ".join(f"{k} {au[k]:,}回(1回平均{au[k] / runs:.1f})" for k in sorted(au) if k not in ("month", "runs"))
    text = (
        f"*📋 1日のまとめ({jst(s['since'])} 〜 {jst()})*\n"
        f"候補: 🟢小 {cands.get('小', 0)} ／ 🟡中 {cands.get('中', 0)} ／ 🔴大 {cands.get('大', 0)} 銘柄\n"
        f"見送り: {len(s['skip'])}銘柄 ／ 除外: {len(s['excluded'])}銘柄(主な理由: {top_ex})\n\n"
        f"*見送りの主な理由*(銘柄ごとに最初の理由で集計)\n{top}\n\n"
        f"*段階ごとの成績(通知後の値動き・全期間)*\n{performance_text(cand_rows)}\n\n"
        + (paper_text(paper_rows, s["since"]) + "\n\n" if paper_text(paper_rows, s["since"]) else "") +
        f"*今月のAPI呼び出し*(実行{runs}回): {api_line or 'なし'}\n"
        f"Solana Tracker 今月の使用回数: {usage.get('count', 0)} / {CFG['solana_tracker']['monthly_budget']}"
    )
    # Slack の1ブロックは3,000文字まで
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": part}}
              for part in [p for p in text.split("\n\n") if p.strip()]]
    ok = slack_send({"text": "1日のまとめ", "attachments": [{"color": "#6B7280", "blocks": blocks[:40]}]}, dry)
    if ok:
        state["summary"] = {"since": now_ts(), "skip": {}, "excluded": {}, "cands": {}, "last_date": today.strftime("%Y-%m-%d")}


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def run(dry):
    os.makedirs(DATA, exist_ok=True)
    http = Http()
    state = load_state()
    prune_state(state)
    st_api = SolanaTracker(http, state)
    if not st_api.key:
        log("⚠ SOLANATRACKER_API_KEY が未設定です。pump.fun卒業前の取得とバンドル率の審査ができません。")
    state["runs"] = state.get("runs", 0) + 1
    t = now_ts()
    rt = CFG["runtime"]
    pc = CFG.get("pattern") or {}
    watch = state["watch"]

    # 1) 候補を集める ------------------------------------------------------
    new, descs = ds_collect(http)
    log(f"DEX Screener から {len(new)} 銘柄")
    st_raw = {}
    grad_complete = False
    pf = CFG["sources"]["pumpfun"]
    if pf.get("enabled") and state["runs"] % max(1, pf.get("every_n_runs", 1)) == 0 and st_api.can():
        for ti in st_api.graduating():
            mint = (ti.get("token") or {}).get("mint")
            if mint:
                st_raw[mint] = ti
                new.setdefault(mint, "pump.fun卒業前")
        # 一覧が上限件数に届いていなければ「一覧にない pump.fun 銘柄 = 進捗が範囲外」と判断できる
        grad_complete = 0 < len(st_raw) < pf["limit"]
        log(f"pump.fun卒業前 {len(st_raw)} 銘柄")
    # Solana Tracker のトレンド・卒業済み一覧(C基準の主な探し場所)
    default_labels = {"trending": "STトレンド", "graduated": "ST卒業済み"}
    for key, sc in (CFG["sources"] or {}).items():
        if not isinstance(sc, dict) or not sc.get("path"):
            continue
        label = sc.get("label") or default_labels.get(key, key)
        n = max(1, sc.get("every_n_runs", 1))
        if not sc.get("enabled") or (state["runs"] + sc.get("offset", 0)) % n != 0 or not st_api.can():
            continue
        lst = st_api.token_list(sc["path"])[: sc.get("limit", 100)]
        for ti in lst:
            mint = (ti.get("token") or {}).get("mint")
            if mint:
                st_raw[mint] = ti
                new.setdefault(mint, label)
        log(f"{label} {len(lst)} 銘柄")
    # 発行元が Pump.Fun でないと判定済みの銘柄は、7日間は調べ直さない
    rows = []
    summ = state.setdefault("summary", {"since": t, "skip": {}, "excluded": {}, "cands": {}})
    perm = state.setdefault("perm_excluded", {})
    for mint in [m for m in new if m in perm]:
        new.pop(mint)

    for mint, src in new.items():
        w = watch.setdefault(mint, {"first": t, "src": src})
        if src == "pump.fun卒業前":
            w["src"] = src
        if descs.get(mint) and not w.get("desc"):
            w["desc"] = descs[mint]

    # 2) 今回チェックする銘柄を選ぶ ------------------------------------------
    todo = []
    for mint, w in watch.items():
        last = w.get("checked", 0)
        if w.get("verdict") == "除外":
            due = t - last >= rt["excluded_recheck_hours"] * 3600
        else:
            due = mint in st_raw or t - last >= rt["recheck_minutes"] * 60 or not last
            # 高値をつけた銘柄は短い間隔で記録(🎯検出用)。実行間隔のずれを見込んで2分早めに
            if pc.get("enabled") and w.get("pw", 0) > t and t - last >= pc["recheck_minutes"] * 60 - 120:
                due = True
        if due:
            todo.append(mint)
    todo.sort(key=lambda m: (watch[m].get("checked", 0), -watch[m]["first"]))
    log(f"今回チェック: {len(todo)} 銘柄(追跡中 {len(watch)})")

    pairs = ds_pairs(http, todo)
    for mint, ti in st_raw.items():
        state["st_cache"][mint] = {"ts": t, "d": summarize_st(ti)}

    rc_calls = 0
    cand_rows = read_candidates()
    paper_rows = read_csv(PAPER_CSV)
    notified = state["notified"]
    sent = 0

    def get_rc(mint, fresh=False):
        """RugCheck。権限・拡張機能はほぼ変わらないので、追跡中は結果を使い回す。
        fresh=True(候補になりそう・除外の再確認)のときだけ、古ければ取り直す"""
        nonlocal rc_calls
        c = state["rc_cache"].get(mint)
        max_age = rt["rugcheck_fresh_minutes"] * 60 if fresh else rt["watch_hours"] * 3600
        if c and now_ts() - c["ts"] <= max_age:
            return c["d"]
        if rc_calls >= rt["rugcheck_max_per_run"] or time_left() < 30:
            return c["d"] if c else None
        rc_calls += 1
        d = summarize_rugcheck(http.call(f"{RC}/tokens/{mint}/report", min_interval=rt["rugcheck_interval_sec"]))
        if d:
            state["rc_cache"][mint] = {"ts": now_ts(), "d": d}
            return d
        return c["d"] if c else None

    def view(mint, src, pair, rc, st, ads=None):
        return build_view(mint, src, pair, rc, st, ads, not st_api.can(),
                          grad_complete and mint not in st_raw, watch.get(mint, {}).get("desc", ""))

    def evaluate(mint, src, pair, rc, st):
        """RugCheck が必要なら取り、候補になりそうなら最新の RugCheck で確定する"""
        v = view(mint, src, pair, rc, st)
        res = classify(v)
        if "rc" in res["need"] and rc is None:
            rc = get_rc(mint)
            if rc is None:
                return None
            v = view(mint, src, pair, rc, st)
            res = classify(v)
        if res["tier"]:
            rc2 = get_rc(mint, fresh=True)
            if rc2 is not rc:
                rc = rc2
                v = view(mint, src, pair, rc, st)
                res = classify(v)
        return [src, pair, rc, st, v, res]

    # 1巡目(Solana Tracker が必要な銘柄は、あとで20件ずつまとめて1回で取る)
    pending_st = []
    results = {}
    for mint in todo:
        if time_left() < 20:
            log("時間切れのため残りは次回に回します")
            break
        src = watch[mint]["src"]
        pair = pairs.get(mint)
        st = (state["st_cache"].get(mint) or {}).get("d")
        if not pair and not st:
            continue  # まだ DEX に載っていない → 次回
        pre = bool(st and st.get("pre_grad"))
        rc = None
        if not pre:
            ps = summarize_pair(pair) or {}
            # 流動性が基準未満の卒業済み銘柄は、RugCheck を呼ばずに見送り(pump.fun 卒業前は対象外)
            suffix = CFG["mandatory"].get("require_address_suffix") or ""
            skip_rc = (rt.get("rugcheck_skip_below_liquidity") and pair is not None
                       and (not suffix or mint.endswith(suffix))
                       and ps.get("dex_id") != "pumpfun" and mint not in state["rc_cache"]
                       and (ps.get("liquidity") or 0) < CFG["criteria_a"]["min_liquidity_usd"])
            if not skip_rc:
                rc = get_rc(mint, fresh=watch[mint].get("verdict") == "除外")
                if rc is None:
                    continue  # RugCheck が取れない → 次回
        r = evaluate(mint, src, pair, rc, st)
        if r is None:
            continue
        if "st" in r[5]["need"] and st is None:
            pending_st.append(mint)
        results[mint] = r

    if pending_st and st_api.can():
        got = st_api.multi(pending_st)
        for mint in pending_st:
            d = summarize_st(got.get(mint))
            if d:
                state["st_cache"][mint] = {"ts": now_ts(), "d": d}
                src, pair, rc, _, _, _ = results[mint]
                r = evaluate(mint, src, pair, rc, d)
                if r:
                    results[mint] = r

    # 3) 候補は広告の有無を確認して点数を確定 → 通知 ------------------------------
    drop = []
    for mint, (src, pair, rc, st, v, res) in results.items():
        if res["tier"] and not (v["has_ads"]):
            ads = ds_orders(http, mint)
            if ads:
                v = view(mint, src, pair, rc, st, ads)
                res = classify(v)
        w = watch[mint]
        prev_verdict = w.get("verdict")
        w.update({"checked": now_ts(), "verdict": res["verdict"]})
        if pc.get("enabled") and res["verdict"] != "除外" and v["mcap"]:
            ps_ = summarize_pair(pair) or {}
            # 卒業前は Solana Tracker の値が古いことがあるので、DEX Screener の値を優先
            record_hist(w, v, now_ts(), pc, first(ps_.get("mcap"), v["mcap"]), first(ps_.get("liquidity") or None, v.get("liquidity")))
        if prev_verdict != res["verdict"]:
            rows.append(log_row(v, res))
        main_reason = res["reasons"][0] if res["reasons"] else ""
        if res["verdict"] == "除外":
            summ["excluded"][mint] = main_reason
            if res.get("permanent"):
                perm[mint] = now_ts()   # 発行元が対象外 → 追跡をやめ、7日間は調べ直さない
                drop.append(mint)
        elif res["verdict"] == "見送り":
            if main_reason not in ("審査データ待ち", "発行元の確認待ち(RugCheck)"):
                summ["skip"][mint] = main_reason
        elif res["tier"]:
            summ["skip"].pop(mint, None)
            summ["cands"][mint] = res["tier"]
            prev = notified.get(mint)
            fresh = not prev or now_ts() - prev["ts"] > CFG["notify"]["renotify_hours"] * 3600
            changed = prev and prev.get("tier") != res["tier"]
            if (fresh or changed) and res["tier"] in CFG["notify"]["notify_tiers"]:
                buy, pmsg = False, ""
                if (CFG.get("paper_trading") or {}).get("enabled"):
                    buy, pmsg = paper_check(paper_rows, v, res, sol_price(http, state))
                narr = narrative(v, state)
                if slack_send(candidate_payload(v, res, prev.get("tier") if changed else None, pmsg, narr), dry):
                    sent += 1
                    notified[mint] = {"ts": now_ts(), "tier": res["tier"]}
                    cand_rows.append({
                        "通知日時(JST)": jst(), "通知ts": int(now_ts()), "段階": res["tier"], "合計点": res["score"],
                        "区分": v["category"], "シンボル": v["symbol"], "名前": v["name"], "アドレス": mint,
                        "通知時価格": fmt_price(v["price"]), "通知時時価総額": r1(v["mcap"]),
                        "通知時流動性": r1(v["liquidity"]), "テーマ": "・".join(detect_themes(v)),
                        "ナラティブ": narr[2] if narr else "",
                        "DEX Screener": f"https://dexscreener.com/solana/{mint}",
                    })
                    if prev_verdict == res["verdict"]:
                        rows.append(log_row(v, res))
                    if buy:
                        paper_open(paper_rows, v, res)

    for mint in drop:
        watch.pop(mint, None)

    # 3b) 🎯 勝ちパターン(急騰→急落→横ばい) -----------------------------------------
    if pc.get("enabled"):
        pn = state.setdefault("pattern_notified", {})
        for k in [k for k, x in pn.items() if now_ts() - (x["ts"] if isinstance(x, dict) else x) > 7 * 86400]:
            pn.pop(k)
        hits, why, n_pat = [], Counter(), 0
        dc = CFG.get("dip_alert") or {}
        dn = state.setdefault("dip_notified", {})
        for k in [k for k, ts in dn.items() if now_ts() - ts > 7 * 86400]:
            dn.pop(k)
        dips = []
        for mint, (src, pair, rc, st, v, res) in results.items():
            w = watch.get(mint)
            if not w or res["verdict"] == "除外" or len(w.get("h", [])) < 3:
                continue
            sec, _ = section_of(v, check_lp=False)
            if not sec:
                continue
            spc = section_pc(pc, sec)
            info, reason = pattern_eval(w["h"], v, spc)
            if info:
                ok, label = pattern_can_notify(pn.get(mint), info, spc)
                if ok:
                    info["label"] = label
                    info["kind"] = "🎯"
                    hits.append([mint, info])
                    continue
            else:
                why[reason.split("(")[0]] += 1
            # 📉 急落(横ばいの判定は5分足で人が行う)
            if dc.get("enabled") and len(dips) < dc.get("max_per_run", 10) \
                    and now_ts() - dn.get(mint, 0) >= dc.get("renotify_hours", 6) * 3600 \
                    and now_ts() - ((pn.get(mint) or {}).get("ts", 0) if isinstance(pn.get(mint), dict) else (pn.get(mint) or 0)) >= 3600:
                dinfo, _ = dip_eval(w["h"], v, spc)
                if dinfo:
                    dinfo["kind"] = "📉"
                    dips.append([mint, dinfo])
        # 形が合った銘柄だけ、急騰の判定(6時間+100%)を除いた C基準と安全性を最新の情報で確認
        hits = hits + dips
        need_st = [m for m, _ in hits if not (state["st_cache"].get(m) or {}).get("d")]
        if need_st and st_api.can():
            got = st_api.multi(need_st)
            for m in need_st:
                d = summarize_st(got.get(m))
                if d:
                    state["st_cache"][m] = {"ts": now_ts(), "d": d}
        for mint, info in hits:
            src, pair, _, _, _, _ = results[mint]
            rc = get_rc(mint, fresh=True)
            st = (state["st_cache"].get(mint) or {}).get("d")
            v = view(mint, src, pair, rc, st)
            ps_ = summarize_pair(pair) or {}
            if ps_.get("mcap"):
                v["mcap"], v["liquidity"] = ps_["mcap"], first(ps_.get("liquidity") or None, v["liquidity"])
            sec, why_sec = section_of(v)
            if not sec:
                why[why_sec] += 1
                continue
            spc = section_pc(pc, sec)
            base = classify(dict(v, pc_h6=None))
            if base["verdict"] == "除外":
                why["除外: " + (base["reasons"][0] if base["reasons"] else "?")] += 1
                continue
            if sec == "スキャナー枠" and info.get("kind") != "📉":
                if not base["tier"]:
                    why["C基準外: " + (base["reasons"][0] if base["reasons"] else "?")] += 1
                    continue
            elif sec != "スキャナー枠":
                fails = section_safety(v, sec)
                if fails:
                    why[f"{sec}の安全チェック: {fails[0]}"] += 1
                    continue
            score_token(v)  # LP の表示文を作る
            if (v["liquidity"] or 0) < spc["min_liquidity_usd"]:
                why["流動性不足"] += 1
                continue
            if crashed_before(v, spc):
                why["記録前の暴落"] += 1
                continue
            if info.get("kind") == "📉":
                if sec == "スキャナー枠":
                    fails = section_safety(v, "すき間枠")  # 急落時は C基準ではなく安全チェックだけで見る
                    if fails:
                        why[f"📉安全チェック: {fails[0]}"] += 1
                        continue
                if slack_send(dip_payload(v, info, spc, sec), dry):
                    sent += 1
                    dn[mint] = now_ts()
                    cand_rows.append({
                        "通知日時(JST)": jst(), "通知ts": int(now_ts()), "段階": "📉", "合計点": "",
                        "区分": f"{v['category']}・{sec}", "シンボル": v["symbol"], "名前": v["name"], "アドレス": mint,
                        "通知時価格": fmt_price(v["price"]), "通知時時価総額": r1(v["mcap"]),
                        "通知時流動性": r1(v["liquidity"]), "テーマ": "・".join(detect_themes(v)), "ナラティブ": "",
                        "DEX Screener": f"https://dexscreener.com/solana/{mint}",
                    })
                continue
            if slack_send(pattern_payload(v, info, spc, sec), dry):
                sent += 1
                n_pat += 1
                pn[mint] = {"ts": now_ts(), "lo": info["post_low"]}
                cand_rows.append({
                    "通知日時(JST)": jst(), "通知ts": int(now_ts()), "段階": SECTION_TIER.get(sec, "🎯"), "合計点": "",
                    "区分": f"{v['category']}・{sec}", "シンボル": v["symbol"], "名前": v["name"], "アドレス": mint,
                    "通知時価格": fmt_price(v["price"]), "通知時時価総額": r1(v["mcap"]),
                    "通知時流動性": r1(v["liquidity"]), "テーマ": "・".join(detect_themes(v)), "ナラティブ": "",
                    "DEX Screener": f"https://dexscreener.com/solana/{mint}",
                })
        log(f"🎯 勝ちパターン: 通知 {n_pat} 件 ｜ 📉 候補 {len(dips)} 件 ｜ 形が合わなかった理由 "
            + (", ".join(f"{k} {n}" for k, n in why.most_common(5)) or "なし"))

    counts = Counter(r[5]["verdict"] for r in results.values())
    log("判定結果: " + (", ".join(f"{k} {n}" for k, n in counts.items()) or "なし") + f" ｜ 通知 {sent} 件")

    # 4) 記録・価格の追跡・1日のまとめ ------------------------------------------
    n = update_followups(http, cand_rows)
    if n:
        log(f"候補の追跡価格を {n} 件記録")
    closed = paper_update(http, paper_rows, t, state)
    if closed:
        log(f"仮想売買: {closed} 件を決済")
    append_log(rows, dry)
    write_candidates(cand_rows, dry)
    write_csv(PAPER_CSV, PAPER_FIELDS, paper_rows, dry)
    month = dt.datetime.now(JST).strftime("%Y-%m")
    au = state.setdefault("api_usage", {})
    if au.get("month") != month:
        au.clear()
        au.update({"month": month, "runs": 0})
    au["runs"] += 1
    for k, n in http.counts.items():
        au[k] = au.get(k, 0) + n
    daily_summary(state, cand_rows, dry, paper_rows)
    save_state(state, dry)
    log(f"完了({time.time() - START:.0f}秒) 今回のAPI呼び出し: " +
        (" ／ ".join(f"{k} {n}回" for k, n in sorted(http.counts.items())) or "なし") +
        f" ｜ Solana Tracker 今月 {st_api.usage['count']}回(上限 {CFG['solana_tracker']['monthly_budget']})")


def main():
    global CFG
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-slack", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    with open(os.path.join(BASE, "config.yaml"), encoding="utf-8") as f:
        CFG = yaml.safe_load(f)
    dry = a.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"
    if a.report:
        print(performance_text(read_candidates()))
        return
    if a.test_slack or os.environ.get("TEST_SLACK", "").lower() == "true":
        ok = slack_send({"text": "✅ ミームコイン・スキャナーのテスト通知です。この通知が見えれば Slack の設定は完了です。"}, False)
        sys.exit(0 if ok else 1)
    run(dry)


CFG = {}
if __name__ == "__main__":
    main()
