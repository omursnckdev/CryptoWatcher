# CryptoWatcher

[BISTWatcher](https://github.com/omursnckdev/BISTWatcher)'ın kripto karşılığı: **Binance USDⓈ-M Futures TESTNET** üzerinde
kaldıraçlı **long ve short** pozisyon açan, teknik göstergeleri + işlem hacmini + haber akışını birlikte değerlendiren,
yaptığı her işlemi **Telegram**'dan bildiren bir bot.

> **Yalnızca testnet.** `TradingClient` testnet dışındaki bir adrese emir göndermeyi kodda reddeder
> (`binance.py` → `TRADING_HOSTS`). Gerçek Binance API anahtarı girmeyin. Bu bir yatırım tavsiyesi değildir;
> stratejinin geçmiş veriyle doğrulanmış bir kârlılığı **yoktur** (bkz. [Sınırlamalar](#sınırlamalar-ve-dürüst-notlar)).

## Hızlı başlangıç

Python 3.12+:

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -e .

cryptowatcher scan --demo          # anahtarsız, çevrimdışı deneme (sentetik veri)
cryptowatcher scan                 # gerçek piyasa verisiyle skor tablosu, emir göndermez
```

### Testnet ve Telegram kurulumu

1. **Testnet anahtarı:** <https://testnet.binancefuture.com> → GitHub ile giriş → *API Key* sekmesi.
   (Testnet hesabına otomatik sanal USDT verilir.)
2. **Telegram botu:** [@BotFather](https://t.me/BotFather) ile bot oluşturup token alın. Bota bir mesaj yazın, sonra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` adresinden `chat.id` değerini bulun.
3. `.env.example` dosyasını `.env` olarak kopyalayıp dört değeri doldurun.
4. Bağlantıları doğrulayın:

```bash
cryptowatcher check                # anahtarlar, bakiye, Telegram test mesajı, veri ve haber kaynakları
cryptowatcher run --dry-run        # sinyalleri Telegram'a yollar, EMİR GÖNDERMEZ (anahtar gerekmez)
cryptowatcher run                  # testnet'te gerçekten işlem açar
```

`run --once` tek bir tarama/yönetim turu yapıp çıkar. `Ctrl+C` botu durdurur; açık pozisyonlar borsadaki
stop/TP emirleriyle korunmaya devam eder, yeniden başlatınca bot durumu `state/state.json`'dan okuyup korumaları doğrular.

## Ne yapar?

Her 5 dakikada (ayarlanabilir) evreni tarar; **yalnızca kapanmış mumları** kullanır.

**Evren:** Binance'in gerçek (mainnet, imzasız/public) 24 saatlik USDT hacmine göre en likit ilk 25 perpetual
(stablecoin, endeks sözleşmeleri ve 30 günden yeni listelemeler hariç) ∩ testnet'te işlem gören semboller.
BTC/ETH ile sınırlı değildir; `min_quote_volume` ile altcoin eşiğini siz belirlersiniz.

**Skor (0–100, long ve short ayrı hesaplanır, her puan nedeniyle raporlanır):**

| Bileşen | Puan | İçerik |
|---|---|---|
| Trend | 20 | Fiyat/EMA20-50-200 dizilimi, ADX>20 ve +DI/−DI, son 10 mumda EMA50 tarafında kalma |
| Momentum | 15 | RSI bandı (long 50–70, short 30–50), MACD/sinyal, histogram ivmesi |
| Bollinger | 10 | %B konumu, hacimli bant kırılımı |
| Hacim / akış | 15 | Hacim oranı>1.2 ve mum yönü, OBV, taker alış/satış baskısı |
| Türev | 5 | Funding oranı yönün aleyhine kalabalık değil |
| Üst zaman dilimi | 10 | 4s EMA50, EMA20/50, MACD |
| BTC'ye göre güç | 10 | 6/24/72 mumda BTC'ye göre getiri farkı |
| BTC rejimi | 5 | BULL / NEUTRAL / BEAR / HIGH_VOLATILITY |
| **Haber** | 10 | CoinDesk, Cointelegraph, Decrypt, The Block, CryptoSlate RSS başlıkları; anahtar kelime sözlüğü + zaman ağırlığı |

Eksik veri **nötr puan almaz**: haber bulunamazsa (ya da BTC için "BTC'ye göre güç" yoksa) skor mevcut puanlar üzerinden
normalize edilir ve rapor bunu `unavailable` olarak gösterir. Haber ayrıca bir **veto** işlevi görür: pozisyon yönünün aleyhine
güçlü duygu (`news_veto`) işlemi engeller (hack, delist, dava, çöküş…; kaldıraçlı işlemde en tehlikeli haber türleri).

**Giriş koşulu:** skor ≥ `entry_score` (70; BTC rejimine ters yönde +10) ve hiçbir *blocker* yok: BTC aşırı oynak, aşırı
funding, ATR bandı dışı, sinyal mumundan beri fiyat > 1 ATR kaymış, aleyhte haber, risk planı kurulamıyor.

**Risk yönetimi:**
- Stop = 2×ATR, hedef = 2R. Miktar = `min(risk bütçesi, marj bütçesi)`: işlem başına sermayenin %1'i risk, pozisyon başına
  en çok %20 marj. Stop, tasfiye fiyatından önce gelmeyecek kadar genişse (`stop > 0.6 × giriş/kaldıraç`) işlem açılmaz.
- Varsayılan 5x, **ISOLATED** marj, one-way mod. En çok 5 açık pozisyon (aynı yönde 3), günlük gerçekleşen zarar %5'i
  geçerse yeni işlem yok, kapanan sembol için 2 saat bekleme.
- Borsa tarafında `STOP_MARKET` + `TAKE_PROFIT_MARKET` (Binance'in yeni **Algo Order API**'si; eski `/fapi/v1/order`
  bu tipler için -4120 verir). **Stop konulamazsa pozisyon hemen kapatılır.**
- +1R'de stop, komisyonu karşılayacak şekilde girişin hemen ötesine çekilir (önce yeni stop konur, sonra eskisi iptal edilir);
  48 saat sonra zaman stopu.

## Telegram bildirimleri

Her işlem Türkçe bildirilir: 🟢 LONG / 🔴 SHORT açıldı (kaldıraç, giriş, miktar, stop, hedef, marj, risk, skor, gerekçeler, haber başlıkları),
🛡 stop başa çekildi, ✅/❌ pozisyon kapandı (sebep: stop / TP / başa baş / zaman / dış müdahale; net PnL, R katı, komisyon, süre),
⚠️ uyarılar (emir hataları, korumasız pozisyon, izlenmeyen pozisyon), 📊 günlük özet.

Komutlar (yalnızca `TELEGRAM_CHAT_ID` sahibinden kabul edilir; bot **çalışırken** cevap verir, Telegram'daki `/` menüsüne otomatik eklenir):

| Komut | Ne gösterir |
|---|---|
| `/positions` (`/pozisyon`) | Açık pozisyonlar: giriş → anlık fiyat, PnL (USDT), ROE, R katı, stop/hedef, tasfiye fiyatı, süre, toplam gerçekleşmemiş |
| `/pnl` (`/kar`) | Bugün gerçekleşen + açık pozisyonların gerçekleşmemiş PnL'i, tüm zamanlar toplamı ve isabet oranı, son 5 işlem |
| `/status` (`/durum`) | Bot durumu, bakiye, BTC rejimi |
| `/top` | En yüksek skorlu 5 coin |
| `/pause` · `/resume` | Yeni işlem açmayı durdur / sürdür (açık pozisyonlar yönetilmeye devam eder) |

## Ayarlar

`config/settings.toml` (hepsi isteğe bağlı; varsayılanlar kod içinde). Hatalı değerler başlangıçta reddedilir.
Başlıcaları: `leverage`, `risk_fraction`, `capital_usdt`, `max_open_positions`, `entry_score`, `min_quote_volume`,
`max_symbols`, `include_symbols`, `allow_long/allow_short`, `atr_stop_multiplier`, `tp_r`, `breakeven_r`, `max_hold_hours`,
`news_enabled`. Başka bir dosya için `cryptowatcher --config yol.toml ...`.

## Sınırlamalar ve dürüst notlar

- **Testnet verisi gerçek değildir.** Testnet fiyat/hacimleri yapay (ör. küçük coinlerde milyarlarca dolarlık hacim). Bu yüzden
  analiz, `market_data = "auto"` ile gerçek mainnet public verisini kullanır; ulaşılamazsa testnet verisine **uyarıyla** düşer
  ve skorlar anlamsız olur. Mainnet'e erişim bazı ülke/IP'lerde engelli olabilir (HTTP 451); o durumda `scan` çıktısında uyarı görürsünüz.
  Emirler her koşulda yalnızca testnet'e gider; stop/hedef yüzde olarak hesaplanıp testnet dolum fiyatına uygulanır.
- Testnet'in derinliği sığdır; dolum fiyatı ve kayma gerçek piyasadan farklıdır. Testnet sonuçları gerçek performansı **göstermez**.
- **Strateji geriye dönük test edilmedi.** Puan ağırlıkları ve eşikler makul başlangıç varsayımlarıdır (BISTWatcher'daki gibi);
  kârlılık iddiası yoktur. Kaldıraç, kayıpları da büyütür.
- İmzalı uç noktalar (hesap, emir, algo emir) birim testlerde sahte borsayla doğrulandı; public uçlar ve haber/Telegram
  akışları gerçek servislere karşı denendi. İlk gerçek testnet çalışmasında `cryptowatcher check` ve kısa bir gözlem önerilir.
- Haber duygusu basit bir sözlük yaklaşımıdır (başlıklarda geçen ticker/coin adı eşleştirilir); ironi ve bağlamı anlamaz.
  Küçük altcoinler için çoğu zaman haber bulunmaz, bu durumda skora katılmaz.
- Açık interest, likidasyon akışı, emir defteri derinliği ve korelasyon yönetimi henüz yok.

## Geliştirme

```bash
python -m unittest discover -s tests -v    # 83 test; ağ gerektirmez
cryptowatcher scan --demo --json
```

Kaynak düzeni: `config.py` (ayarlar/sırlar) · `binance.py` (REST istemcisi, testnet kilidi) · `data.py` (mum/ticker/evren, demo veri) ·
`indicators.py` · `strategy.py` (skor + risk planı, saf fonksiyonlar) · `engine.py` (tarama) · `news.py` · `executor.py` (emir/pozisyon
yaşam döngüsü) · `bot.py` (döngü, komutlar) · `telegram.py` · `state.py` · `cli.py`.
