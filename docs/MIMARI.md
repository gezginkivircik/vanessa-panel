# Vanessa Güzellik Merkezi — Sistem Mimarisi

Bu doküman sistemin teknik mimarisini, veritabanı yapısını, ekranlarını ve geliştirme fazlarını tanımlar.
Her faz tamamlandıkça güncellenir.

---

## 1. Teknoloji seçimi ve gerekçesi

| Katman | Seçim | Not |
|---|---|---|
| Backend | **Python 3.12+ / Flask 3** | Sunucu tarafı render (Jinja2) + JSON API. Bütün yetki kontrolleri sunucuda. |
| Veritabanı | **PostgreSQL 16** (üretim), SQLite (geliştirme/test) | Şema iki motorda da birebir aynı mantıkla kurulur. Sürücü farkı yalnızca `app/db.py` içindedir. |
| Kimlik doğrulama | Sunucu tarafı oturum tablosu + HttpOnly cookie | Şifreler `scrypt` ile tuzlanarak hash'lenir. Token veritabanında yalnızca SHA-256 özeti olarak saklanır. |
| Arayüz | Jinja2 şablonları + elle yazılmış, hafif CSS/JS | Framework bağımlılığı yok, mobil öncelikli, hızlı ilk açılış. |
| Zamanlanmış görevler | DB tabanlı iş kuyruğu (`automations`) + worker süreci | Faz 9'da. Cron/systemd timer ile `flask run-scheduler` çağrılır. Tekrarlı gönderim `UNIQUE(dedupe_key)` ile engellenir. |
| WhatsApp | `WhatsAppProvider` soyutlaması: `MockProvider` / `MetaCloudProvider` | Faz 8'de. Credential yokken MOCK modda çalışır. Secret'lar yalnızca ortam değişkeninde tutulur. |

**Neden Next.js değil?** Proje Next.js/Prisma ile başlamak üzere planlandı, ancak bu geliştirme ortamında paket
kayıtlarına (npm/pip) erişim yok. Bu yüzden kodu burada çalıştırıp test etmek mümkün olmazdı. Kullanıcının
onayıyla, ortamda hazır bulunan ve üretimde de olgun olan Flask + PostgreSQL yığını seçildi. Böylece her faz burada
gerçekten çalıştırılıp test edilir.

### Katmanlar

```
Tarayıcı (müşteri sitesi / yönetim paneli)
   │  HTTPS, HttpOnly session cookie, CSRF token
   ▼
Flask uygulaması
   ├── blueprints (public, auth, panel, api, webhooks)   ← HTTP katmanı, form doğrulama
   ├── services (customers, appointments, sessions, payments, messaging, automation)  ← iş kuralları
   ├── security (auth, rbac, csrf, rate limit, audit)
   ├── integrations/whatsapp (provider abstraction)
   └── db (bağlantı, transaction, dialect adaptörü)
   ▼
PostgreSQL  ←  worker (scheduler + message queue)
```

Kural: **Route'lar SQL yazmaz, servisleri çağırır.** Servisler transaction açar, iş kuralını uygular, audit log yazar.

---

## 2. Güvenlik tasarımı

- **Şifreler:** `hashlib.scrypt` (n=2^15, r=8, p=1, 16 byte salt). Plaintext asla saklanmaz.
- **Oturum:** 256 bit rastgele token → cookie (`HttpOnly`, `SameSite=Lax`, üretimde `Secure`). DB'de token'ın
  SHA-256 özeti tutulur. Böylece DB sızıntısında oturum çalınamaz. 12 saat boşta kalınca oturum düşer, mutlak ömrü 7 gündür.
  Çıkışta oturum iptal edilir. Şifre değişince kullanıcının diğer oturumları da iptal edilir.
- **CSRF:** Her oturuma özel token, bütün POST/PUT/PATCH/DELETE isteklerinde zorunlu (webhook hariç; webhook imza ile doğrulanır).
- **Rate limit:** DB tabanlı (çoklu süreçte de çalışır). IP başına 15 dakikada 20 giriş denemesi yapılabilir.
  Kullanıcı adı başına 5 hatalı denemeden sonra hesap 15 dakika kilitlenir.
- **RBAC:** Rol → izin haritası (`app/security/rbac.py`). Her route `@require_permission(...)` ile korunur.
  Şablonda buton gizlemek yetki sayılmaz, her kontrol sunucuda yapılır.
- **Audit log:** Giriş/çıkış, başarısız giriş, kullanıcı/müşteri/randevu/ödeme değişiklikleri; kim, ne zaman, hangi
  kayıt, önce/sonra değerleri (JSON).
- **Güvenlik başlıkları:** CSP, X-Frame-Options=DENY, X-Content-Type-Options, Referrer-Policy, Permissions-Policy.
- **KVKK:** `customer_consents` tablosunda kanal (whatsapp/sms/email) ve **kategori ayrı** tutulur:
  `service` (randevu/hizmet bildirimleri) ve `marketing` (kampanya). Kampanya gönderimi yalnızca geçerli
  `marketing` onayı olan müşterilere yapılır. İzin geri alınabilir, her değişiklik tarihçeli kaydedilir.
- **Kişisel veri görünürlüğü:** Uzman rolü yalnızca kendi randevusu olan müşterileri görür, finansı göremez.
  Dışa aktarma yalnızca yöneticiye açıktır ve audit log'a yazılır.
- **Para:** Bütün tutarlar **kuruş cinsinden tam sayı** (`*_kurus`) olarak saklanır. Kayan nokta hatası olmaz.
- **Zaman:** Bütün zaman damgaları UTC ISO-8601 olarak saklanır, arayüzde Europe/Istanbul'a çevrilir.

---

## 3. Veritabanı şeması

Dosya: `migrations/001_init.sql` (taşınabilir şablon) → `app/db.py` hedef motora göre derler.
PostgreSQL'e özel ek kısıtlar: `migrations/postgres_extras.sql`.

### Kimlik ve yetki
| Tablo | Amaç |
|---|---|
| `users` | Panel kullanıcıları: username, password_hash, role (`admin`/`reception`/`specialist`), staff_id, kilit alanları |
| `user_sessions` | Sunucu tarafı oturumlar (token özeti, csrf, son görülme, iptal) |
| `login_attempts` | Rate limit ve güvenlik izi |
| `audit_logs` | Kim, ne zaman, hangi işlem, önce/sonra |

### Personel ve hizmet
| Tablo | Amaç |
|---|---|
| `staff` | Uzmanlar (ad, unvan, renk, aktif) |
| `services` | Hizmetler: ad, slug, açıklama, görsel, süre, fiyat, kampanyalı fiyat, **tekrar süresi (gün)**, seans takibi, aktif |
| `staff_services` | Hangi uzman hangi hizmeti yapar (N-N) |
| `working_hours` | Uzman × haftanın günü × başlangıç/bitiş |
| `staff_breaks` | Tekrarlayan molalar (gün + saat aralığı) |
| `holidays` | İzin/kapalı gün (staff_id NULL ise bütün merkez kapalı) |

### Müşteri ve CRM
| Tablo | Amaç |
|---|---|
| `customers` | Ad, soyad, telefon (E.164 normalize), doğum tarihi, kaynak, **no_show_count**, `is_demo`, `deleted_at` |
| `customer_consents` | KVKK izinleri: kanal × kategori × durum × kaynak × tarih |
| `customer_notes` | Dahili notlar (yazan kullanıcı + tarih, müşteriye görünmez) |

### Randevu
| Tablo | Amaç |
|---|---|
| `appointments` | customer, staff, service, starts_at, ends_at, status, kaynak, fiyat, `customer_package_id`, `session_id` |
| `appointment_status_history` | Her durum değişikliği (eski → yeni, kim, neden, kanal) |

Durumlar: `pending` BEKLEMEDE · `confirmed` ONAYLANDI · `arrived` GELDİ · `completed` İŞLEM TAMAMLANDI ·
`cancelled` İPTAL · `no_show` GELMEDİ · `postponed` ERTELENDİ · `reschedule_needed` YENİDEN PLANLAMA GEREKİYOR.
Aktif durumlar (saat bloke eder): `pending`, `confirmed`, `arrived`, `reschedule_needed`.

**Çakışma koruması (iki katman):**
1. Servis katmanı yazma transaction'ı içinde (`BEGIN IMMEDIATE` / PG'de `SELECT … FOR UPDATE` + advisory lock) çakışma kontrolü yapar.
2. PostgreSQL'de `EXCLUDE USING gist (staff_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (aktif)` kısıtı
   veritabanı seviyesinde son garanti verir. Eş zamanlı iki istekten yalnızca biri kazanır.

### Paket ve seans
| Tablo | Amaç |
|---|---|
| `packages` | Paket tanımı (ör. 10 seans lazer, fiyat) |
| `customer_packages` | Müşterinin satın aldığı paket: toplam seans, durum, toplam tutar |
| `session_areas` | Paket içindeki bölgeler (Tüm Bacak 10, Koltuk Altı 10, …) |
| `sessions` | Tekil seans: area, sıra no (1..N), tarih, uzman, not, durum, randevu bağlantısı |

### Ödeme
| Tablo | Amaç |
|---|---|
| `payments` | Borç kalemi (paket/işlem): toplam, indirim, durum (`paid`/`partial`/`unpaid`) |
| `payment_transactions` | Tahsilat hareketleri (tarih, tutar, yöntem: nakit/kart/havale, kaydeden) |

### Mesajlaşma ve otomasyon
| Tablo | Amaç |
|---|---|
| `message_templates` | Şablon anahtarı, kategori (`service`/`marketing`), gövde, butonlar, provider şablon adı/dili |
| `messages` | Gönderilen/gelen her mesaj; durum (`queued`/`sent`/`delivered`/`read`/`failed`), provider_message_id, hata |
| `automations` | İş kuyruğu: created_at, scheduled_at, sent_at, status, customer_id, appointment_id, message_type, provider_message_id, error_message, attempts, **dedupe_key (UNIQUE)** |
| `webhook_events` | Gelen webhook olayları, `provider_event_id UNIQUE` → idempotency |
| `campaigns` / `campaign_recipients` | Kampanya, hedef kitle filtresi (JSON), alıcı başına durum |
| `notifications` | Panel bildirim merkezi |

### Sistem
| Tablo | Amaç |
|---|---|
| `settings` | Anahtar/değer (işletme adı, hatırlatma süreleri `[24, 2]`, para birimi, …) |
| `schema_migrations` | Uygulanan migration'lar |

### İndeksler (sık sorgular)
`customers(phone)` UNIQUE (silinmemişler için), `customers(last_name, first_name)`, `appointments(starts_at)`,
`appointments(staff_id, starts_at)`, `appointments(status)`, `appointments(customer_id)`, `appointments(service_id)`,
`automations(status, scheduled_at)`, `messages(customer_id, created_at)`, `audit_logs(entity_type, entity_id)`,
`sessions(customer_package_id)`, `payment_transactions(payment_id)`.

---

## 4. Ekranlar

### A — Müşteri web sitesi (herkese açık)
Ana sayfa (hero + Randevu Al + WhatsApp) · Hakkımızda · Hizmetler (DB'den) · Kampanyalar · Online Randevu
(hizmet → uzman → tarih → uygun saat → ad/telefon → onay) · İletişim/Konum · Randevu değiştirme bağlantısı (imzalı, süreli token).

### B — Yönetim paneli (`/panel`, giriş zorunlu)
| Ekran | Roller |
|---|---|
| Dashboard (bugün/yarın/bekleyen/iptal/ciro kartları, sıradaki randevu) | hepsi (ciro yalnızca yönetici) |
| Takvim (gün/hafta/ay, uzman sütunları) | hepsi (uzman: yalnız kendi) |
| Müşteriler + hızlı arama + hızlı ekle | yönetici, resepsiyon (uzman: kendi müşterileri, salt okunur) |
| Müşteri profili (özet, timeline, seanslar, ödemeler, mesajlar, notlar, izinler) | rol bazlı sekmeler |
| Hizmetler, Personel ve çalışma saatleri, İzinler | yönetici |
| WhatsApp şablonları, mesaj kayıtları, gönderilemeyen mesajlar | yönetici (kayıtları resepsiyon da görür) |
| Kampanyalar | yönetici |
| Raporlar, Dışa aktarma | yönetici |
| Kullanıcılar, Ayarlar, Audit log | yönetici |
| Bildirim merkezi | hepsi |

Mobilde alt sekme çubuğu: **Bugün · Randevular · + (Randevu/Müşteri) · Müşteriler · Menü**.

---

## 5. Geliştirme fazları

| Faz | Kapsam | Durum |
|---|---|---|
| 1 | Veritabanı şeması, migration, auth, roller, CSRF, rate limit, audit log, demo veri, panel iskeleti | ✅ (34 test) |
| 2 | Müşteri CRM: liste, arama, hızlı ekle, profil, notlar, KVKK izinleri | ✅ (+22 test) |
| 3 | Hizmet ve personel yönetimi, çalışma saatleri, mola, izin, görsel, randevu ayarları | ✅ (+14 test) |
| 4 | Randevu motoru (uygun saat hesaplama, çakışma kontrolü), durumlar, takvim | ✅ (+14 test; Faz 5–6 temeli: seans düşümü ve tekrar tarihi) |
| 5 | Lazer paket/seans ve bölge takibi | ⏳ |
| 6 | Tekrar periyotları (sonraki işlem tarihi) | ⏳ |
| 7 | Ödeme ve tahsilat hareketleri | ⏳ |
| 8 | WhatsApp provider katmanı (Mock + Meta Cloud API) | ⏳ |
| 9 | Scheduler + kuyruk + otomatik mesajlar | ⏳ |
| 10 | Webhook (imza doğrulama, idempotency), buton cevaplarıyla randevu onayı | ⏳ |
| 11 | Raporlama, dışa aktarma (CSV/XLSX) | ⏳ |
| 12 | Müşteri web sitesi + online randevu | ⏳ |
| 13 | Mobil optimizasyon, güvenlik sertleştirme, uçtan uca testler | ⏳ |

Her fazın sonunda bütün test paketi (`python -m unittest`) çalıştırılır. Önceki fazların testleri geçmeden yeni faza geçilmez.

---

## 6. Üretim ortamı: yedekleme, geri yükleme, loglama, izleme

- **Yedekleme:** PostgreSQL için günlük `pg_dump -Fc` (30 gün saklama) ve WAL arşivleme ile anlık geri dönüş (PITR).
  Yedekler şifreli olarak sunucu dışında, ayrı bir bölgedeki nesne depolamada tutulur. Yönetilen PostgreSQL
  (ör. Neon, Supabase, AWS RDS) kullanılırsa otomatik yedek + PITR açık tutulur. Örnek komut: `scripts/backup.sh`.
- **Geri yükleme testi:** Ayda bir yedek boş bir veritabanına `pg_restore` ile açılır, `flask check-db` ile doğrulanır.
- **Loglama:** JSON satır loglar (stdout), istek kimliği ile. Kişisel veri (telefon, isim) loglara yazılmaz.
- **Hata izleme:** `SENTRY_DSN` tanımlıysa Sentry entegrasyonu (opsiyonel). Gönderilemeyen mesajlar panelde ayrıca listelenir.
- **Sağlık kontrolü:** `/healthz` (DB bağlantısı + kuyruk gecikmesi).

---

## 7. Ortam değişkenleri

| Değişken | Açıklama |
|---|---|
| `VANESSA_SECRET_KEY` | Uygulama gizli anahtarı (üretimde zorunlu, ≥32 karakter) |
| `DATABASE_URL` | `postgresql://…` veya `sqlite:///instance/vanessa.db` |
| `VANESSA_ENV` | `development` / `production` |
| `WHATSAPP_PROVIDER` | `mock` (varsayılan) / `meta` |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | Meta WhatsApp Cloud API (Faz 8'de istenecek) |

---

## 8. Canlıya alma (Render.com örneği)

1. Kod bir GitHub deposuna yüklenir.
2. Render'da **New → Blueprint** seçilip depo bağlanır; `render.yaml` web servisini ve PostgreSQL'i birlikte oluşturur.
3. İlk açılışta `flask init-db` migration'ları uygular (Dockerfile CMD).
4. Render Shell'den `flask create-user` ile ilk yönetici oluşturulur.
5. Alan adı (ör. `panel.vanessaguzellik.com`) Render'da Custom Domain olarak eklenir, HTTPS otomatik gelir.

Docker destekleyen her ortamda (Railway, Fly.io, kendi VPS) aynı `Dockerfile` kullanılabilir.
