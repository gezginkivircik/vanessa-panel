-- Faz 3: Görseller (hizmet görseli vb.) veritabanında saklanır.
-- Render gibi barındırmalarda dosya sistemi kalıcı değildir; görsel DB'de olursa yedekle birlikte korunur.
CREATE TABLE media (
    id              @ID,
    kind            VARCHAR(30)  NOT NULL,           -- service_image, staff_photo, logo
    content_type    VARCHAR(40)  NOT NULL CHECK (content_type IN ('image/png','image/jpeg','image/webp')),
    data            @BLOB        NOT NULL,
    size_bytes      INTEGER      NOT NULL,
    created_by      @REF REFERENCES users(id) ON DELETE SET NULL,
    created_at      @TS          NOT NULL DEFAULT @NOW
);
ALTER TABLE services ADD COLUMN image_media_id @REF REFERENCES media(id) ON DELETE SET NULL;
