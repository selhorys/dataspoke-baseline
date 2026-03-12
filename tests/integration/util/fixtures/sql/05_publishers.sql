-- 05_publishers.sql — UC1: upstream publisher feed with raw JSONB payloads.

CREATE TABLE publishers.feed_raw (
    feed_id       SERIAL PRIMARY KEY,
    publisher     VARCHAR(200) NOT NULL,
    isbn          VARCHAR(17),
    raw_payload   JSONB NOT NULL,
    received_at   TIMESTAMP DEFAULT NOW(),
    processed     BOOLEAN DEFAULT FALSE
);

INSERT INTO publishers.feed_raw (publisher, isbn, raw_payload, received_at, processed) VALUES
('Imazon Press',      '9780000000001', '{"title":"The Silent Cipher","author":"Elena Vasquez","format":"Hardcover","list_price":24.99,"status":"active"}',       '2024-12-01 08:00:00', TRUE),
('Imazon Press',      '9780000000002', '{"title":"Quantum Dreams","author":"Marcus Chen","format":"Hardcover","list_price":26.99,"status":"active"}',             '2024-12-01 08:05:00', TRUE),
('Nordic House',      '9780000000003', '{"title":"Love in the Algorithm","author":"Sofia Bergström","format":"Paperback","list_price":13.99,"status":"active"}',  '2024-12-01 08:10:00', TRUE),
('Imazon Press',      '9780000000004', '{"title":"The Dragon Codex","author":"James Okafor","format":"Hardcover","list_price":28.99,"status":"active"}',          '2024-12-01 08:15:00', TRUE),
('Academic Lane',     '9780000000005', '{"title":"Echoes of Empire","author":"Dr. Lucia Ferri","format":"Hardcover","list_price":32.99,"status":"active"}',        '2024-12-01 08:20:00', TRUE),
('Summit Books',      '9780000000006', '{"title":"The Innovator Mindset","author":"Raj Patel","format":"Hardcover","list_price":29.99,"status":"active"}',         '2024-12-01 08:25:00', TRUE),
('Imazon Press',      '9780000000007', '{"title":"My Life in Code","author":"Ada Kowalski","format":"Paperback","list_price":17.99,"status":"active"}',            '2024-12-01 08:30:00', TRUE),
('Little Readers Co', '9780000000008', '{"title":"Tiny Explorers","author":"Maria Santos","format":"Hardcover","list_price":9.99,"status":"active"}',              '2024-12-01 08:35:00', TRUE),
('Imazon Press',      '9780000000009', '{"title":"Stars and Quarks","author":"Prof. Yuki Tanaka","format":"Paperback","list_price":15.99,"status":"active"}',      '2024-12-01 08:40:00', TRUE),
('Gallimard US',      '9780000000010', '{"title":"The Midnight Garden","author":"Claire Dubois","format":"Paperback","list_price":12.99,"status":"active"}',       '2024-12-01 08:45:00', TRUE),
('Summit Books',      '9780000000011', '{"title":"Rise and Grind","author":"Tom Harwick","format":"Paperback","list_price":14.99,"status":"active"}',              '2024-12-01 08:50:00', TRUE),
('Imazon Press',      '9780000000012', '{"title":"The Frost Blade","author":"James Okafor","format":"Hardcover","list_price":28.99,"status":"active"}',            '2024-12-01 08:55:00', TRUE),
('Nordic House',      '9780000000013', '{"title":"Ocean Whispers","author":"Sofia Bergström","format":"Paperback","list_price":14.99,"status":"active"}',          '2024-12-01 09:00:00', FALSE),
('Imazon Press',      '9780000000014', '{"title":"Cyber Siege","author":"Elena Vasquez","format":"Hardcover","list_price":25.99,"status":"active"}',               '2024-12-01 09:05:00', FALSE),
('Imazon Press',      '9780000000015', '{"title":"The Pocket Universe","author":"Marcus Chen","format":"Hardcover","list_price":27.99,"status":"active"}',         '2024-12-01 09:10:00', FALSE),
('Little Readers Co', '9780000000016', '{"title":"Little Chef","author":"Maria Santos","format":"Hardcover","list_price":10.99,"status":"active"}',                '2024-12-01 09:15:00', FALSE),
('Academic Lane',     '9780000000017', '{"title":"The Gene Revolution","author":"Prof. Yuki Tanaka","format":"Hardcover","list_price":28.99,"status":"active"}',   '2024-12-01 09:20:00', FALSE),
('Summit Books',      '9780000000018', '{"title":"Boardroom Battles","author":"Raj Patel","format":"Hardcover","list_price":31.99,"status":"active"}',             '2024-12-01 09:25:00', FALSE),
('Gallimard US',      '9780000000019', '{"title":"The Last Lighthouse","author":"Claire Dubois","format":"Paperback","list_price":13.99,"status":"active"}',       '2024-12-01 09:30:00', FALSE),
('Little Readers Co', '9780000000020', '{"title":"Pirate Panda","author":"Tom Harwick","format":"Paperback","list_price":11.99,"status":"active"}',                '2024-12-01 09:35:00', FALSE);
