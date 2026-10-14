-- Yalnızca PostgreSQL: veritabanı seviyesinde çift randevu koruması.
-- Aynı uzman için aktif (is_active_slot = 1) iki randevunun zaman aralığı çakışamaz.
-- Eş zamanlı iki transaction aynı saati almaya çalışırsa biri exclusion_violation (23P01) alır.
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE appointments
    ADD CONSTRAINT ex_appointments_no_overlap
    EXCLUDE USING gist (
        staff_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (is_active_slot = 1);
