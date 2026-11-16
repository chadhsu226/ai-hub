#!/usr/bin/env python3
"""AI 投資決策中樞 — 資料管線

每次執行：
  1. 抓市場資料（TWSE OpenAPI、TWSE MIS 五檔、Yahoo Finance、Binance、FRED）
  2. 抓新聞（鉅亨網 JSON、經濟日報 RSS），新進新聞交給 LLM 評情緒（無金鑰時退回詞典法）
  3. 以邏輯斯迴歸因子模型估計 TAIEX 次日上漲機率 P(up)，並做樣本外回測
  4. 規則式資產配置（每一步可追溯到輸入數字）
  5. 寫出 data/dashboard.json（前端唯一讀取檔）、data/news_cache.json、data/log.json

任何來源失敗都不會中斷整體流程：該欄位標 status=error，前端顯示「無資料」，絕不補假數字。
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TPE = timezone(timedelta(hours=8))
# 注意：Yahoo 會對完整瀏覽器 UA 字串回 429，簡短的 "Mozilla/5.0" 反而可通
UA = {"User-Agent": "Mozilla/5.0"}
NOW = datetime.now(TPE)
LOG: list[dict] = []
SOURCES: dict[str, dict] = {}


T0 = time.time()


def log(agent: str, msg: str, level: str = "info"):
    msg = f"{msg}" if agent != "SYSTEM" else f"{msg} (+{time.time() - T0:.0f}s)"
    LOG.append({"ts": datetime.now(TPE).isoformat(timespec="seconds"), "agent": agent, "msg": msg, "level": level})
    print(f"[{agent}] {msg}", file=sys.stderr)


def source(key: str, name: str, url: str, ok: bool, note: str = ""):
    SOURCES[key] = {"name": name, "url": url, "ok": ok, "note": note,
                    "fetched_at": datetime.now(TPE).isoformat(timespec="seconds")}


def get(url, *, params=None, timeout=20, retries=3, as_json=True):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                last = RuntimeError("429 rate limited")
                continue
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise last


# ───────────────────────── 市場資料 ─────────────────────────

_YT = [0.0]


def _yahoo_throttle():
    # Yahoo 對短時間連續請求會 429；每次呼叫至少間隔 1.5 秒
    dt = time.time() - _YT[0]
    if dt < 1.5:
        time.sleep(1.5 - dt)
    _YT[0] = time.time()


def yahoo_spark(symbols: list[str]):
    """一次請求取多檔報價（降低 429 機率）。回傳 {sym: {value, chg_pct, ts}}"""
    url = "https://query1.finance.yahoo.com/v8/finance/spark"
    _yahoo_throttle()
    try:
        j = get(url, params={"symbols": ",".join(symbols), "range": "5d", "interval": "1d"})
        out = {}
        for s in symbols:
            d = j.get(s)
            if not d or not d.get("close"):
                continue
            closes = [c for c in d["close"] if c is not None]
            px = d.get("fulldayPrice") or closes[-1]
            chg = d.get("fulldayChangePercent")
            if chg is None and len(closes) >= 2:
                chg = (closes[-1] / closes[-2] - 1) * 100
            out[s] = {"value": px, "chg_pct": chg, "closes": closes,
                      "ts": datetime.fromtimestamp(d["timestamp"][-1], TPE).isoformat() if d.get("timestamp") else None}
        source("yahoo:spark", f"Yahoo Finance spark（{', '.join(symbols)}）", "https://finance.yahoo.com", True)
        return out
    except Exception as e:  # noqa: BLE001
        source("yahoo:spark", "Yahoo Finance spark", url, False, str(e)[:120])
        log("FETCH", f"Yahoo spark 失敗：{e}", "error")
        return {}


def yq(spark, sym, label):
    url = f"https://finance.yahoo.com/quote/{sym}"
    q = spark.get(sym)
    if not q:
        return {"label": label, "value": None, "chg_pct": None, "source": "Yahoo Finance", "url": url, "error": "no data"}
    return {"label": label, "value": q["value"], "chg_pct": q["chg_pct"], "ts": q["ts"], "source": "Yahoo Finance", "url": url}


def yahoo_chart(symbol: str, interval="1d", rng="3y"):
    err = None
    for host in ("query1", "query2"):
        _yahoo_throttle()
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            j = get(url, params={"interval": interval, "range": rng})
            res = j["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            ts = res.get("timestamp") or []
            rows = []
            for k, t in enumerate(ts):
                c = q["close"][k]
                if c is None:
                    continue
                rows.append({"t": t, "o": q["open"][k], "h": q["high"][k], "l": q["low"][k], "c": c,
                             "v": (q.get("volume") or [0] * len(ts))[k] or 0})
            return res["meta"], rows
        except Exception as e:  # noqa: BLE001
            err = e
    raise err


def twse_indices():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
    try:
        rows = get(url)
        out = {}
        for r in rows:
            try:
                pct = float(r["漲跌百分比"]) * (-1 if r.get("漲跌") == "-" else 1)
                out[r["指數"]] = {"close": float(r["收盤指數"].replace(",", "")), "chg_pct": pct, "date": r["日期"]}
            except (ValueError, KeyError):
                continue
        source("twse:MI_INDEX", "證交所 OpenAPI 每日收盤指數", url, True)
        d = next(iter(out.values()))["date"] if out else ""
        log("FETCH", f"TWSE MI_INDEX {len(out)} 檔指數（資料日 {roc_to_iso(d)}）")
        return out
    except Exception as e:  # noqa: BLE001
        source("twse:MI_INDEX", "證交所 OpenAPI 每日收盤指數", url, False, str(e)[:120])
        log("FETCH", f"TWSE MI_INDEX 失敗：{e}", "error")
        return {}


def roc_to_iso(d: str) -> str:
    if not d or len(d) < 7:
        return d
    return f"{int(d[:3]) + 1911}-{d[3:5]}-{d[5:7]}"


def twse_stock_day_all():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        rows = get(url, timeout=40)
        source("twse:STOCK_DAY_ALL", "證交所 OpenAPI 個股日成交", url, True)
        return {r["Code"]: r for r in rows}
    except Exception as e:  # noqa: BLE001
        source("twse:STOCK_DAY_ALL", "證交所 OpenAPI 個股日成交", url, False, str(e)[:120])
        log("FETCH", f"STOCK_DAY_ALL 失敗：{e}", "error")
        return {}


def _f(x):
    try:
        return float(x) if x not in (None, "", "-") else None
    except ValueError:
        return None


def twse_mis(code="2330"):
    """一次取 TAIEX (t00) 與個股：最新成交、昨收、五檔。回傳 (taiex_kpi, tsmc_kpi, orderbook)"""
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw|tse_{code}.tw&json=1&delay=0"
    page = f"https://mis.twse.com.tw/stock/fibest.jsp?stock={code}"
    try:
        j = get(url)
        msgs = {m["c"]: m for m in j["msgArray"]}
        sp = lambda s: [float(x) for x in (s or "").strip("_").split("_") if x not in ("", "-")]

        def last_px(m):
            z = _f(m.get("z")) or _f((m.get("trade") or {}).get("z")) or _f(m.get("pz"))
            if z is None:  # 該時點無成交：取最佳買賣中價
                a, b = sp(m.get("a")), sp(m.get("b"))
                z = (a[0] + b[0]) / 2 if a and b else None
            return z

        def kpi(m, label):
            z, y = last_px(m), _f(m.get("y"))
            ts = datetime.fromtimestamp(int(m["tlong"]) / 1000, TPE).isoformat() if m.get("tlong") else None
            return {"label": label, "value": z, "chg_pct": (z / y - 1) * 100 if z and y else None, "ts": ts,
                    "source": "TWSE MIS", "url": page}
        t = msgs.get("t00")
        m = msgs[code]
        taiex = kpi(t, "TAIEX 加權指數") if t else None
        if taiex:
            taiex["url"] = "https://mis.twse.com.tw/stock/index"
        tsmc = kpi(m, f"{code} 台積電")
        asks = list(zip(sp(m.get("a")), sp(m.get("f"))))
        bids = list(zip(sp(m.get("b")), sp(m.get("g"))))
        ts = tsmc["ts"]
        last = tsmc["value"]
        source("twse:MIS", "證交所 基本市況報導 (MIS) 指數/個股/五檔", url, True, "排程快照，非逐筆即時")
        return taiex, tsmc, {"asks": asks, "bids": bids, "last": last, "prev_close": _f(m.get("y")),
                "open": float(m["o"]) if m.get("o") not in (None, "-") else None,
                "high": float(m["h"]) if m.get("h") not in (None, "-") else None,
                "low": float(m["l"]) if m.get("l") not in (None, "-") else None,
                "volume_lots": int(m["v"]) if str(m.get("v", "")).isdigit() else None,
                "ts": ts, "source": "TWSE MIS", "url": page}
    except Exception as e:  # noqa: BLE001
        source("twse:MIS", "證交所 基本市況報導 (MIS) 指數/個股/五檔", url, False, str(e)[:120])
        log("FETCH", f"MIS 失敗：{e}", "error")
        return None, None, None


def binance_daily(symbol="BTCUSDT", limit=30):
    url = "https://data-api.binance.vision/api/v3/klines"
    try:
        k = get(url, params={"symbol": symbol, "interval": "1d", "limit": limit})
        source(f"binance:{symbol}", f"Binance {symbol} 日K", url, True)
        return [float(x[4]) for x in k]
    except Exception as e:  # noqa: BLE001
        source(f"binance:{symbol}", f"Binance {symbol} 日K", url, False, str(e)[:120])
        return []


def fred_series(sid: str):
    key = os.environ.get("FRED_API_KEY")
    try:
        if key:
            url = "https://api.stlouisfed.org/fred/series/observations"
            j = get(url, params={"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 60})
            obs = [(o["date"], float(o["value"])) for o in j["observations"] if o["value"] != "."][::-1]
        else:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
            txt = get(url, as_json=False, timeout=12, retries=1)
            obs = []
            for line in txt.strip().splitlines()[1:]:
                d, v = line.split(",")[:2]
                if v not in (".", ""):
                    obs.append((d, float(v)))
            obs = obs[-60:]
        source(f"fred:{sid}", f"FRED {sid}", f"https://fred.stlouisfed.org/series/{sid}", True)
        return obs
    except Exception as e:  # noqa: BLE001
        source(f"fred:{sid}", f"FRED {sid}", f"https://fred.stlouisfed.org/series/{sid}", False, str(e)[:120])
        log("FETCH", f"FRED {sid} 失敗：{str(e)[:80]}", "warn")
        return []


# ───────────────────────── 新聞 ─────────────────────────

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


def news_cnyes(limit=30):
    url = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"
    try:
        j = get(url, params={"limit": limit})
        items = []
        for it in j["items"]["data"]:
            items.append({"id": f"cnyes:{it['newsId']}", "title": strip_html(it["title"]),
                          "summary": strip_html(it.get("summary", ""))[:200],
                          "url": f"https://news.cnyes.com/news/id/{it['newsId']}",
                          "source": "鉅亨網", "ts": datetime.fromtimestamp(int(it["publishAt"]), TPE).isoformat()})
        source("cnyes", "鉅亨網 台股新聞 API", url, True)
        return items
    except Exception as e:  # noqa: BLE001
        source("cnyes", "鉅亨網 台股新聞 API", url, False, str(e)[:120])
        log("FETCH", f"鉅亨網失敗：{e}", "error")
        return []


def news_udn(url="https://money.udn.com/rssfeed/news/1001/5590?ch=money"):
    try:
        txt = get(url, as_json=False)
        root = ET.fromstring(txt.encode("utf-8"))
        items = []
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            m = re.search(r"/(\d+)$", link)
            try:
                ts = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TPE).isoformat()
            except Exception:  # noqa: BLE001
                ts = NOW.isoformat()
            items.append({"id": f"udn:{m.group(1) if m else link}", "title": strip_html(it.findtext("title")),
                          "summary": strip_html(it.findtext("description"))[:200], "url": link,
                          "source": "經濟日報", "ts": ts})
        source("udn", "經濟日報 RSS", url, True)
        return items
    except Exception as e:  # noqa: BLE001
        source("udn", "經濟日報 RSS", url, False, str(e)[:120])
        log("FETCH", f"經濟日報 RSS 失敗：{e}", "error")
        return []


SECTORS = ["半導體", "電腦及週邊", "電子零組件", "通信網路", "光電", "金融保險",
           "航運", "鋼鐵", "塑膠", "生技醫療", "觀光餐旅", "建材營造"]

POS_WORDS = ["創新高", "兩年高", "紅燈", "大漲", "攻頂", "飆", "簽約", "授權", "上調", "成長", "買超", "優於預期", "獲利", "大增", "轉盈", "看好", "強勁", "漲停", "新高", "訂單", "受惠", "回升", "擴產", "年增"]
NEG_WORDS = ["下修", "衰退", "賣超", "虧損", "不如預期", "下滑", "調降", "跌停", "重挫", "疑慮", "利空", "減少", "年減", "砍單", "警示", "風險", "崩"]
SECTOR_KEYS = {
    "半導體": ["半導體", "晶圓", "台積電", "聯電", "封裝", "HBM", "記憶體", "IC", "聯發科", "日月光"],
    "電腦及週邊": ["伺服器", "廣達", "緯創", "鴻海", "英業達", "技嘉", "AI PC", "筆電"],
    "電子零組件": ["零組件", "PCB", "被動元件", "國巨", "台光電", "散熱"],
    "通信網路": ["光通訊", "網通", "5G", "中華電", "交換器", "CPO"],
    "光電": ["光電", "面板", "友達", "群創", "大立光", "鏡頭"],
    "金融保險": ["金控", "銀行", "壽險", "國泰金", "富邦金", "金融", "保險"],
    "航運": ["航運", "貨櫃", "長榮", "陽明", "萬海", "運價", "散裝"],
    "鋼鐵": ["鋼鐵", "中鋼", "鋼價"],
    "塑膠": ["塑化", "台塑", "南亞", "石化"],
    "生技醫療": ["生技", "醫療", "新藥", "製藥", "中化生"],
    "觀光餐旅": ["觀光", "飯店", "餐飲", "旅遊"],
    "建材營造": ["營建", "建商", "房市", "營造", "建案"],
}


def lexicon_score(item):
    t = item["title"] + " " + item.get("summary", "")
    p = sum(t.count(w) for w in POS_WORDS)
    n = sum(t.count(w) for w in NEG_WORDS)
    score = 0.0 if p + n == 0 else (p - n) / (p + n) * min(1.0, 0.4 + 0.2 * (p + n))
    secs = [s for s, ks in SECTOR_KEYS.items() if any(k in t for k in ks)]
    return {"score": round(score, 3), "sectors": secs, "tickers": [], "reason": f"詞典法：正向詞 {p}、負向詞 {n}", "method": "lexicon"}


def llm_score(items):
    """以 Anthropic Messages API 批次評分；回傳 {id: {...}}。無金鑰或失敗時回傳 {}。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not items:
        return {}, 0
    model = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
    lines = "\n".join(f'{i}. [{it["id"]}] {it["title"]}｜{it.get("summary", "")[:120]}' for i, it in enumerate(items))
    prompt = (
        "你是台股新聞情緒分析師。對下列每則新聞，評估其對相關台股（或整體台股）短期股價的影響。\n"
        f"產業只能從這個清單選（可多選或空陣列）：{SECTORS}\n"
        "只輸出 JSON 陣列，不要其他文字。每個元素格式：\n"
        '{"id": "原始 id", "score": -1.0 到 1.0 的數字（-1 極負面，0 中性，1 極正面）, '
        '"sectors": [...], "tickers": ["4 碼股票代號", ...], "reason": "15 字內理由"}\n\n' + lines)
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=90, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        j = r.json()
        text = "".join(b.get("text", "") for b in j["content"])
        arr = json.loads(text[text.index("["): text.rindex("]") + 1])
        tok = j.get("usage", {}).get("input_tokens", 0) + j.get("usage", {}).get("output_tokens", 0)
        out = {}
        for a in arr:
            s = float(a.get("score", 0))
            out[a["id"]] = {"score": round(max(-1, min(1, s)), 3),
                            "sectors": [x for x in a.get("sectors", []) if x in SECTORS],
                            "tickers": [str(x) for x in a.get("tickers", [])][:5],
                            "reason": str(a.get("reason", ""))[:40], "method": f"llm:{model}"}
        log("LLM", f"{model} 評分 {len(out)} 則新聞 · tokens={tok}")
        return out, tok
    except Exception as e:  # noqa: BLE001
        log("LLM", f"LLM 評分失敗，改用詞典法：{str(e)[:100]}", "warn")
        return {}, 0


def process_news():
    cache_f = DATA / "news_cache.json"
    cache = json.loads(cache_f.read_text("utf-8")) if cache_f.exists() else {}
    fresh = news_cnyes() + news_udn()
    seen, items = set(), []
    for it in fresh:
        if it["id"] not in seen:
            seen.add(it["id"])
            items.append(it)
    new = [it for it in items if it["id"] not in cache or cache[it["id"]].get("method") == "lexicon" and os.environ.get("ANTHROPIC_API_KEY")]
    log("FETCH", f"新聞 {len(items)} 則（新進 {len(new)} 則待評分）")
    scored = {}
    for i in range(0, len(new), 40):
        s, _ = llm_score(new[i:i + 40])
        scored.update(s)
    for it in new:
        cache[it["id"]] = {**it, **(scored.get(it["id"]) or lexicon_score(it))}
    # 只保留 7 天
    cutoff = (NOW - timedelta(days=7)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("ts", "") >= cutoff}
    cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
    allnews = sorted(cache.values(), key=lambda x: x["ts"], reverse=True)

    # 情緒 EMA：近 48 小時，半衰期 12 小時
    def w(ts):
        h = (NOW - datetime.fromisoformat(ts)).total_seconds() / 3600
        return 0.5 ** (h / 12) if h <= 48 else 0
    ws = [(w(n["ts"]), n["score"]) for n in allnews]
    tw = sum(a for a, _ in ws)
    ema = sum(a * b for a, b in ws) / tw if tw else 0.0
    sect = {}
    for s in SECTORS:
        sub = [(w(n["ts"]), n["score"]) for n in allnews if s in n.get("sectors", [])]
        t = sum(a for a, _ in sub)
        sect[s] = {"sentiment": (sum(a * b for a, b in sub) / t) if t else None, "n": sum(1 for a, _ in sub if a > 0)}
    methods = sorted({n.get("method", "") for n in allnews[:40]})
    log("SENTIMENT", f"48h 加權情緒 EMA={ema:+.3f}（方法：{', '.join(methods)}）")
    return allnews[:40], ema, sect, methods


# ───────────────────────── 因子模型 ─────────────────────────

def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    out = np.full(len(c), np.nan)
    au, ad = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
        out[i] = 100 - 100 / (1 + au / ad) if ad > 0 else 100
    return out


def to_date(t):
    return datetime.fromtimestamp(t, TPE).date()


def build_features():
    _, tw = yahoo_chart("^TWII", "1d", "5y")
    # 盤中（13:35 前）Yahoo 日線最後一根是未收盤的即時值，不能當「收盤特徵」，丟掉
    if tw and to_date(tw[-1]["t"]) == NOW.date() and (NOW.hour, NOW.minute) < (13, 35):
        tw = tw[:-1]
    _, sox = yahoo_chart("^SOX", "1d", "5y")
    _, fx = yahoo_chart("TWD=X", "1d", "5y")
    source("yahoo:hist", "Yahoo Finance 日線（^TWII, ^SOX, TWD=X）", "https://finance.yahoo.com/quote/%5ETWII/history", True)
    dates = [to_date(r["t"]) for r in tw]
    c = np.array([r["c"] for r in tw], float)
    # 美股以紐約日期為準，取「台股日期之前最後一個美股交易日」的報酬，避免前視
    sox_d = [datetime.fromtimestamp(r["t"], timezone(timedelta(hours=-5))).date() for r in sox]
    sox_c = np.array([r["c"] for r in sox], float)
    sox_r = np.concatenate([[np.nan], sox_c[1:] / sox_c[:-1] - 1])
    fx_d = [to_date(r["t"]) for r in fx]
    fx_c = np.array([r["c"] for r in fx], float)

    def last_before(ds, vals, d, strict=True):
        lo, hi = 0, len(ds)
        while lo < hi:
            m = (lo + hi) // 2
            if (ds[m] < d) if strict else (ds[m] <= d):
                lo = m + 1
            else:
                hi = m
        return vals[lo - 1] if lo > 0 else np.nan

    r = np.concatenate([[np.nan], c[1:] / c[:-1] - 1])
    f = {
        "mom5": c / np.roll(c, 5) - 1,
        "mom20": c / np.roll(c, 20) - 1,
        "vol20": np.array([np.nanstd(r[max(0, i - 19):i + 1]) * math.sqrt(252) if i >= 20 else np.nan for i in range(len(c))]),
        "rsi14": rsi(c),
        "sox_prev": np.array([last_before(sox_d, sox_r, d) for d in dates]),
        "fx5": np.array([last_before(fx_d, fx_c, d) for d in dates]),
    }
    f["fx5"] = f["fx5"] / np.roll(f["fx5"], 5) - 1
    for k in ("mom5", "mom20", "fx5"):
        f[k][:21] = np.nan
    X = np.column_stack([f[k] for k in f])
    y_ret = np.concatenate([r[1:], [np.nan]])  # t → t+1 報酬
    return dates, c, X, list(f), y_ret


def fit_logit(X, y, l2=1.0, iters=3000, lr=0.1):
    n, k = X.shape
    w, b = np.zeros(k), 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def auc(y, p):
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    npos, nneg = pos.sum(), (~pos).sum()
    return float((ranks[pos].sum() - npos * (npos + 1) / 2) / (npos * nneg)) if npos and nneg else float("nan")


def risk_metrics(rets):
    rets = np.asarray(rets)
    nav = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(nav)
    sd = rets.std(ddof=1)
    return {"var95_1d": float(-np.percentile(rets, 5)), "mdd": float((1 - nav / peak).max()),
            "sharpe": float(rets.mean() / sd * math.sqrt(252)) if sd > 0 else None,
            "vol_ann": float(sd * math.sqrt(252)), "cagr": float(nav[-1] ** (252 / len(rets)) - 1)}


def run_model(sent_ema):
    labels = {"mom5": "5日動能", "mom20": "20日動能", "vol20": "20日波動度", "rsi14": "RSI14",
              "sox_prev": "費半前一日", "fx5": "台幣5日變動"}
    dates, c, X, names, y_ret = build_features()
    ok = ~np.isnan(X).any(1)
    train_mask = ok & ~np.isnan(y_ret)
    idx = np.where(train_mask)[0]
    split = idx[int(len(idx) * 0.7)]
    tr, te = idx[idx < split], idx[idx >= split]
    mu, sd = X[tr].mean(0), X[tr].std(0)
    Z = (X - mu) / sd
    y = (y_ret > 0).astype(float)
    w, b = fit_logit(Z[tr], y[tr])
    p_te = 1 / (1 + np.exp(-(Z[te] @ w + b)))
    thr = 0.5
    pos = (p_te > thr).astype(float)
    strat = pos * y_ret[te]
    bh = y_ret[te]
    acc = float(((p_te > 0.5) == (y[te] == 1)).mean())
    base = float(y[te].mean())
    # 全樣本重估，用最後一天的特徵預測下一交易日
    mu2, sd2 = X[idx].mean(0), X[idx].std(0)
    Z2 = (X - mu2) / sd2
    w2, b2 = fit_logit(Z2[idx], y[idx])
    last = np.where(ok)[0][-1]
    z_last = Z2[last]
    logit_f = float(z_last @ w2 + b2)
    p_factor = 1 / (1 + math.exp(-logit_f))
    # 情緒疊加：固定先驗權重（非估計值，因新聞情緒尚無足夠歷史可回測）
    W_SENT = float(os.environ.get("SENT_WEIGHT", 0.5))
    p_ens = 1 / (1 + math.exp(-(logit_f + W_SENT * sent_ema)))
    feats = [{"key": k, "label": labels[k], "value": float(X[last, i]), "z": float(z_last[i]),
              "coef": float(w2[i]), "contrib": float(z_last[i] * w2[i])} for i, k in enumerate(names)]
    step = max(1, len(te) // 250)
    nav_s, nav_b = np.cumprod(1 + strat), np.cumprod(1 + bh)
    bt = {"dates": [dates[i].isoformat() for i in te[::step]],
          "strategy": [round(float(x), 4) for x in nav_s[::step]],
          "buyhold": [round(float(x), 4) for x in nav_b[::step]]}
    m = {"p_factor": p_factor, "p_ensemble": p_ens, "sent_weight": W_SENT, "intercept": float(b2),
         "asof": dates[last].isoformat(), "features": feats,
         "oos": {"start": dates[te[0]].isoformat(), "end": dates[te[-1]].isoformat(), "n": int(len(te)),
                 "accuracy": acc, "base_rate_up": base, "auc": auc(y[te], p_te), "exposure": float(pos.mean())},
         "train": {"start": dates[tr[0]].isoformat(), "end": dates[tr[-1]].isoformat(), "n": int(len(tr))},
         "risk_strategy": risk_metrics(strat), "risk_buyhold": risk_metrics(bh), "backtest": bt,
         "rsi14": float(X[last, names.index("rsi14")]), "taiex_close": float(c[last])}
    log("MODEL", f"邏輯斯迴歸 P_factor={p_factor:.3f} → 疊加情緒 P_ens={p_ens:.3f}；OOS 準確率 {acc:.1%}（基準 {max(base, 1 - base):.1%}）AUC={m['oos']['auc']:.3f}")
    log("RISK", f"OOS 策略 Sharpe={m['risk_strategy']['sharpe']:.2f} MDD={m['risk_strategy']['mdd']:.1%} VaR95={m['risk_strategy']['var95_1d']:.2%}")
    log("MOMENTUM", f"TAIEX RSI14={m['rsi14']:.1f} 20日動能={feats[1]['value']:+.2%} 費半前一日={feats[4]['value']:+.2%}")
    return m


# ───────────────────────── 資產配置（規則式） ─────────────────────────

def allocate(p_ens, p_factor, sent, us10y_chg, btc_mom):
    assets = ["台股", "美股", "債券", "加密資產", "現金"]
    base = {"台股": .30, "美股": .20, "債券": .25, "加密資產": .05, "現金": .20}
    tilts = {  # 每個訊號對每個資產的加減碼（權重單位）
        "因子模型": {"台股": 0.6 * (p_factor - 0.5)},
        "新聞情緒": {"台股": 0.10 * sent, "美股": 0.05 * sent},
        "總經/動能": {"債券": -0.5 * (us10y_chg or 0), "加密資產": 0.05 * math.tanh(10 * (btc_mom or 0))},
    }
    w = dict(base)
    for t in tilts.values():
        for a, v in t.items():
            w[a] += v
    w = {a: max(0.0, v) for a, v in w.items() if a != "現金"}
    w["現金"] = max(0.0, 1 - sum(w.values()))
    s = sum(w.values())
    w = {a: v / s for a, v in w.items()}
    # Sankey 流量：各資產權重依「基礎份額 + 正向加碼」比例分攤給三個訊號來源
    srcs = list(tilts)
    flows = []
    for a in assets:
        share = [base[a] / 3 + max(0, tilts[sname].get(a, 0)) for sname in srcs]
        tot = sum(share) or 1
        for sname, sh in zip(srcs, share):
            flows.append({"from": sname, "to": a, "value": w[a] * sh / tot})
    return {"weights": w, "base": base, "tilts": tilts, "flows": flows,
            "inputs": {"p_factor": p_factor, "sentiment": sent, "us10y_20d_chg": us10y_chg, "btc_7d_mom": btc_mom}}


# ───────────────────────── 主程式 ─────────────────────────

def main():
    log("SYSTEM", f"管線啟動 {NOW:%Y-%m-%d %H:%M} (Asia/Taipei)")
    taiex, tsmc, ob = twse_mis("2330")
    spark = yahoo_spark(["TWD=X", "^VIX", "^SOX", "^TNX", "NVDA", "AAPL", "GC=F", "^TWII", "2330.TW"])
    kpi = {
        "taiex": taiex if taiex and taiex["value"] else yq(spark, "^TWII", "TAIEX 加權指數"),
        "tsmc": tsmc if tsmc and tsmc["value"] else yq(spark, "2330.TW", "2330 台積電"),
        "usdtwd": yq(spark, "TWD=X", "USD / TWD"),
        "vix": yq(spark, "^VIX", "VIX 恐慌指數"),
        "sox": yq(spark, "^SOX", "費城半導體"),
    }
    log("FETCH", "KPI " + " · ".join(f"{v['label']}={v['value']:,.2f}" for v in kpi.values() if v and v.get("value")))
    try:
        meta, bars = yahoo_chart("2330.TW", "1m", "1d")
        intraday = {"bars": bars, "source": "Yahoo Finance 1m", "url": "https://finance.yahoo.com/quote/2330.TW",
                    "ts": datetime.fromtimestamp(meta["regularMarketTime"], TPE).isoformat()}
        if len(bars) < 30:  # 盤前 / 假日：改給近 5 日 5 分 K
            meta, bars = yahoo_chart("2330.TW", "5m", "5d")
            intraday.update(bars=bars, source="Yahoo Finance 5m (近 5 日)")
        log("FETCH", f"2330 K 線 {len(bars)} 根（{intraday['source']}）")
    except Exception as e:  # noqa: BLE001
        intraday = {"bars": [], "error": str(e)[:120]}
        log("FETCH", f"2330 K 線失敗：{e}", "error")

    idx = twse_indices()
    sda = twse_stock_day_all()
    us10y = fred_series("DGS10")
    us10y_chg = (us10y[-1][1] - us10y[-21][1]) / 100 if len(us10y) > 21 else None
    if us10y:
        kpi["us10y"] = {"label": "美 10Y 殖利率", "value": us10y[-1][1], "chg_pct": None, "chg_bp": (us10y[-1][1] - us10y[-2][1]) * 100,
                        "ts": us10y[-1][0], "source": "FRED DGS10", "url": "https://fred.stlouisfed.org/series/DGS10"}
    elif "^TNX" in spark:  # 退路：Yahoo ^TNX（5 日變動近似）
        q = yq(spark, "^TNX", "美 10Y 殖利率")
        q["source"] = "Yahoo ^TNX（FRED 不可用時的備援）"
        cl = spark["^TNX"]["closes"]
        q["chg_bp"] = (cl[-1] - cl[-2]) * 100 if len(cl) >= 2 else None
        kpi["us10y"] = q
        us10y_chg = (cl[-1] - cl[0]) / 100 if len(cl) >= 2 else None
    btc = binance_daily("BTCUSDT", 10)
    btc_mom = btc[-1] / btc[-8] - 1 if len(btc) >= 8 else None

    news, sent, sect_sent, methods = process_news()
    try:
        model = run_model(sent)
    except Exception as e:  # noqa: BLE001
        model = None
        log("MODEL", f"模型失敗：{e}", "error")

    sectors = []
    for s in SECTORS:
        hit = next((v for k, v in idx.items() if k.startswith(s) and k.endswith("類指數")), None)
        sectors.append({"name": s, "index_chg_pct": hit["chg_pct"] if hit else None,
                        "index_close": hit["close"] if hit else None,
                        "sentiment": sect_sent[s]["sentiment"], "n_news": sect_sent[s]["n"]})
    idx_date = roc_to_iso(next(iter(idx.values()))["date"]) if idx else None

    tape_codes = ["2317", "2454", "2382", "2308", "2303", "2603", "2882", "2881", "3711", "2412"]
    tape = []
    for code in tape_codes:
        r = sda.get(code)
        if not r:
            continue
        try:
            close, ch = float(r["ClosingPrice"]), float(r["Change"] or 0)
            tape.append({"sym": f"{code} {r['Name']}", "price": close, "chg_pct": ch / (close - ch) * 100 if close - ch else 0,
                         "source": "TWSE STOCK_DAY_ALL", "date": roc_to_iso(r.get("Date", ""))})
        except (ValueError, KeyError):
            continue
    for sym, lab in (("NVDA", "NVDA"), ("AAPL", "AAPL"), ("GC=F", "黃金期貨")):
        q = spark.get(sym)
        if q:
            tape.append({"sym": lab, "price": q["value"], "chg_pct": q["chg_pct"], "source": "Yahoo Finance"})

    alloc = allocate(model["p_ensemble"], model["p_factor"], sent, us10y_chg, btc_mom) if model else None
    if alloc:
        log("ALLOC", "建議權重 " + " / ".join(f"{a} {v:.0%}" for a, v in alloc["weights"].items()))

    out = {"generated_at": datetime.now(TPE).isoformat(timespec="seconds"), "kpi": kpi, "intraday_2330": intraday,
           "orderbook_2330": ob, "sectors": {"date": idx_date, "items": sectors, "index_source": "TWSE OpenAPI MI_INDEX"},
           "news": news, "sentiment": {"ema48h": sent, "methods": methods}, "model": model, "allocation": alloc,
           "tape": tape, "sources": SOURCES}
    (DATA / "dashboard.json").write_text(json.dumps(out, ensure_ascii=False, default=float), "utf-8")
    # file:// 開啟時瀏覽器禁止 fetch 本機 JSON；同步輸出 .js 版本供 <script> 載入
    (DATA / "dashboard.js").write_text("window.__DASH__=" + json.dumps(out, ensure_ascii=False, default=float) + ";", "utf-8")

    lf = DATA / "log.json"
    old = json.loads(lf.read_text("utf-8")) if lf.exists() else []
    log("SYSTEM", f"完成：{sum(s['ok'] for s in SOURCES.values())}/{len(SOURCES)} 來源成功")
    logs = (LOG[::-1] + old)[:200]
    lf.write_text(json.dumps(logs, ensure_ascii=False, indent=0), "utf-8")
    (DATA / "log.js").write_text("window.__LOG__=" + json.dumps(logs, ensure_ascii=False) + ";", "utf-8")


if __name__ == "__main__":
    main()
