"""Read-only full report of everything the bot has recorded, straight from its state directory (no Telegram, no exchange calls).

Run inside the container; it never writes, trades or sends anything:
    sudo docker compose exec -T cryptowatcher python - /app/state < tools/report.py
"""
from collections import defaultdict
from datetime import datetime, timezone
import html
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from crypto_watcher import telegram as tg
from crypto_watcher.config import Settings
from crypto_watcher.journal import Journal, KIND_TEXT
from crypto_watcher.shadow import ShadowRecorder
from crypto_watcher.signals import SignalLog, _t_stat


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def trade_tables(journal: Journal) -> list[str]:
    done = sorted((r for r in journal.records if r.get("r") is not None), key=lambda r: r["closed_ms"])
    if not done:
        return ["(günlükte sonuçlanmış işlem yok)"]
    lines = []
    rs = sorted(r["r"] for r in done)
    pnl = sum(r["pnl"] or 0 for r in done)
    lines.append(f"{len(done)} işlem · {_stamp(done[0]['closed_ms'])} → {_stamp(done[-1]['closed_ms'])} UTC · toplam {pnl:+,.0f} USDT "
                 f"· ortalama {_mean(rs):+.3f}R · medyan {rs[len(rs) // 2]:+.2f}R · kazanma %{100 * sum(r > 0 for r in rs) / len(rs):.0f}")
    lines.append(f"en kötü 3: {', '.join(f'{r:+.1f}R' for r in rs[:3])} · en iyi 3: {', '.join(f'{r:+.1f}R' for r in rs[-3:])}")
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    if wins and losses:
        lines.append(f"ortalama kazanç {_mean(wins):+.2f}R · ortalama kayıp {_mean(losses):+.2f}R · "
                     f"R toplamı: kazançlar {sum(wins):+.1f}, kayıplar {sum(losses):+.1f}")
    lines += ["", "Neden kapandı:"]
    by_reason = defaultdict(list)
    for r in done:
        by_reason[r["reason"]].append(r)
    for reason, rows in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  {reason:15} {len(rows):3d} işlem · ort {_mean(x['r'] for x in rows):+.2f}R · toplam {sum(x['pnl'] or 0 for x in rows):+,.0f} USDT")
    lines += ["", "Yön:"]
    for side in ("LONG", "SHORT"):
        rows = [r for r in done if r["side"] == side]
        if rows:
            lines.append(f"  {side:5} {len(rows):3d} işlem · ort {_mean(x['r'] for x in rows):+.3f}R · kazanma %{100 * sum(x['r'] > 0 for x in rows) / len(rows):.0f}")
    lines += ["", "Sınıf (nasıl bitti):"]
    for kind, text in KIND_TEXT.items():
        rows = [r for r in done if r["kind"] == kind]
        if rows:
            lines.append(f"  {len(rows):3d}× {text} · ort {_mean(x['r'] for x in rows):+.2f}R")
    lines += ["", "Gün gün (UTC, kapanış gününe göre):"]
    days = defaultdict(list)
    for r in done:
        days[datetime.fromtimestamp(r["closed_ms"] / 1000, timezone.utc).strftime("%m-%d")].append(r)
    for day, rows in sorted(days.items()):
        lines.append(f"  {day} {len(rows):2d} işlem · {sum(x['r'] for x in rows):+6.1f}R · {sum(x['pnl'] or 0 for x in rows):+8,.0f} USDT")
    lines += ["", "Coin bazında (en az 3 işlem):"]
    coins = defaultdict(list)
    for r in done:
        coins[r["symbol"]].append(r)
    for symbol, rows in sorted(coins.items(), key=lambda kv: _mean(x["r"] for x in kv[1])):
        if len(rows) >= 3:
            lines.append(f"  {symbol:12} {len(rows):3d} işlem · ort {_mean(x['r'] for x in rows):+.2f}R · toplam {sum(x['pnl'] or 0 for x in rows):+,.0f} USDT")
    return lines


def unusual_closes(journal: Journal) -> list[str]:
    """Trades that did not end as a plain stop / target / breakeven: forced or external closes, time stops, and losses beyond 1.5R."""
    rows = [r for r in sorted(journal.records, key=lambda r: r["closed_ms"])
            if r.get("reason") in ("EXTERNAL", "LIQUIDATION", "ADL", "TIME_STOP") or (r.get("r") is not None and r["r"] < -1.5)]
    if not rows:
        return ["(yok)"]
    lines = []
    for r in rows:
        r_text = "PnL yok" if r.get("r") is None else f"{r['r']:+.1f}R"
        exit_text = "?" if not r.get("exit") else f"{r['exit']:g}"
        lines.append(f"  {_stamp(r['closed_ms'])} {r['symbol']:12} {r['side']:5} {r['reason']:14} giriş {r['entry']:g} → {exit_text} · {r_text} · "
                     f"{r.get('pnl') or 0:+,.0f} USDT · en iyi {r.get('mfe_r', 0):+.1f}R / en kötü {r.get('mae_r', 0):+.1f}R · {r.get('hold_h', 0):.1f} sa")
    return lines


def signal_groups(signals: SignalLog) -> list[str]:
    """Per group: mean, day-clustered t and how much of the total comes from the single best day (a high share = one market event)."""
    rows = signals.rows()
    groups = defaultdict(list)
    for row in rows:
        groups[row["status"]].append(row)
    lines = [f"{'grup':32} {'n':>5} {'gün':>4} {'ort R':>7} {'t(gün)':>7} {'en iyi günün payı':>18}"]
    for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        by_day = defaultdict(float)
        for row in items:
            by_day[_stamp(row["seen_ms"])[:5]] += row["net_r"]
        total = sum(by_day.values())
        t = _t_stat(items)
        share = f"%{100 * max(by_day.values()) / total:.0f}" if total > 0 else "-"
        lines.append(f"{name:32} {len(items):5d} {len(by_day):4d} {_mean(r['net_r'] for r in items):+7.2f} "
                     f"{'—' if t != t else format(t, '+.1f'):>7} {share:>18}")
    return lines


def shadow_days(recorder: ShadowRecorder, horizon: int = 24) -> list[str]:
    rows = [(e["hour"], recorder.outcomes[(e["id"], horizon)]["net"]) for e in recorder.non_overlapping(horizon)
            if "net" in recorder.outcomes.get((e["id"], horizon), {})]
    if not rows:
        return ["(sonuçlanmış olay yok)"]
    by_day = defaultdict(list)
    for hour, net in rows:
        by_day[_stamp(hour)[:5]].append(net)
    pooled = _mean(n for _, n in rows)
    daily = [_mean(v) for v in by_day.values()]
    lines = [f"{horizon}sa, örtüşmesiz {len(rows)} olay, {len(by_day)} gün · olay ortalaması {pooled:+.0f} bp · günlük ortalamaların ortalaması {_mean(daily):+.0f} bp",
             "olay sayısı en yüksek günler (gün · olay · ortalama bp):"]
    for day, nets in sorted(by_day.items(), key=lambda kv: -len(kv[1]))[:6]:
        lines.append(f"  {day} · {len(nets):3d} olay · {_mean(nets):+6.0f} bp")
    top_day = max(by_day, key=lambda day: len(by_day[day]))
    rest = [net for day, nets in by_day.items() if day != top_day for net in nets]
    lines.append(f"en yoğun gün ({top_day}) tüm olayların %{100 * len(by_day[top_day]) / len(rows):.0f}'ini oluşturuyor; o günü çıkarınca olay ortalaması "
                 f"{(_mean(rest) if rest else float('nan')):+.0f} bp")
    return lines


def report(state_dir: str | Path) -> str:
    state_dir = Path(state_dir)
    out = []

    def section(title, body):
        out.extend(["", "=" * 8 + f" {title} " + "=" * 8, *body] if isinstance(body, list) else ["", "=" * 8 + f" {title} " + "=" * 8, body])

    state = {}
    if (state_dir / "state.json").is_file():
        state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    totals, daily = state.get("totals", {}), state.get("daily", {})
    section("Bot durumu (state.json)", [
        f"açık işlem: {list(state.get('trades', {})) or 'yok'} · duraklatıldı: {state.get('paused')} · son hata: {state.get('last_error')}",
        plain(tg.fmt_pnl(daily, totals, None, state.get("history", []), None))])

    journal = Journal(state_dir / "journal.jsonl")
    section("İşlem günlüğü", trade_tables(journal))
    section("Olağandışı kapanışlar (zorla/dışarıdan/zaman stopu/-1.5R altı)", unusual_closes(journal))
    section("/analiz", plain(journal.format_summary(last=8)))

    signals = SignalLog(state_dir / "signals.jsonl", None, Settings())
    section("/atlanan", plain(signals.format_summary(journal)))
    section("Atlanan sinyaller: grup tablosu", signal_groups(signals))

    shadow_path = state_dir / "shadow.jsonl"
    if shadow_path.is_file():
        with tempfile.TemporaryDirectory() as scratch:        # the recorder may append a start line to a file lacking one: work on a copy
            copy = Path(scratch) / "shadow.jsonl"
            shutil.copyfile(shadow_path, copy)
            recorder = ShadowRecorder(None, None, copy)
            section("/golge", plain(recorder.format_summary()))
            section("Gölge kayıt: günlere dağılım", shadow_days(recorder))
    else:
        section("/golge", "shadow.jsonl yok")
    return "\n".join(out).strip()


if __name__ == "__main__":
    print(report(sys.argv[1] if len(sys.argv) > 1 else "/app/state"))
