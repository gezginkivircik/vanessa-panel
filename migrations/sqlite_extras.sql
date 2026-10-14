-- Yalnızca SQLite: PostgreSQL exclusion constraint'inin karşılığı.
CREATE TRIGGER trg_appointments_no_overlap_ins
BEFORE INSERT ON appointments
WHEN NEW.is_active_slot = 1
BEGIN
    SELECT RAISE(ABORT, 'ex_appointments_no_overlap')
    WHERE EXISTS (
        SELECT 1 FROM appointments a
        WHERE a.staff_id = NEW.staff_id AND a.is_active_slot = 1
          AND a.starts_at < NEW.ends_at AND NEW.starts_at < a.ends_at
    );
END;

CREATE TRIGGER trg_appointments_no_overlap_upd
BEFORE UPDATE OF staff_id, starts_at, ends_at, is_active_slot ON appointments
WHEN NEW.is_active_slot = 1
BEGIN
    SELECT RAISE(ABORT, 'ex_appointments_no_overlap')
    WHERE EXISTS (
        SELECT 1 FROM appointments a
        WHERE a.id <> NEW.id AND a.staff_id = NEW.staff_id AND a.is_active_slot = 1
          AND a.starts_at < NEW.ends_at AND NEW.starts_at < a.ends_at
    );
END;
