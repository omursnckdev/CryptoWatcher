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
| **Haber** | 10 | CoinDesk, Cointelegraph, Decrypt, The Block RSS başlıkları; anahtar kelime sözlüğü + zaman ağırlığı |

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

## Telegram

**Bot yalnızca iki durumda mesaj atar:** (1) siz bir komut yazınca, (2) bir işlem **açıldığında, kapandığında veya revize edildiğinde**
(stop başa çekilince / yeniden konunca). Başlangıç-durdurma mesajı, günlük özet ve hata uyarısı gönderilmez; hatalar yalnızca log'a yazılır. Tek istisna korumasız kalan pozisyon gibi güvenlik uyarılarıdır.
(`run --dry-run` modunda emir olmadığından, "açılacak" işlemler her mumda tek bir özet mesajında gelir.)

```
🟢 LONG SOLUSDT 5x @ 150.20 · skor 84
SL 147.0 (-2.1%) · TP 156.8 (+4.4%) · risk 100 USDT · 📰 +0.3 (3)

🛡 SOLUSDT stop başa baş: 150.35

✅ SOLUSDT LONG kapandı · hedef
150.20 → 156.80 · +195.30 USDT (+1.9R) · 3sa 12dk
```
`telegram_verbose = true` yaparsanız açılış mesajına gerekçeler, haber başlıkları, marj ve funding da eklenir.

Komutlar (bot **çalışırken** cevap verir, Telegram'ın `/` menüsüne otomatik eklenir):

| Komut | Ne gösterir |
|---|---|
| `/positions` (`/pozisyon`) | Açık pozisyonlar, pozisyon başına 2 satır: PnL (USDT, %, R), giriş → anlık fiyat, SL/TP, süre; toplam açık PnL |
| `/pnl` (`/kar`) | Bugün (kapanan + açık), tüm zamanlar, isabet oranı, son 5 işlem |
| `/analiz` (`/gunluk`) | Kapanan işlemlerin teşhisi: neden kaybetti/kazandı, stoptan sonra fiyat ne yaptı |
| `/status` (`/durum`) | Bot durumu, bakiye, BTC rejimi |
| `/top` | En yüksek skorlu 5 coin |
| `/pause` · `/resume` | Yeni işlem açmayı durdur / sürdür (açık pozisyonlar yönetilmeye devam eder) |

### Grupta kullanma

1. Botu gruba ekleyin. Gruba bir mesaj yazın, örneğin `/status@BOT_ADINIZ`.
2. `cryptowatcher telegram-id` çalıştırın: gruplar ve kullanıcı id'leri listelenir. Grup id'si **negatiftir** (`-100...`);
   `.env` içinde `TELEGRAM_CHAT_ID=` satırına onu yazın (kişisel id'niz değil).
3. **Gizlilik modu:** Telegram gruplarda botlara varsayılan olarak yalnızca `/komut@bot_adı` biçimindeki komutları iletir.
   Menüden seçince bu ek otomatik gelir; elle yazarken `/positions@BOT_ADINIZ` yazın. Düz `/positions` yazabilmek için
   @BotFather → `/setprivacy` → botu seçin → **Disable**, ardından botu gruptan çıkarıp **yeniden ekleyin** (ayar yeni eklenmede geçerli olur).
4. Gruptaki herkes okuma komutlarını kullanabilir; `/pause` ve `/resume` yalnızca `.env` içindeki
   `TELEGRAM_ALLOWED_USER_IDS` listesindeki kullanıcılara açıktır (listeyi `telegram-id` çıktısından alın).
5. Grup "supergroup"a yükseltilirse id değişir; bot log'da yeni id'yi yazar, `.env`'yi güncelleyin.

Bot yalnızca `TELEGRAM_CHAT_ID`'deki sohbeti dinler ve oraya yazar; başka sohbetlerden gelen komutları yok sayar.

**Mesaj freni:** Hangi nedenle olursa olsun sohbete dakikada en fazla 12, saatte en fazla 80 mesaj gider; aynı metin 30 saniyede en fazla 2 kez.
Sınır aşılırsa fazlası bastırılır ve tek bir uyarı gelir (log'da `Telegram flood brake` satırı görünür). Yeniden başlatmada, bot kapalıyken
yazılmış eski komutlar (`/pause`, `/positions` ...) **çalıştırılmaz**, atılır.

## Ayarlar

`config/settings.toml` (hepsi isteğe bağlı; varsayılanlar kod içinde). Hatalı değerler başlangıçta reddedilir.
Başlıcaları: `leverage`, `risk_fraction`, `capital_usdt`, `max_open_positions`, `entry_score`, `min_quote_volume`,
`max_symbols`, `include_symbols`, `allow_long/allow_short`, `atr_stop_multiplier`, `tp_r`, `breakeven_r`, `max_hold_hours`,
`news_enabled`, `telegram_verbose`, `notify_breakeven`. Başka bir dosya için `cryptowatcher --config yol.toml ...`.

## Sunucuda 7/24 çalıştırma (bilgisayar kapalıyken)

Bot yalnızca çalıştığı makine açıkken çalışır. Bilgisayarınız kapanınca durur; **açık pozisyonlar yine de korunur**, çünkü stop/TP emirleri
borsadadır. Durunca yalnızca breakeven güncellemesi, zaman stopu, yeni işlem ve Telegram bildirimleri kesilir. Kesintisiz çalışması için
küçük bir sanal sunucu (VPS) kiralayın: bot yalnızca dışarıya bağlanır, bir port açmanız gerekmez.

**Sunucu seçimi:** Ubuntu 24.04, 1 vCPU / 1-2 GB RAM yeter (genelde ayda birkaç €/$: Hetzner, DigitalOcean, Contabo, Vultr…).
**Konumu Avrupa seçin (Almanya/Finlandiya/Hollanda).** Binance gerçek piyasa verisini ABD veri merkezlerinden engeller (HTTP 451); bot o zaman
anlamsız testnet verisine düşer.

### Yöntem 1: Docker (önerilen)

Windows PowerShell'den sunucuya bağlanın: `ssh root@SUNUCU_IP`. Sunucuda:

```bash
curl -fsSL https://get.docker.com | sh                     # Docker kurulumu
git clone -b claude/nifty-rubin-wn484a https://github.com/omursnckdev/CryptoWatcher.git
cd CryptoWatcher
cp .env.example .env && nano .env                          # anahtarları doldurun (Ctrl+O, Enter, Ctrl+X)
chmod 600 .env
docker compose up -d --build                               # kur ve arka planda başlat
docker compose logs -f --tail 50                           # canlı log (çıkış: Ctrl+C, bot çalışmaya devam eder)
```

`restart: unless-stopped` sayesinde bot çökerse veya sunucu yeniden başlarsa kendiliğinden açılır.

| İş | Komut |
|---|---|
| Durum | `docker compose ps` |
| Güncelleme | `git pull && docker compose up -d --build` |
| Ayar değişikliği (`config/settings.toml`) | `docker compose restart` |
| `.env` değişikliği | `docker compose up -d --force-recreate` |
| Durdurma | `docker compose down` (pozisyonlar borsada korunur) |

### Yalnızca kendi bilgisayarınızda Docker (geçici çözüm)

Docker Desktop (Windows) ile bot arka planda çalışır, terminali kapatsanız da durmaz ve çökerse yeniden açılır. **Ama bilgisayar kapanırsa veya
uykuya geçerse bot da durur** (açık pozisyonlar borsadaki stop/TP ile korunur). Kesintisiz için sunucu şart.

```powershell
cd C:\Users\user\Desktop\CryptoWatcher\CryptoWatcher     # .env dosyasının olduğu klasör
docker compose up -d --build
docker compose logs -f --tail 50
```
Docker Desktop → Settings → General → *Start Docker Desktop when you sign in* açık olsun; Windows güç ayarlarında uykuyu kapatın.

### Yöntem 2: Docker'sız (systemd)

Python **3.12 veya üstü** gerekir (Ubuntu 24.04'te hazır gelir; Ubuntu 22.04 ve Debian 12'de eski olduğundan kurulum başarısız olur; Docker'da bu sorun yoktur).

```bash
apt update && apt install -y python3 python3-venv git
useradd --system --create-home --home-dir /opt/cryptowatcher cryptowatcher
sudo -u cryptowatcher git clone -b claude/nifty-rubin-wn484a https://github.com/omursnckdev/CryptoWatcher.git /opt/cryptowatcher/app
cd /opt/cryptowatcher/app && sudo -u cryptowatcher bash -c 'python3 -m venv .venv && .venv/bin/pip install .'
sudo -u cryptowatcher cp .env.example .env && nano .env && chmod 600 .env
sed 's#/opt/cryptowatcher#/opt/cryptowatcher/app#g' deploy/cryptowatcher.service > /etc/systemd/system/cryptowatcher.service
systemctl daemon-reload && systemctl enable --now cryptowatcher
journalctl -u cryptowatcher -f                             # log
```

| İş | Komut |
|---|---|
| Durum | `systemctl status cryptowatcher` |
| Güncelleme | `cd /opt/cryptowatcher/app && sudo -u cryptowatcher git pull && sudo -u cryptowatcher .venv/bin/pip install . && systemctl restart cryptowatcher` |
| Ayar (`config/settings.toml`) veya `.env` değişikliği | `systemctl restart cryptowatcher` |
| Durdurma | `systemctl stop cryptowatcher` (pozisyonlar borsada korunur) |

### Geçerken dikkat

- **Bilgisayardaki botu kapatın.** İki bot aynı Telegram komutlarını okur (her komuta çift cevap) ve aynı hesapta çift işlem açabilir.
- **Açık pozisyonlar:** Sunucudaki bot, bilgisayardaki botun açtığı pozisyonları tanımaz (durum dosyası `state/state.json` makineye özeldir);
  "izlenmeyen pozisyon" olarak bırakır ve yönetmez. Ya pozisyonlar kapanana kadar bekleyip sonra geçin, ya da `state/state.json` dosyasını taşıyın:
  `scp state\state.json root@SUNUCU_IP:/root/` sonra sunucuda `docker compose create && docker compose cp /root/state.json cryptowatcher:/app/state/state.json && docker compose up -d`.
- **Güvenlik:** `.env` yalnızca sunucuda kalsın ve git'e eklenmesin (`.gitignore`'da). Mümkünse SSH'ı parola yerine anahtarla kullanın
  ve `ufw allow OpenSSH && ufw enable` ile gelen bağlantıları kısıtlayın.
- Bu Docker imajı ve systemd dosyası bu depodaki geliştirme ortamında derlenip denenemedi (orada Docker çalışmıyor); kurulum adımları temiz bir
  Python ortamında simüle edilerek doğrulandı. Sunucuda ilk çalıştırmada `docker compose logs` ve Telegram'dan `/status` ile kontrol edin.

## İşlem günlüğü ve zarar analizi (`/analiz`)

Bot her işlemin **girişte ne gördüğünü** (skor, BTC rejimi, RSI/ADX/hacim, gerekçeler), işlem açıkken **fiyatın nereye kadar gittiğini**
(en iyi / en kötü an, R cinsinden: MFE / MAE) ve kapanıştan **12 saat sonra fiyatın ne yaptığını** `state/journal.jsonl` dosyasına yazar.

- Her kapanış mesajına tek satır eklenir: `↳ en iyi +0.5R · en kötü -1.0R · bir miktar lehine gitti, geri döndü`.
- Teşhis sınıfları: *hedefe ulaştı · hiç lehine gitmedi (en iyi < 0,25R) · bir miktar gitti geri döndü · kârdayken geri verdi · başa baş stop · süre doldu · dışarıdan kapandı*.
- Stop yiyen işlemlerde kapanıştan sonraki 12 saat mainnet 5 dakikalık mumlarla değerlendirilir: fiyat lehimize ≥ 1R döndüyse **stop dar/gürültüye takıldı**,
  aleyhe ≥ 1R devam ettiyse **stop doğru çıkıştı**. Yalnızca göreli hareket kullanılır (işlem testnet fiyatında, mumlar mainnet'ten).
- `/analiz` toplu özeti verir (kazanma, ortalama R, sınıf dağılımı, yön ve skor dilimi bazında ortalama, son işlemler). **En az 20 işlem birikmeden yorum yapmaz**;
  küçük örnekteki rakamlara bakıp ayar değiştirmek yanıltıcıdır.
- Özellik açıldığından önce kapanan işlemler günlükte yoktur (o zaman bu veriler tutulmuyordu). Emir göndermez, hata verirse işlem akışını etkilemez.
  `journal_enabled = false` ile kapatılır. Dosya: `sudo docker compose exec cryptowatcher cat /app/state/journal.jsonl`.

## Gölge kayıt (işlemsiz hipotez testi)

Bot, **tek bir dondurulmuş hipotezi** gerçek zamanlı ve işlem açmadan izler: *"24 saatte açık pozisyon (OI) kendi geçmişinin en düşük %5'ine
çöktü ve fiyat düştü → LONG"*. Geçmiş çalışmada (2023-Haz.2025) 24 saatlik tutuşta +51 baz puan net (t≈2,9) verdi, sonraki dönemde (Tem.2025-Eyl.2026)
+3 baz puana indi. Sinyalin hâlâ yaşayıp yaşamadığını ancak hiç görülmemiş yeni veri söyler.

- **Emir göndermez, Telegram'a otomatik mesaj atmaz.** Her saat evreni tarar; kalıp görülürse `state/shadow.jsonl` dosyasına yazar. Holding süreleri (8, 24, 48 saat)
  bitince sonucu gerçek mumlardan ve gerçek funding'den hesaplar (giriş: tespitten bir saat sonraki açılış; maliyet 14 baz puan).
- `/golge` (veya `/shadow`) komutu özeti gösterir: olay sayısı, ufuk başına ortalama net, kazanma oranı, t, pozitif ay oranı ve **karar**.
- **Kural önceden belirlendi ve değiştirilmez** (`src/crypto_watcher/shadow.py` içindeki `DECISION`): en az 150 örtüşmesiz olay, en az 90 gün, 24 saatte ortalama
  ≥ +15 baz puan, gün-kümeli t ≥ 1,65 ve ayların ≥ %60'ı pozitif. Aksi halde sinyal rafa kalkar. Tanım (eşik, pencere, ufuklar) sonuçlara bakıp ayarlanmamalıdır.
- Yalnızca gerçek (mainnet) veriyle çalışır; testnet verisine düşülürse kapalıdır. `shadow_enabled = false` ile kapatılır.
- Doğrulama: `cryptowatcher check` açık pozisyon geçmişi endpoint'inin okunabildiğini sınar. Geçmiş saatler canlı kodla yeniden oynatıldığında çalışmadaki sinyalin %99,3'ü üretildi.
- Docker'da dosyayı görmek için: `sudo docker compose exec cryptowatcher cat /app/state/shadow.jsonl`.
- Beklenti: yılda ~700-1000 olay, yani karar için yaklaşık 3 ay. Etkinin küçük olması bekleniyor (maliyetin birkaç on baz puan üstü en iyi ihtimalle).

## Sorun giderme

**`CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate`** (Windows'ta sık görülür): Python'un kendi sertifika listesi
eski ya da antivirüs/kurumsal ağ kendi sertifikasını yalnızca Windows'a eklemiştir. Bot artık Windows'un sertifika deposunu kullanır
(`truststore`); güncellemek için `git pull` ve `python -m pip install -e .` yeterlidir. Doğrulama hiçbir zaman kapatılmaz.
`cryptowatcher check` hangi sertifika kaynağının kullanıldığını ilk satırda gösterir. Sorun sürerse antivirüsün "HTTPS taraması"nı
Binance ve haber siteleri için kapatın veya `python -m pip install -U certifi` deneyin.

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
python -m unittest discover -s tests -v    # 132 test; ağ gerektirmez
cryptowatcher scan --demo --json
```

Kaynak düzeni: `config.py` (ayarlar/sırlar) · `binance.py` (REST istemcisi, testnet kilidi) · `data.py` (mum/ticker/evren, demo veri) ·
`indicators.py` · `strategy.py` (skor + risk planı, saf fonksiyonlar) · `engine.py` (tarama) · `news.py` · `executor.py` (emir/pozisyon
yaşam döngüsü) · `bot.py` (döngü, komutlar) · `telegram.py` · `state.py` · `cli.py`.
