-- 08_marketing.sql — UC5: EU email campaigns downstream of eu_profiles PII.

CREATE TABLE marketing.eu_email_campaigns (
    campaign_id   SERIAL PRIMARY KEY,
    campaign_name VARCHAR(200) NOT NULL,
    subject_line  VARCHAR(300) NOT NULL,
    customer_ids  INTEGER[] NOT NULL,
    sent_at       TIMESTAMP,
    open_rate     NUMERIC(5,2),
    click_rate    NUMERIC(5,2),
    status        VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft','scheduled','sent','cancelled')),
    created_at    TIMESTAMP DEFAULT NOW()
);

INSERT INTO marketing.eu_email_campaigns (campaign_name, subject_line, customer_ids, sent_at, open_rate, click_rate, status) VALUES
('Winter Sale DE',          'Winterschlussverkauf — bis zu 40% Rabatt!',    ARRAY[1,6,11,16],       '2024-12-01 09:00:00', 32.5, 8.2,  'sent'),
('Nouveautés Décembre FR',  'Découvrez nos nouveautés de décembre',         ARRAY[2,7,12,17],       '2024-12-05 10:00:00', 28.7, 6.5,  'sent'),
('Rebajas de Invierno ES', 'Rebajas de invierno — ¡hasta 40% de descuento!', ARRAY[3,8,13,18],    '2024-12-08 09:30:00', 30.1, 7.8,  'sent'),
('Saldi Invernali IT',     'Saldi invernali — fino al 40% di sconto!',     ARRAY[4,9,14,19],       '2024-12-10 10:30:00', 27.3, 5.9,  'sent'),
('Winteruitverkoop NL',    'Winteruitverkoop — tot 40% korting!',           ARRAY[5,10,15,20],      '2024-12-12 09:00:00', 31.8, 7.1,  'sent'),
('New Year All EU',        'Happy New Year from Imazon!',                   ARRAY[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20], '2025-01-01 00:00:00', 45.2, 12.3, 'sent'),
('Valentine Romance EU',   'Love is in the air — romance picks for you',   ARRAY[2,3,5,7,8,10,12,13,15,17,18,20],  '2025-02-10 09:00:00', 35.6, 9.4, 'sent'),
('Spring Books DE',        'Frühlingslektüre — neue Titel für Sie',         ARRAY[1,6,11,16],       '2025-03-01 09:00:00', NULL, NULL, 'scheduled'),
('Printemps Livres FR',    'Lecture de printemps — nouveaux titres',         ARRAY[2,7,12,17],       '2025-03-01 10:00:00', NULL, NULL, 'scheduled'),
('Primavera Libros ES',    'Lectura de primavera — nuevos títulos',         ARRAY[3,8,13,18],       '2025-03-01 09:30:00', NULL, NULL, 'scheduled'),
('GDPR Opt-out Test',      'Test campaign — should exclude opt-out',        ARRAY[1,2,3,4,5],       NULL,                  NULL, NULL, 'draft'),
('Abandoned Cart DE',      'Sie haben etwas vergessen!',                    ARRAY[1,6],             '2024-11-15 14:00:00', 22.1, 4.5, 'sent'),
('Abandoned Cart FR',      'Vous avez oublié quelque chose!',              ARRAY[2,7],             '2024-11-15 14:00:00', 20.8, 3.9, 'sent'),
('Black Friday All EU',    'Black Friday — our biggest sale ever!',         ARRAY[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20], '2024-11-29 06:00:00', 52.3, 18.7, 'sent'),
('Cancelled Campaign',     'This should never have been sent',              ARRAY[1,2,3],           NULL,                  NULL, NULL, 'cancelled');
