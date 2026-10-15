-- Faz 12: Herkese açık formlar için hız sınırı kayıtları (online randevu, iptal).
-- IP adresi açık olarak değil, SHA-256 özeti olarak tutulur.
CREATE TABLE rate_events (
    id          @ID,
    bucket      VARCHAR(40)  NOT NULL,
    key_hash    CHAR(64)     NOT NULL,
    created_at  @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_rate_events_lookup ON rate_events (bucket, key_hash, created_at);
