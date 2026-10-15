-- Faz 6: Takip listesi (zamanı gelen işlemler) — arama sonuçları ve erteleme.
ALTER TABLE service_followups ADD COLUMN reason VARCHAR(20) NOT NULL DEFAULT 'repeat';
ALTER TABLE service_followups ADD COLUMN snoozed_until @DATE;
ALTER TABLE service_followups ADD COLUMN contact_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE service_followups ADD COLUMN last_contact_at @TS;
ALTER TABLE service_followups ADD COLUMN last_contact_result VARCHAR(20);

CREATE TABLE followup_contacts (
    id              @ID,
    followup_id     @REF NOT NULL REFERENCES service_followups(id) ON DELETE CASCADE,
    result          VARCHAR(20)  NOT NULL CHECK (result IN ('no_answer','later','declined','reopened')),
    note            VARCHAR(300),
    user_id         @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW
);
CREATE INDEX ix_followup_contacts ON followup_contacts (followup_id, created_at);
