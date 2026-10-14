-- Faz 2: Türkçe karakterden bağımsız müşteri araması.
-- search_name = katlanmış "ad soyad" (ör. 'ayse yilmaz'). Servis katmanı günceller; mevcut kayıtlar Python hook ile doldurulur.
ALTER TABLE customers ADD COLUMN search_name VARCHAR(200);
CREATE INDEX ix_customers_search_name ON customers (search_name);
