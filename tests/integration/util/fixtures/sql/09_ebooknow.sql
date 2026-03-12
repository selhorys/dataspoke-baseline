-- 09_ebooknow.sql — UC4: eBookNow acquired company data (products, content, storefront).
-- ~30% NULL isbn in digital_catalog, free-text creator field, ~70% title overlap with catalog.title_master.

-- products.digital_catalog (20 rows)
CREATE TABLE products.digital_catalog (
    product_id    SERIAL PRIMARY KEY,
    title         VARCHAR(300) NOT NULL,
    creator       VARCHAR(200),         -- free-text, not normalized
    isbn          VARCHAR(17),           -- ~30% NULL
    format        VARCHAR(20),
    price         NUMERIC(10,2),
    drm_protected BOOLEAN DEFAULT TRUE,
    file_size_mb  NUMERIC(8,2),
    language      VARCHAR(5) DEFAULT 'en',
    status        VARCHAR(20) DEFAULT 'active',
    imported_at   TIMESTAMP DEFAULT NOW()
);

INSERT INTO products.digital_catalog (title, creator, isbn, format, price, drm_protected, file_size_mb, status) VALUES
('The Silent Cipher',        'Vasquez, Elena',       '9780000000001', 'EPUB',  9.99,  TRUE,  2.4,  'active'),
('Quantum Dreams',           'Chen, Marcus',         '9780000000002', 'EPUB',  11.99, TRUE,  3.1,  'active'),
('Love in the Algorithm',    'Bergström, S.',        '9780000000003', 'EPUB',  8.99,  TRUE,  1.8,  'active'),
('The Dragon Codex',         'Okafor, J.',           '9780000000004', 'EPUB',  12.99, TRUE,  4.2,  'active'),
('Echoes of Empire',         'Ferri, Lucia Dr.',     '9780000000005', 'PDF',   14.99, FALSE, 8.5,  'active'),
('The Innovator Mindset',    'Raj Patel',            '9780000000006', 'EPUB',  10.99, TRUE,  2.0,  'active'),
('My Life in Code',          'Kowalski A',           NULL,            'EPUB',  9.99,  TRUE,  2.2,  'active'),   -- NULL isbn
('Tiny Explorers',           'Santos, Maria',        '9780000000008', 'PDF',   6.99,  FALSE, 15.3, 'active'),
('Stars and Quarks',         'Prof Yuki Tanaka',     '9780000000009', 'EPUB',  7.99,  TRUE,  1.9,  'active'),
('The Midnight Garden',      'Dubois Claire',        '9780000000010', 'EPUB',  8.99,  TRUE,  1.7,  'active'),
('Rise and Grind',           'Harwick, Tom',         NULL,            'MOBI',  9.99,  TRUE,  1.5,  'active'),   -- NULL isbn
('The Frost Blade',          'James Okafor',         '9780000000012', 'EPUB',  12.99, TRUE,  4.5,  'active'),
('Ocean Whispers',           'Bergström Sofia',      '9780000000013', 'EPUB',  8.99,  TRUE,  1.9,  'active'),
('Cyber Siege',              'E. Vasquez',           NULL,            'EPUB',  10.99, TRUE,  2.6,  'active'),   -- NULL isbn
('The Pocket Universe',      'Chen M.',              '9780000000015', 'EPUB',  11.99, TRUE,  3.0,  'active'),
('Little Chef',              'Santos Maria',         NULL,            'PDF',   7.99,  FALSE, 12.8, 'active'),   -- NULL isbn
('eBookNow Exclusive: Cooking Secrets', 'Chef Amir', NULL,           'EPUB',  5.99,  TRUE,  1.2,  'active'),   -- NULL isbn, no Imazon match
('eBookNow Exclusive: Yoga Guide',     'Priya Sen', NULL,            'PDF',   4.99,  FALSE, 3.4,  'active'),   -- NULL isbn, no Imazon match
('The Gene Revolution',      'Tanaka, Yuki Prof.',   '9780000000017', 'EPUB',  13.99, TRUE,  2.8,  'active'),
('Boardroom Battles',        'Patel Raj',            '9780000000018', 'EPUB',  14.99, TRUE,  2.3,  'active');

-- content.ebook_assets (20 rows)
CREATE TABLE content.ebook_assets (
    asset_id      SERIAL PRIMARY KEY,
    product_id    INTEGER NOT NULL,
    asset_type    VARCHAR(10) NOT NULL CHECK (asset_type IN ('EPUB','PDF','MOBI','COVER','SAMPLE')),
    file_path     VARCHAR(500) NOT NULL,
    file_size_mb  NUMERIC(8,2),
    checksum_sha256 VARCHAR(64),
    uploaded_at   TIMESTAMP DEFAULT NOW()
);

INSERT INTO content.ebook_assets (product_id, asset_type, file_path, file_size_mb, checksum_sha256) VALUES
(1,  'EPUB',   '/assets/epub/silent-cipher.epub',        2.4,  'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'),
(1,  'COVER',  '/assets/covers/silent-cipher.jpg',       0.3,  'b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3'),
(2,  'EPUB',   '/assets/epub/quantum-dreams.epub',       3.1,  'c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4'),
(3,  'EPUB',   '/assets/epub/love-algorithm.epub',       1.8,  'd4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5'),
(4,  'EPUB',   '/assets/epub/dragon-codex.epub',         4.2,  'e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6'),
(4,  'SAMPLE', '/assets/samples/dragon-codex-ch1.epub',  0.5,  'f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1'),
(5,  'PDF',    '/assets/pdf/echoes-empire.pdf',          8.5,  'a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3'),
(6,  'EPUB',   '/assets/epub/innovator-mindset.epub',    2.0,  'b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4'),
(8,  'PDF',    '/assets/pdf/tiny-explorers.pdf',        15.3,  'c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5'),
(8,  'COVER',  '/assets/covers/tiny-explorers.jpg',      0.5,  'd5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6'),
(9,  'EPUB',   '/assets/epub/stars-quarks.epub',         1.9,  'e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7'),
(10, 'EPUB',   '/assets/epub/midnight-garden.epub',      1.7,  'f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2b3c4d5e6f7a2'),
(11, 'MOBI',   '/assets/mobi/rise-and-grind.mobi',      1.5,  'a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4'),
(12, 'EPUB',   '/assets/epub/frost-blade.epub',          4.5,  'b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5'),
(13, 'EPUB',   '/assets/epub/ocean-whispers.epub',       1.9,  'c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6'),
(15, 'EPUB',   '/assets/epub/pocket-universe.epub',      3.0,  'd6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7'),
(16, 'PDF',    '/assets/pdf/little-chef.pdf',           12.8,  'e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8'),
(17, 'EPUB',   '/assets/epub/cooking-secrets.epub',      1.2,  'f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3'),
(19, 'EPUB',   '/assets/epub/gene-revolution.epub',      2.8,  'a4b5c6d7e8f9a4b5c6d7e8f9a4b5c6d7e8f9a4b5c6d7e8f9a4b5c6d7e8f9a4b5'),
(20, 'EPUB',   '/assets/epub/boardroom-battles.epub',    2.3,  'b5c6d7e8f9a4b5c6d7e8f9a4b5c6d7e8f9a4b5c6d7e8f9a4b5c6d7e8f9a4b5c6');

-- storefront.listing_items (15 rows)
CREATE TABLE storefront.listing_items (
    listing_id    SERIAL PRIMARY KEY,
    product_id    INTEGER NOT NULL,
    display_title VARCHAR(300) NOT NULL,
    display_price NUMERIC(10,2) NOT NULL,
    badge         VARCHAR(50),
    sort_rank     INTEGER,
    is_featured   BOOLEAN DEFAULT FALSE,
    listed_at     TIMESTAMP DEFAULT NOW()
);

INSERT INTO storefront.listing_items (product_id, display_title, display_price, badge, sort_rank, is_featured) VALUES
(1,  'The Silent Cipher — eBook',              9.99,  'Bestseller',     1,  TRUE),
(2,  'Quantum Dreams — eBook',                 11.99, 'New Release',    2,  TRUE),
(4,  'The Dragon Codex — eBook',               12.99, 'Top Rated',      3,  TRUE),
(5,  'Echoes of Empire — Digital Edition',      14.99, NULL,             4,  FALSE),
(6,  'The Innovator Mindset — eBook',          10.99, 'Staff Pick',     5,  FALSE),
(8,  'Tiny Explorers — Digital Picture Book',   6.99,  'Kids Favorite',  6,  TRUE),
(9,  'Stars and Quarks — eBook',                7.99,  NULL,             7,  FALSE),
(10, 'The Midnight Garden — eBook',             8.99,  NULL,             8,  FALSE),
(12, 'The Frost Blade — eBook',                12.99, 'Sequel',          9,  FALSE),
(15, 'The Pocket Universe — eBook',            11.99, 'New Release',    10, TRUE),
(17, 'Cooking Secrets — eBookNow Exclusive',    5.99,  'Exclusive',     11, FALSE),
(18, 'Yoga Guide — eBookNow Exclusive',         4.99,  'Exclusive',     12, FALSE),
(19, 'The Gene Revolution — eBook',            13.99, NULL,             13, FALSE),
(20, 'Boardroom Battles — eBook',              14.99, NULL,             14, FALSE),
(3,  'Love in the Algorithm — eBook',           8.99,  'Romance Pick',  15, FALSE);
