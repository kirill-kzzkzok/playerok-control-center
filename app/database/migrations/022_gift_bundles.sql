ALTER TABLE gift_listings ADD COLUMN bundle TEXT;
ALTER TABLE gift_jobs ADD COLUMN bundle TEXT;
ALTER TABLE gift_jobs ADD COLUMN description_sha256 TEXT;
ALTER TABLE gift_jobs ADD COLUMN reserved_day TEXT;
ALTER TABLE gift_jobs ADD COLUMN reserved_stars INTEGER NOT NULL DEFAULT 0;
CREATE TABLE gift_steps (
 order_id TEXT NOT NULL REFERENCES gift_jobs(order_id),
 position INTEGER NOT NULL CHECK(position>=0),
 code TEXT NOT NULL,
 gift_id TEXT NOT NULL,
 stars INTEGER NOT NULL CHECK(stars>0),
 operation_id TEXT UNIQUE REFERENCES outbox(id),
 reference TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','CHECKING','SPENDING','SUCCEEDED','BLOCKED','UNCERTAIN')),
 recipient_id INTEGER,
 payer_id INTEGER,
 form_id TEXT UNIQUE,
 spent_at TEXT,
 message_id INTEGER,
 receipt TEXT,
 error_code TEXT,
 PRIMARY KEY(order_id,position)
);
UPDATE gift_listings SET bundle=json_array(json_object('code',gift_code,'gift_id',gift_id,'stars',approved_stars));
UPDATE gift_jobs SET bundle=json_array(json_object('code',gift_code,'gift_id',gift_id,'stars',COALESCE(stars,(SELECT approved_stars FROM gift_listings WHERE item_id=gift_jobs.item_id)))),
 description_sha256=(SELECT description_sha256 FROM gift_listings WHERE item_id=gift_jobs.item_id);
INSERT INTO gift_steps(order_id,position,code,gift_id,stars,operation_id,reference,state,recipient_id,payer_id,form_id,spent_at,message_id,receipt,error_code)
 SELECT order_id,0,gift_code,gift_id,COALESCE(stars,(SELECT approved_stars FROM gift_listings WHERE item_id=gift_jobs.item_id)),operation_id,reference,
 CASE WHEN state='INVALID_RECIPIENT' THEN 'BLOCKED' ELSE state END,recipient_id,payer_id,form_id,spent_at,message_id,receipt,error_code FROM gift_jobs;
CREATE INDEX gift_steps_state ON gift_steps(state,order_id);
