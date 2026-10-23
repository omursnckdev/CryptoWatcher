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
from crypto_watcher.signals import SignalLog


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
    section("/analiz", plain(journal.format_summary(last=8)))

    signals = SignalLog(state_dir / "signals.jsonl", None, Settings())
    section("/atlanan", plain(signals.format_summary(journal)))

    shadow_path = state_dir / "shadow.jsonl"
    if shadow_path.is_file():
        with tempfile.TemporaryDirectory() as scratch:        # the recorder may append a start line to a file lacking one: work on a copy
            copy = Path(scratch) / "shadow.jsonl"
            shutil.copyfile(shadow_path, copy)
            section("/golge", plain(ShadowRecorder(None, None, copy).format_summary()))
    else:
        section("/golge", "shadow.jsonl yok")
    return "\n".join(out).strip()


if __name__ == "__main__":
    print(report(sys.argv[1] if len(sys.argv) > 1 else "/app/state"))
