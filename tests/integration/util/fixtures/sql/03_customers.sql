-- 03_customers.sql — UC5: EU customer profiles with PII (GDPR-relevant).

CREATE TABLE customers.eu_profiles (
    customer_id   SERIAL PRIMARY KEY,
    email         VARCHAR(200) NOT NULL,
    full_name     VARCHAR(200) NOT NULL,
    date_of_birth DATE,
    country_code  VARCHAR(2) NOT NULL CHECK (country_code IN ('DE','FR','ES','IT','NL')),
    city          VARCHAR(100),
    phone         VARCHAR(30),
    gdpr_consent  BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW()
);

INSERT INTO customers.eu_profiles (email, full_name, date_of_birth, country_code, city, phone, gdpr_consent) VALUES
('hans.mueller@example.de',      'Hans Müller',          '1985-03-12', 'DE', 'Berlin',    '+49-30-1234567',  TRUE),
('marie.dupont@example.fr',      'Marie Dupont',         '1990-07-25', 'FR', 'Paris',     '+33-1-23456789',  TRUE),
('carlos.garcia@example.es',     'Carlos García',        '1988-11-03', 'ES', 'Madrid',    '+34-91-1234567',  TRUE),
('giulia.rossi@example.it',      'Giulia Rossi',         '1992-01-18', 'IT', 'Roma',      '+39-06-1234567',  TRUE),
('jan.devries@example.nl',       'Jan de Vries',         '1987-06-30', 'NL', 'Amsterdam', '+31-20-1234567',  TRUE),
('petra.schmidt@example.de',     'Petra Schmidt',        '1983-09-22', 'DE', 'Berlin',    '+49-30-7654321',  TRUE),
('sophie.martin@example.fr',     'Sophie Martin',        '1995-04-14', 'FR', 'Paris',     '+33-1-98765432',  TRUE),
('elena.fernandez@example.es',   'Elena Fernández',      '1991-08-08', 'ES', 'Madrid',    '+34-91-7654321',  TRUE),
('marco.bianchi@example.it',     'Marco Bianchi',        '1986-12-01', 'IT', 'Milano',    '+39-02-1234567',  TRUE),
('anna.jansen@example.nl',       'Anna Jansen',          '1993-05-20', 'NL', 'Amsterdam', '+31-20-7654321',  TRUE),
('thomas.weber@example.de',      'Thomas Weber',         '1989-02-28', 'DE', 'Berlin',    '+49-30-1112233',  TRUE),
('camille.leroy@example.fr',     'Camille Leroy',        '1994-10-15', 'FR', 'Paris',     '+33-1-11223344',  TRUE),
('pablo.martinez@example.es',    'Pablo Martínez',       '1987-07-07', 'ES', 'Madrid',    '+34-91-1112233',  FALSE),
('alessandra.conti@example.it',  'Alessandra Conti',     '1990-03-30', 'IT', 'Roma',      '+39-06-7654321',  TRUE),
('pieter.bakker@example.nl',     'Pieter Bakker',        '1984-11-11', 'NL', 'Amsterdam', '+31-20-1112233',  TRUE),
('klaus.hoffmann@example.de',    'Klaus Hoffmann',       '1982-08-19', 'DE', 'Berlin',    '+49-30-4445566',  TRUE),
('isabelle.petit@example.fr',    'Isabelle Petit',       '1996-01-05', 'FR', 'Paris',     '+33-1-44556677',  TRUE),
('lucia.sanchez@example.es',     'Lucía Sánchez',        '1993-06-22', 'ES', 'Madrid',    '+34-91-4445566',  TRUE),
('lorenzo.ferrara@example.it',   'Lorenzo Ferrara',      '1988-04-17', 'IT', 'Roma',      '+39-06-4445566',  TRUE),
('eva.vanderberg@example.nl',    'Eva van der Berg',     '1991-09-09', 'NL', 'Amsterdam', '+31-20-4445566',  TRUE);
