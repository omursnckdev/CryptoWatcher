import argparse
import json
import logging
import sys
import time
from pathlib import Path
from .config import load_secrets, load_settings


def _print_report(report: dict, top: int):
    print(f"CryptoWatcher | {report['mode']} | veri: {report['data_source']} | {report['as_of']} | "
          f"BTC rejimi: {report['market_regime']} | sermaye: {report['capital_usdt']:,.0f} USDT")
    print(f"{'SYMBOL':12}{'SKOR':>7}  {'YÖN':6}{'SİNYAL':16}{'L/S skor':>14}  engeller")
    for row in report["signals"][:top]:
        sides = row["sides"]
        ls = "/".join(f"{sides[s]['score']:.0f}" if s in sides else "-" for s in ("LONG", "SHORT"))
        blockers = "; ".join(sides[row["side"]]["blockers"])
        print(f"{row['symbol']:12}{row['score']:7.2f}  {row['side']:6}{row['signal']:16}{ls:>14}  {blockers}")
    for error in report["errors"]:
        print(f"ERROR {error['symbol']}: {error['error']}", file=sys.stderr)


def cmd_scan(args, settings, secrets) -> int:
    from .data import CachedProvider, DemoProvider, choose_market_source, parse_exchange_info, select_universe
    from .binance import MarketClient, TESTNET_URL
    from .engine import scan
    from .news import NewsService
    now_ms = int(time.time() * 1000)
    if args.demo:
        provider, symbols, source, warning = DemoProvider(now_ms), DemoProvider().symbols(), "demo", None
    else:
        provider, source, warning = choose_market_source(settings.market_data)
        infos = parse_exchange_info(MarketClient(TESTNET_URL).exchange_info())
        symbols = select_universe(settings, infos, provider.tickers(), now_ms)
        provider = CachedProvider(provider)
    if warning:
        print(f"UYARI: {warning}", file=sys.stderr)
    news = NewsService(settings.news_feeds, settings.news_max_age_hours, settings.news_cache_minutes) \
        if settings.news_enabled and not args.demo else None
    report = scan(provider, settings, symbols, settings.capital_usdt or 10_000.0, news, now_ms, source, args.demo)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    if args.json:
        print(encoded)
    else:
        _print_report(report, args.top)
    return 1 if report["errors"] else 0


def cmd_run(args, settings, secrets) -> int:
    from .bot import build_bot
    bot = build_bot(settings, secrets, args.dry_run)
    if args.once:
        bot.start()
        bot.tick()
        return 0
    bot.run_forever()
    return 0


def cmd_check(args, settings, secrets) -> int:
    """Verify every external dependency once and print a checklist."""
    import requests
    from .binance import BinanceError, MarketClient, TESTNET_URL, TradingClient
    from .data import choose_market_source
    from .news import NewsService
    from .telegram import TelegramNotifier
    ok = True
    from .net import trust_source
    print(f"• TLS sertifika kaynağı: {trust_source()}")

    def line(good: bool | None, text: str):
        nonlocal ok
        ok &= good is not False
        print(("✔ " if good else "✘ " if good is False else "• ") + text)
    try:
        _, source, warning = choose_market_source(settings.market_data)
        line(True, f"Piyasa verisi: {source}" + (f"  (uyarı: {warning})" if warning else ""))
    except BinanceError as error:
        line(False, f"Piyasa verisi alınamadı: {error}")
    try:
        MarketClient(TESTNET_URL).ping()
        line(True, "Binance futures testnet erişilebilir")
    except BinanceError as error:
        line(False, f"Testnet erişilemiyor: {error}")
    if secrets.has_binance:
        try:
            client = TradingClient(secrets.api_key, secrets.api_secret)
            offset = client.sync_time()
            balance = client.usdt_balance()
            line(True, f"Testnet API anahtarı geçerli · USDT bakiye {balance['wallet']:,.2f} · saat farkı {offset} ms")
            line(not client.position_mode() or None, "Pozisyon modu: one-way" if not client.position_mode()
                 else "Hedge modu açık: bot ilk çalışmada one-way'e geçirmeyi deneyecek")
            open_positions = client.positions()
            line(None, f"Açık testnet pozisyonu: {len(open_positions)}")
        except (BinanceError, ValueError, requests.RequestException) as error:
            line(False, f"Testnet API anahtarı kullanılamadı: {error}")
    else:
        line(None, "Binance testnet anahtarı yok (.env): yalnızca --dry-run çalışır")
    if secrets.has_telegram:
        sent = TelegramNotifier(secrets.telegram_token, secrets.telegram_chat_id).send("✅ CryptoWatcher bağlantı testi")
        line(sent, "Telegram test mesajı gönderildi" if sent else "Telegram mesajı gönderilemedi (token/chat id?)")
    else:
        line(None, "Telegram ayarı yok: bildirimler yalnızca log'a yazılır")
    if settings.news_enabled:
        news = NewsService(settings.news_feeds)
        news.refresh(force=True)
        line(len(news._items) > 0, f"Haber akışı: {len(news._items)} başlık, {len(news.last_errors)} kaynak hatası")
    return 0 if ok else 1


def cmd_telegram_id(args, settings, secrets) -> int:
    """Print the chat/user ids the bot has recently seen, to fill in .env."""
    from .telegram import TelegramNotifier
    if not secrets.telegram_token:
        print("TELEGRAM_BOT_TOKEN .env içinde yok.", file=sys.stderr)
        return 2
    notifier = TelegramNotifier(secrets.telegram_token, "0")
    username = notifier.whoami()
    if not username:
        print("Token geçersiz (getMe başarısız).", file=sys.stderr)
        return 2
    seen = notifier.discover()
    print(f"Bot: @{username}\n")
    if not seen["chats"]:
        print("Henüz mesaj görmedim. Önce özelde bota 'merhaba' yazın; grup için gruba "
              f"'/status@{username}' yazın, sonra bu komutu tekrar çalıştırın.")
        return 1
    print("Sohbetler (TELEGRAM_CHAT_ID için birini seçin):")
    for chat_id, (kind, name) in seen["chats"].items():
        print(f"  {chat_id:>16}  {kind:<11} {name}")
    print("\nKullanıcılar (grupta /pause, /resume yetkisi için TELEGRAM_ALLOWED_USER_IDS):")
    for user_id, name in seen["users"].items():
        print(f"  {user_id:>16}  {name}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cryptowatcher", description="CryptoWatcher: Binance futures TESTNET long/short bot")
    parser.add_argument("--config", type=Path, help="TOML settings file (default: config/settings.toml if present)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="Analyse the universe and print scores; sends no orders")
    scan_p.add_argument("--demo", action="store_true", help="Synthetic offline data")
    scan_p.add_argument("--json", action="store_true")
    scan_p.add_argument("--output", type=Path)
    scan_p.add_argument("--top", type=int, default=20)
    run_p = sub.add_parser("run", help="Run the bot loop on Binance futures testnet")
    run_p.add_argument("--dry-run", action="store_true", help="Only announce signals on Telegram; place no orders")
    run_p.add_argument("--once", action="store_true", help="Single scan/manage iteration, then exit")
    sub.add_parser("check", help="Test API keys, Telegram, data sources and news feeds")
    sub.add_parser("telegram-id", help="Find your Telegram chat id / group id and user id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        config_path = args.config or (Path("config/settings.toml") if Path("config/settings.toml").is_file() else None)
        settings = load_settings(config_path)
        secrets = load_secrets(args.env_file)
        return {"scan": cmd_scan, "run": cmd_run, "check": cmd_check, "telegram-id": cmd_telegram_id}[args.command](args, settings, secrets)
    except (ValueError, TypeError, OSError, KeyError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
