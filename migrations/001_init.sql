-- Vanessa Güzellik Merkezi — başlangıç şeması
-- Taşınabilir şablon: app/db.py makroları hedef motora göre derler.
--   @ID   → birincil anahtar (SQLite: INTEGER PK AUTOINCREMENT, PG: BIGINT IDENTITY)
--   @REF  → yabancı anahtar sütun tipi
--   @TS   → zaman damgası (UTC)      @DATE → tarih      @TIME → saat (HH:MM)
--   @JSON → JSON                      @NOW  → şimdiki zaman varsayılanı
-- Para alanları kuruş cinsinden tam sayıdır (*_kurus). Boolean alanlar SMALLINT 0/1'dir.

-- =====================================================================
-- PERSONEL
-- =====================================================================
CREATE TABLE staff (
    id              @ID,
    full_name       VARCHAR(120) NOT NULL,
    title           VARCHAR(120),
    phone           VARCHAR(20),
    color           VARCHAR(9)   NOT NULL DEFAULT '#B08D57',
    bio             TEXT,
    photo_path      VARCHAR(255),
    is_active       SMALLINT     NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    sort_order      INTEGER      NOT NULL DEFAULT 0,
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW,
    deleted_at      @TS
);

-- =====================================================================
-- KULLANICILAR VE OTURUMLAR
-- =====================================================================
CREATE TABLE users (
    id                    @ID,
    username              VARCHAR(60)  NOT NULL,
    full_name             VARCHAR(120) NOT NULL,
    email                 VARCHAR(160),
    password_hash         VARCHAR(255) NOT NULL,
    role                  VARCHAR(20)  NOT NULL CHECK (role IN ('admin','reception','specialist')),
    staff_id              @REF REFERENCES staff(id) ON DELETE SET NULL,
    is_active             SMALLINT     NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    must_change_password  SMALLINT     NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
    failed_login_count    INTEGER      NOT NULL DEFAULT 0,
    locked_until          @TS,
    last_login_at         @TS,
    password_changed_at   @TS,
    created_at            @TS          NOT NULL DEFAULT @NOW,
    updated_at            @TS          NOT NULL DEFAULT @NOW,
    deleted_at            @TS
);
CREATE UNIQUE INDEX ux_users_username ON users (username);
CREATE INDEX ix_users_staff ON users (staff_id);

CREATE TABLE user_sessions (
    id              @ID,
    user_id         @REF NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      CHAR(64)     NOT NULL,
    csrf_token      VARCHAR(64)  NOT NULL,
    ip_address      VARCHAR(64),
    user_agent      VARCHAR(255),
    created_at      @TS          NOT NULL DEFAULT @NOW,
    last_seen_at    @TS          NOT NULL DEFAULT @NOW,
    expires_at      @TS          NOT NULL,
    revoked_at      @TS
);
CREATE UNIQUE INDEX ux_user_sessions_token ON user_sessions (token_hash);
CREATE INDEX ix_user_sessions_user ON user_sessions (user_id);

CREATE TABLE login_attempts (
    id              @ID,
    username        VARCHAR(60),
    ip_address      VARCHAR(64),
    success         SMALLINT     NOT NULL CHECK (success IN (0,1)),
    reason          VARCHAR(40),
    created_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_login_attempts_ip ON login_attempts (ip_address, created_at);
CREATE INDEX ix_login_attempts_user ON login_attempts (username, created_at);

-- =====================================================================
-- HİZMETLER
-- =====================================================================
CREATE TABLE services (
    id                      @ID,
    name                    VARCHAR(120) NOT NULL,
    slug                    VARCHAR(140) NOT NULL,
    category                VARCHAR(60),
    short_description       VARCHAR(255),
    description             TEXT,
    image_path              VARCHAR(255),
    duration_minutes        INTEGER      NOT NULL CHECK (duration_minutes > 0 AND duration_minutes <= 600),
    buffer_minutes          INTEGER      NOT NULL DEFAULT 0 CHECK (buffer_minutes >= 0),
    price_kurus             INTEGER      NOT NULL DEFAULT 0 CHECK (price_kurus >= 0),
    campaign_price_kurus    INTEGER      CHECK (campaign_price_kurus IS NULL OR campaign_price_kurus >= 0),
    repeat_interval_days    INTEGER      CHECK (repeat_interval_days IS NULL OR repeat_interval_days > 0),
    tracks_sessions         SMALLINT     NOT NULL DEFAULT 0 CHECK (tracks_sessions IN (0,1)),
    online_booking          SMALLINT     NOT NULL DEFAULT 1 CHECK (online_booking IN (0,1)),
    is_active               SMALLINT     NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    sort_order              INTEGER      NOT NULL DEFAULT 0,
    created_at              @TS          NOT NULL DEFAULT @NOW,
    updated_at              @TS          NOT NULL DEFAULT @NOW,
    deleted_at              @TS
);
CREATE UNIQUE INDEX ux_services_slug ON services (slug);

CREATE TABLE staff_services (
    staff_id        @REF NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    service_id      @REF NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    created_at      @TS  NOT NULL DEFAULT @NOW,
    PRIMARY KEY (staff_id, service_id)
);
CREATE INDEX ix_staff_services_service ON staff_services (service_id);

-- =====================================================================
-- ÇALIŞMA SAATLERİ / MOLA / İZİN
-- =====================================================================
CREATE TABLE working_hours (
    id              @ID,
    staff_id        @REF NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    weekday         SMALLINT     NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0=Pazartesi … 6=Pazar
    start_time      @TIME        NOT NULL,
    end_time        @TIME        NOT NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW,
    CHECK (end_time > start_time)
);
CREATE UNIQUE INDEX ux_working_hours_staff_day ON working_hours (staff_id, weekday);

CREATE TABLE staff_breaks (
    id              @ID,
    staff_id        @REF NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    weekday         SMALLINT     CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 6),  -- NULL = her gün
    start_time      @TIME        NOT NULL,
    end_time        @TIME        NOT NULL,
    label           VARCHAR(60),
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW,
    CHECK (end_time > start_time)
);
CREATE INDEX ix_staff_breaks_staff ON staff_breaks (staff_id);

CREATE TABLE holidays (
    id              @ID,
    staff_id        @REF REFERENCES staff(id) ON DELETE CASCADE,  -- NULL = bütün merkez kapalı
    start_date      @DATE        NOT NULL,
    end_date        @DATE        NOT NULL,
    reason          VARCHAR(160),
    created_by      @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW,
    CHECK (end_date >= start_date)
);
CREATE INDEX ix_holidays_range ON holidays (start_date, end_date);

-- =====================================================================
-- MÜŞTERİLER
-- =====================================================================
CREATE TABLE customers (
    id                  @ID,
    first_name          VARCHAR(80)  NOT NULL,
    last_name           VARCHAR(80)  NOT NULL,
    phone               VARCHAR(20)  NOT NULL,           -- E.164: +905xxxxxxxxx
    email               VARCHAR(160),
    birth_date          @DATE,
    gender              VARCHAR(10),
    source              VARCHAR(30)  NOT NULL DEFAULT 'panel',  -- panel / online / whatsapp / import
    no_show_count       INTEGER      NOT NULL DEFAULT 0,
    is_demo             SMALLINT     NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1)),
    created_by          @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW,
    deleted_at          @TS
);
CREATE UNIQUE INDEX ux_customers_phone_active ON customers (phone) WHERE deleted_at IS NULL;
CREATE INDEX ix_customers_name ON customers (last_name, first_name);
CREATE INDEX ix_customers_birth ON customers (birth_date);

CREATE TABLE customer_consents (
    id              @ID,
    customer_id     @REF NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    channel         VARCHAR(20)  NOT NULL CHECK (channel IN ('whatsapp','sms','email','phone')),
    category        VARCHAR(20)  NOT NULL CHECK (category IN ('service','marketing')),
    granted         SMALLINT     NOT NULL CHECK (granted IN (0,1)),
    source          VARCHAR(40)  NOT NULL,     -- online_form / panel / whatsapp_reply / paper_form
    evidence        VARCHAR(255),              -- form sürümü, metin özeti vb.
    recorded_by     @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW
);
-- Geçmiş korunur; geçerli durum = (customer, channel, category) için en son kayıt.
CREATE INDEX ix_consents_lookup ON customer_consents (customer_id, channel, category, created_at);

CREATE TABLE customer_notes (
    id              @ID,
    customer_id     @REF NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    author_user_id  @REF REFERENCES users(id) ON DELETE SET NULL,
    body            TEXT         NOT NULL,
    is_pinned       SMALLINT     NOT NULL DEFAULT 0 CHECK (is_pinned IN (0,1)),
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW,
    deleted_at      @TS
);
CREATE INDEX ix_customer_notes_customer ON customer_notes (customer_id, created_at);

-- =====================================================================
-- PAKETLER VE SEANSLAR
-- =====================================================================
CREATE TABLE packages (
    id                  @ID,
    service_id          @REF NOT NULL REFERENCES services(id) ON DELETE RESTRICT,
    name                VARCHAR(120) NOT NULL,
    session_count       INTEGER      NOT NULL CHECK (session_count > 0),
    price_kurus         INTEGER      NOT NULL DEFAULT 0 CHECK (price_kurus >= 0),
    session_interval_days INTEGER    CHECK (session_interval_days IS NULL OR session_interval_days > 0),
    is_active           SMALLINT     NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW,
    deleted_at          @TS
);

CREATE TABLE customer_packages (
    id                  @ID,
    customer_id         @REF NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    package_id          @REF REFERENCES packages(id) ON DELETE SET NULL,
    service_id          @REF NOT NULL REFERENCES services(id) ON DELETE RESTRICT,
    name                VARCHAR(160) NOT NULL,
    total_price_kurus   INTEGER      NOT NULL DEFAULT 0 CHECK (total_price_kurus >= 0),
    status              VARCHAR(20)  NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','cancelled','frozen')),
    purchased_at        @TS          NOT NULL DEFAULT @NOW,
    completed_at        @TS,
    notes               TEXT,
    created_by          @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_customer_packages_customer ON customer_packages (customer_id, status);

CREATE TABLE session_areas (
    id                  @ID,
    customer_package_id @REF NOT NULL REFERENCES customer_packages(id) ON DELETE CASCADE,
    area_name           VARCHAR(80)  NOT NULL,      -- Tüm Bacak, Koltuk Altı… (paket tek bölgeyse 'Genel')
    total_sessions      INTEGER      NOT NULL CHECK (total_sessions > 0),
    sort_order          INTEGER      NOT NULL DEFAULT 0,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_session_areas_package ON session_areas (customer_package_id);

-- =====================================================================
-- RANDEVULAR
-- =====================================================================
CREATE TABLE appointments (
    id                  @ID,
    customer_id         @REF NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    staff_id            @REF NOT NULL REFERENCES staff(id) ON DELETE RESTRICT,
    service_id          @REF NOT NULL REFERENCES services(id) ON DELETE RESTRICT,
    customer_package_id @REF REFERENCES customer_packages(id) ON DELETE SET NULL,
    starts_at           @TS          NOT NULL,
    ends_at             @TS          NOT NULL,
    status              VARCHAR(24)  NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending','confirmed','arrived','completed','cancelled','no_show','postponed','reschedule_needed')),
    is_active_slot      SMALLINT     NOT NULL DEFAULT 1 CHECK (is_active_slot IN (0,1)),  -- saat bloke ediyor mu
    source              VARCHAR(20)  NOT NULL DEFAULT 'panel' CHECK (source IN ('panel','online','whatsapp','phone')),
    price_kurus         INTEGER      CHECK (price_kurus IS NULL OR price_kurus >= 0),
    customer_note       VARCHAR(500),
    internal_note       TEXT,
    reschedule_token_hash CHAR(64),
    reschedule_token_expires_at @TS,
    rescheduled_from_id @REF REFERENCES appointments(id) ON DELETE SET NULL,
    created_by          @REF REFERENCES users(id) ON DELETE SET NULL,
    confirmed_at        @TS,
    cancelled_at        @TS,
    completed_at        @TS,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW,
    CHECK (ends_at > starts_at)
);
CREATE INDEX ix_appointments_starts ON appointments (starts_at);
CREATE INDEX ix_appointments_staff_starts ON appointments (staff_id, starts_at);
CREATE INDEX ix_appointments_customer ON appointments (customer_id, starts_at);
CREATE INDEX ix_appointments_status ON appointments (status);
CREATE INDEX ix_appointments_service ON appointments (service_id);
CREATE UNIQUE INDEX ux_appointments_reschedule_token ON appointments (reschedule_token_hash);

CREATE TABLE appointment_status_history (
    id              @ID,
    appointment_id  @REF NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    from_status     VARCHAR(24),
    to_status       VARCHAR(24)  NOT NULL,
    changed_by      @REF REFERENCES users(id) ON DELETE SET NULL,
    channel         VARCHAR(20)  NOT NULL DEFAULT 'panel',   -- panel / whatsapp / online / system
    reason          VARCHAR(255),
    created_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_status_history_appt ON appointment_status_history (appointment_id, created_at);

CREATE TABLE sessions (
    id                  @ID,
    customer_package_id @REF NOT NULL REFERENCES customer_packages(id) ON DELETE CASCADE,
    session_area_id     @REF NOT NULL REFERENCES session_areas(id) ON DELETE CASCADE,
    session_number      INTEGER      NOT NULL CHECK (session_number > 0),
    status              VARCHAR(20)  NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','completed','skipped')),
    appointment_id      @REF REFERENCES appointments(id) ON DELETE SET NULL,
    staff_id            @REF REFERENCES staff(id) ON DELETE SET NULL,
    performed_at        @TS,
    notes               TEXT,
    settings_json       @JSON,             -- cihaz ayarları (J/cm², atış sayısı…)
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE UNIQUE INDEX ux_sessions_area_number ON sessions (session_area_id, session_number);
CREATE INDEX ix_sessions_package ON sessions (customer_package_id);
CREATE INDEX ix_sessions_appointment ON sessions (appointment_id);

-- Müşteri bazında "sonraki işlem tarihi" (tekrar periyodu) takibi.
CREATE TABLE service_followups (
    id                  @ID,
    customer_id         @REF NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    service_id          @REF NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    last_appointment_id @REF REFERENCES appointments(id) ON DELETE SET NULL,
    last_done_on        @DATE        NOT NULL,
    due_on              @DATE        NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'open' CHECK (status IN ('open','notified','booked','dismissed')),
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE UNIQUE INDEX ux_followups_customer_service ON service_followups (customer_id, service_id);
CREATE INDEX ix_followups_due ON service_followups (status, due_on);

-- =====================================================================
-- ÖDEMELER
-- =====================================================================
CREATE TABLE payments (
    id                  @ID,
    customer_id         @REF NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    customer_package_id @REF REFERENCES customer_packages(id) ON DELETE SET NULL,
    appointment_id      @REF REFERENCES appointments(id) ON DELETE SET NULL,
    description         VARCHAR(200) NOT NULL,
    total_kurus         INTEGER      NOT NULL CHECK (total_kurus >= 0),
    discount_kurus      INTEGER      NOT NULL DEFAULT 0 CHECK (discount_kurus >= 0),
    paid_kurus          INTEGER      NOT NULL DEFAULT 0 CHECK (paid_kurus >= 0),
    status              VARCHAR(10)  NOT NULL DEFAULT 'unpaid' CHECK (status IN ('paid','partial','unpaid','void')),
    due_date            @DATE,
    created_by          @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_payments_customer ON payments (customer_id);
CREATE INDEX ix_payments_status ON payments (status);

CREATE TABLE payment_transactions (
    id              @ID,
    payment_id      @REF NOT NULL REFERENCES payments(id) ON DELETE RESTRICT,
    amount_kurus    INTEGER      NOT NULL CHECK (amount_kurus <> 0),   -- negatif = iade
    method          VARCHAR(20)  NOT NULL CHECK (method IN ('cash','card','transfer','other')),
    paid_at         @TS          NOT NULL,
    note            VARCHAR(255),
    recorded_by     @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_payment_tx_payment ON payment_transactions (payment_id);
CREATE INDEX ix_payment_tx_paid_at ON payment_transactions (paid_at);

-- =====================================================================
-- MESAJLAŞMA / OTOMASYON / KAMPANYA
-- =====================================================================
CREATE TABLE message_templates (
    id                  @ID,
    key                 VARCHAR(40)  NOT NULL,   -- appointment_created, appointment_reminder, …
    name                VARCHAR(120) NOT NULL,
    category            VARCHAR(20)  NOT NULL CHECK (category IN ('service','marketing')),
    body                TEXT         NOT NULL,
    buttons_json        @JSON,                  -- [{"id":"CONFIRM","title":"ONAYLIYORUM"}, …]
    provider_template_name VARCHAR(120),        -- Meta onaylı şablon adı
    language            VARCHAR(10)  NOT NULL DEFAULT 'tr',
    is_active           SMALLINT     NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    updated_by          @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE UNIQUE INDEX ux_message_templates_key ON message_templates (key);

CREATE TABLE campaigns (
    id              @ID,
    name            VARCHAR(160) NOT NULL,
    template_id     @REF REFERENCES message_templates(id) ON DELETE SET NULL,
    body_override   TEXT,
    audience_json   @JSON        NOT NULL,     -- {"type":"inactive_days","days":60} vb.
    promo_code      VARCHAR(40),
    status          VARCHAR(20)  NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','scheduled','sending','sent','cancelled')),
    scheduled_at    @TS,
    sent_at         @TS,
    recipient_count INTEGER      NOT NULL DEFAULT 0,
    created_by      @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW
);

CREATE TABLE messages (
    id                  @ID,
    customer_id         @REF REFERENCES customers(id) ON DELETE SET NULL,
    appointment_id      @REF REFERENCES appointments(id) ON DELETE SET NULL,
    campaign_id         @REF REFERENCES campaigns(id) ON DELETE SET NULL,
    template_id         @REF REFERENCES message_templates(id) ON DELETE SET NULL,
    direction           VARCHAR(10)  NOT NULL CHECK (direction IN ('outbound','inbound')),
    channel             VARCHAR(20)  NOT NULL DEFAULT 'whatsapp',
    message_type        VARCHAR(40)  NOT NULL,
    to_phone            VARCHAR(20),
    body                TEXT,
    payload_json        @JSON,
    status              VARCHAR(12)  NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','delivered','read','failed','received')),
    provider            VARCHAR(20)  NOT NULL DEFAULT 'mock',
    provider_message_id VARCHAR(160),
    error_message       VARCHAR(500),
    sent_at             @TS,
    delivered_at        @TS,
    read_at             @TS,
    failed_at           @TS,
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_messages_customer ON messages (customer_id, created_at);
CREATE INDEX ix_messages_status ON messages (status);
CREATE UNIQUE INDEX ux_messages_provider_id ON messages (provider_message_id);

CREATE TABLE automations (
    id                  @ID,
    message_type        VARCHAR(40)  NOT NULL,   -- appointment_reminder, followup_due, birthday, …
    customer_id         @REF REFERENCES customers(id) ON DELETE CASCADE,
    appointment_id      @REF REFERENCES appointments(id) ON DELETE CASCADE,
    campaign_id         @REF REFERENCES campaigns(id) ON DELETE CASCADE,
    message_id          @REF REFERENCES messages(id) ON DELETE SET NULL,
    dedupe_key          VARCHAR(160) NOT NULL,   -- ör. reminder:appt:42:24h — çift gönderimi engeller
    status              VARCHAR(12)  NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','sent','failed','cancelled','skipped')),
    scheduled_at        @TS          NOT NULL,
    sent_at             @TS,
    attempts            INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at     @TS,
    locked_at           @TS,
    provider_message_id VARCHAR(160),
    error_message       VARCHAR(500),
    created_at          @TS          NOT NULL DEFAULT @NOW,
    updated_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE UNIQUE INDEX ux_automations_dedupe ON automations (dedupe_key);
CREATE INDEX ix_automations_due ON automations (status, scheduled_at);
CREATE INDEX ix_automations_appointment ON automations (appointment_id);

CREATE TABLE campaign_recipients (
    id              @ID,
    campaign_id     @REF NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    customer_id     @REF NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    message_id      @REF REFERENCES messages(id) ON DELETE SET NULL,
    status          VARCHAR(12)  NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','delivered','read','failed','skipped')),
    skip_reason     VARCHAR(60),
    created_at      @TS          NOT NULL DEFAULT @NOW,
    updated_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE UNIQUE INDEX ux_campaign_recipient ON campaign_recipients (campaign_id, customer_id);

CREATE TABLE webhook_events (
    id                  @ID,
    provider            VARCHAR(20)  NOT NULL,
    provider_event_id   VARCHAR(200) NOT NULL,
    event_type          VARCHAR(40)  NOT NULL,
    payload_json        @JSON        NOT NULL,
    processed_at        @TS,
    error_message       VARCHAR(500),
    created_at          @TS          NOT NULL DEFAULT @NOW
);
CREATE UNIQUE INDEX ux_webhook_events ON webhook_events (provider, provider_event_id);

-- =====================================================================
-- BİLDİRİM / AUDIT / AYARLAR
-- =====================================================================
CREATE TABLE notifications (
    id              @ID,
    user_id         @REF REFERENCES users(id) ON DELETE CASCADE,  -- NULL = bütün yöneticilere
    kind            VARCHAR(40)  NOT NULL,
    title           VARCHAR(200) NOT NULL,
    body            VARCHAR(500),
    link            VARCHAR(255),
    customer_id     @REF REFERENCES customers(id) ON DELETE CASCADE,
    appointment_id  @REF REFERENCES appointments(id) ON DELETE CASCADE,
    dedupe_key      VARCHAR(160),
    read_at         @TS,
    created_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_notifications_unread ON notifications (user_id, read_at, created_at);
CREATE UNIQUE INDEX ux_notifications_dedupe ON notifications (dedupe_key);

CREATE TABLE audit_logs (
    id              @ID,
    user_id         @REF REFERENCES users(id) ON DELETE SET NULL,
    actor_label     VARCHAR(120),            -- kullanıcı silinse bile isim kalsın
    action          VARCHAR(60)  NOT NULL,   -- appointment.reschedule, payment.create …
    entity_type     VARCHAR(40),
    entity_id       @REF,
    summary         VARCHAR(500),
    before_json     @JSON,
    after_json      @JSON,
    ip_address      VARCHAR(64),
    created_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_audit_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX ix_audit_created ON audit_logs (created_at);
CREATE INDEX ix_audit_user ON audit_logs (user_id, created_at);

CREATE TABLE settings (
    key             VARCHAR(80)  PRIMARY KEY,
    value_json      @JSON        NOT NULL,
    updated_by      @REF REFERENCES users(id) ON DELETE SET NULL,
    updated_at      @TS          NOT NULL DEFAULT @NOW
);
