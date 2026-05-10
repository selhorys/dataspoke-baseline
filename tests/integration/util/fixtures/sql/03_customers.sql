-- 03_customers.sql — UC3, UC5: EU customer profiles with GDPR-relevant PII.

CREATE TABLE customers.eu_profiles (
    user_id           VARCHAR(20) PRIMARY KEY,
    email             VARCHAR(200) NOT NULL,
    country           VARCHAR(2)   NOT NULL CHECK (country IN ('DE','FR','ES','IT','NL')),
    tier              VARCHAR(20)  NOT NULL DEFAULT 'free' CHECK (tier IN ('free','prime','prime_plus')),
    consent_marketing BOOLEAN      NOT NULL DEFAULT FALSE,
    birth_year        INTEGER,
    preferences_json  JSONB,
    created_at        TIMESTAMP    NOT NULL DEFAULT NOW()
);

INSERT INTO customers.eu_profiles (user_id, email, country, tier, consent_marketing, birth_year, preferences_json, created_at) VALUES
('user_101', 'elena.vasquez@example.de',    'DE', 'prime',      TRUE,  1985, '{"genres":["FIC-THR","NF-BIO"],"format":"Hardcover"}',  '2023-01-10 08:00:00'),
('user_102', 'marcus.chen@example.fr',      'FR', 'prime_plus', TRUE,  1990, '{"genres":["FIC-SCI","NF-SCI"],"format":"eBook"}',      '2023-02-14 09:30:00'),
('user_103', 'sofia.berg@example.es',       'ES', 'free',       FALSE, 1988, NULL,                                                    '2023-03-05 11:00:00'),
('user_104', 'james.okafor@example.it',     'IT', 'prime',      TRUE,  1992, '{"genres":["FIC-FAN"],"format":"Hardcover"}',           '2023-03-20 14:00:00'),
('user_105', 'lucia.ferri@example.nl',      'NL', 'prime_plus', TRUE,  1987, '{"genres":["NF-HIS","NF-SCI"],"format":"Paperback"}',   '2023-04-01 10:15:00'),
('user_106', 'raj.patel@example.de',        'DE', 'free',       FALSE, 1983, NULL,                                                    '2023-04-15 16:00:00'),
('user_107', 'ada.kowalski@example.fr',     'FR', 'prime',      TRUE,  1995, '{"genres":["NF-BIO","NF-SELF"],"format":"Audiobook"}',  '2023-05-02 08:45:00'),
('user_108', 'maria.santos@example.es',     'ES', 'prime',      TRUE,  1991, '{"genres":["CH-PIC","CH-MG"],"format":"Hardcover"}',    '2023-05-18 13:30:00'),
('user_109', 'yuki.tanaka@example.it',      'IT', 'free',       FALSE, 1986, NULL,                                                    '2023-06-03 10:00:00'),
('user_110', 'claire.dubois@example.nl',    'NL', 'prime_plus', TRUE,  1993, '{"genres":["FIC-LIT","FIC-ROM"],"format":"Paperback"}', '2023-06-20 15:45:00'),
('user_111', 'tom.harwick@example.de',      'DE', 'prime',      TRUE,  1989, '{"genres":["NF-SELF","CH-MG"],"format":"eBook"}',       '2023-07-07 09:00:00'),
('user_112', 'hans.mueller@example.de',     'DE', 'free',       TRUE,  1982, '{"genres":["NF-BUS"],"format":"Hardcover"}',            '2023-07-25 11:30:00'),
('user_113', 'marie.dupont@example.fr',     'FR', 'prime',      TRUE,  1994, '{"genres":["FIC-ROM","FIC-LIT"],"format":"Audiobook"}', '2023-08-10 14:00:00'),
('user_114', 'carlos.garcia@example.es',    'ES', 'free',       FALSE, 1987, NULL,                                                    '2023-08-28 10:30:00'),
('user_115', 'giulia.rossi@example.it',     'IT', 'prime_plus', TRUE,  1990, '{"genres":["FIC-THR","FIC-SCI"],"format":"eBook"}',     '2023-09-14 08:00:00'),
('user_116', 'jan.devries@example.nl',      'NL', 'free',       FALSE, 1985, NULL,                                                    '2023-09-30 16:30:00'),
('user_117', 'petra.schmidt@example.de',    'DE', 'prime',      TRUE,  1996, '{"genres":["NF-SCI","NF-BIO"],"format":"Paperback"}',   '2023-10-15 12:00:00'),
('user_118', 'sophie.martin@example.fr',    'FR', 'free',       FALSE, 1993, NULL,                                                    '2023-11-01 09:30:00'),
('user_119', 'marco.bianchi@example.it',    'IT', 'prime',      TRUE,  1988, '{"genres":["FIC-FAN","FIC-THR"],"format":"Hardcover"}', '2023-11-20 14:45:00'),
('user_120', 'anna.jansen@example.nl',      'NL', 'prime_plus', TRUE,  1991, '{"genres":["FIC-LIT","NF-HIS"],"format":"Paperback"}',  '2023-12-05 10:00:00');

COMMENT ON TABLE customers.eu_profiles IS 'EU customer accounts subject to GDPR. One row per registered user. The user_id column is the shared identifier referenced by reviews.user_ratings, enabling cross-dataset join paths for UC3 ontology generation.';

COMMENT ON COLUMN customers.eu_profiles.user_id IS 'Stable customer identifier (e.g. user_101). Joined to reviews.user_ratings.user_id to link customer profile to their submitted ratings.';
COMMENT ON COLUMN customers.eu_profiles.email IS 'Primary contact email address. GDPR-classified PII — must not appear in log output.';
COMMENT ON COLUMN customers.eu_profiles.country IS 'ISO 3166-1 alpha-2 country code for the customer''s billing address. Constrained to the five supported EU markets.';
COMMENT ON COLUMN customers.eu_profiles.tier IS 'Subscription tier determining shipping speed and discount eligibility: free (standard), prime (2-day), prime_plus (next-day + extras).';
COMMENT ON COLUMN customers.eu_profiles.consent_marketing IS 'TRUE when the customer has opted into marketing communications under GDPR Article 6(1)(a). Checked before any promotional dispatch.';
COMMENT ON COLUMN customers.eu_profiles.birth_year IS 'Year of birth used for age-gating certain content categories. NULL when not provided at registration.';
COMMENT ON COLUMN customers.eu_profiles.preferences_json IS 'User-defined reading preferences as a JSON object with optional keys: genres (array of genre codes), format (preferred book format). NULL when no preferences set.';
COMMENT ON COLUMN customers.eu_profiles.created_at IS 'Account creation timestamp (UTC). Used for cohort analysis and GDPR data-retention scheduling.';
