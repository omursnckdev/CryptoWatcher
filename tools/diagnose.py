"""Read-only diagnosis of one symbol on the Binance testnet: position, open orders, recent fills, liquidation records, income, bot state.

Run inside the container (it never places or cancels anything):
    sudo docker compose exec -T cryptowatcher python - HYPEUSDT < tools/diagnose.py
"""
import os, json, time
from datetime import datetime, timezone
from crypto_watcher.binance import TradingClient, BinanceError
from crypto_watcher.net import make_session
import sys
SYM = sys.argv[1].upper() if len(sys.argv) > 1 else "HYPEUSDT"
c = TradingClient(os.environ["BINANCE_TESTNET_API_KEY"], os.environ["BINANCE_TESTNET_API_SECRET"], session=make_session())
c.sync_time()
def t(ms): return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%m-%d %H:%M:%S")
def tryit(title, fn):
    print(f"\n=== {title}")
    try: fn()
    except BinanceError as e: print("  (alınamadı)", e)
    except Exception as e: print("  (hata)", type(e).__name__, e)

def pos():
    for r in c._signed("GET", "/fapi/v3/positionRisk", {"symbol": SYM}):
        print({k: r.get(k) for k in ("symbol", "positionAmt", "entryPrice", "markPrice", "unRealizedProfit", "liquidationPrice", "marginType", "isolatedMargin", "positionSide")})
def algos():
    rows = c.open_algo_orders()
    for o in sorted(rows, key=lambda o: o["symbol"]):
        print(o["symbol"], o.get("side"), o.get("orderType"), "tetik", o.get("triggerPrice"), "miktar", o.get("quantity"), "closePos", o.get("closePosition"), o.get("algoStatus"))
    print("toplam", len(rows))
def orders():
    rows = c._signed("GET", "/fapi/v1/openOrders", {"symbol": SYM}); print(rows if rows else "yok")
def trades():
    rows = c.user_trades(SYM, int((time.time() - 5 * 86400) * 1000), 100)
    for f in rows[-14:]:
        print(t(f["time"]), f["side"], "miktar", f["qty"], "fiyat", f["price"], "realizedPnl", f["realizedPnl"], "komisyon", f["commission"], "maker" if f.get("maker") else "taker")
    if not rows: print("son 5 günde işlem yok")
def force():
    rows = c._signed("GET", "/fapi/v1/forceOrders", {"symbol": SYM, "limit": 20})
    for o in rows[-5:]: print(t(o.get("time", o.get("updateTime", 0))), o.get("autoCloseType"), o.get("side"), o.get("origQty"), o.get("avgPrice") or o.get("price"))
    if not rows: print("zorla kapatma (likidasyon/ADL) kaydı yok")
def income():
    rows = c._signed("GET", "/fapi/v1/income", {"symbol": SYM, "startTime": int((time.time() - 5 * 86400) * 1000), "limit": 100})
    for r in rows[-12:]: print(t(r["time"]), r["incomeType"], r["income"], r["asset"])
def state():
    s = json.load(open("/app/state/state.json"))
    print("açık işlemler:", {k: (v["side"], v["entry"], v["stop"], v["breakeven_done"]) for k, v in s["trades"].items()})
    print("son kapananlar:")
    for h in s["history"][-6:]:
        print("  ", t(h["closed_ms"]), h["symbol"], h["side"], "giriş", h["entry"], "çıkış", h["exit"], "pnl", None if h["pnl"] is None else round(h["pnl"], 2), h["reason"])
    print("paused:", s.get("paused"), "| son hata:", s.get("last_error"))
def journal():
    p = "/app/state/journal.jsonl"
    if not os.path.exists(p): print("günlük yok"); return
    for line in open(p).read().splitlines()[-3:]:
        r = json.loads(line); print(r["symbol"], r["side"], r["reason"], "R", r["r"], "en iyi", r["mfe_r"], "en kötü", r["mae_r"], "tutma(sa)", r["hold_h"], "stop", r.get("initial_stop"), "çıkış", r["exit"])

def tracked_orders():
    """For every trade the bot tracks: what the exchange says about the bot's own stop / take-profit order ids."""
    s = json.load(open("/app/state/state.json"))
    for symbol, tr in s["trades"].items():
        print(f"{symbol} {tr['side']} giriş {tr['entry']} · açılış {t(tr['opened_ms'])} · kayıtlı stop {tr['stop']} (başabaş {tr['breakeven_done']}) · TP {tr['take_profit']}")
        for key in ("sl_id", "tp_id"):
            cid = tr.get(key)
            if not cid:
                print(f"   {key}: kayıtlı değil"); continue
            try:
                o = c.get_algo_order(cid)
                extra = {k: v for k, v in o.items() if any(w in k.lower() for w in ("reason", "msg", "error"))}
                print(f"   {key}: {o.get('algoStatus')} · {o.get('orderType')} {o.get('side')} tetik {o.get('triggerPrice')} miktar {o.get('quantity')} "
                      f"closePos {o.get('closePosition')} reduceOnly {o.get('reduceOnly')} · oluşturma {t(o['createTime']) if o.get('createTime') else '?'} "
                      f"· güncelleme {t(o['updateTime']) if o.get('updateTime') else '?'} {extra or ''}")
            except BinanceError as e:
                print(f"   {key}: sorgulanamadı ({e})")
def algo_history():
    rows = c._signed("GET", "/fapi/v1/allAlgoOrders", {"symbol": SYM, "limit": 20})
    rows = rows if isinstance(rows, list) else rows.get("orders", [])
    for o in rows[-10:]:
        print(t(o.get("createTime", 0)), o.get("orderType"), o.get("side"), o.get("algoStatus"), "tetik", o.get("triggerPrice"), "miktar", o.get("quantity"),
              "closePos", o.get("closePosition"), "güncelleme", t(o["updateTime"]) if o.get("updateTime") else "?")
    if not rows: print("kayıt yok")

tryit("pozisyon (ham)", pos)
tryit("Bekleyen koşullu emirler (tüm semboller)", algos)
tryit("bekleyen normal emirler", orders)
tryit("son işlemler (dolumlar)", trades)
tryit("Zorla kapatma / likidasyon kayıtları", force)
tryit("gelir geçmişi", income)
tryit("Botun durumu", state)
tryit("İşlem günlüğü (son 3)", journal)
tryit("Botun izlediği işlemlerin stop/TP emirleri (borsadaki durumları)", tracked_orders)
tryit(f"{SYM} koşullu emir geçmişi (son 10)", algo_history)
