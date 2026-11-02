ALTER TABLE logical_listings ADD COLUMN title_snapshot TEXT;
UPDATE logical_listings SET title_snapshot=(SELECT json_extract(p.baseline,'$.name') FROM logical_products p WHERE p.id=logical_listings.product_id);
UPDATE logical_products SET baseline=json_set(baseline,'$.name',code) WHERE length(code)=3 AND json_extract(baseline,'$.name') IS NOT NULL;
