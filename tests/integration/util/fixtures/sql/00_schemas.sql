-- 00_schemas.sql — Create all custom schemas for dummy data.
-- Executed after CASCADE drop, so these are always fresh.

CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS orders;
CREATE SCHEMA IF NOT EXISTS customers;
CREATE SCHEMA IF NOT EXISTS reviews;
CREATE SCHEMA IF NOT EXISTS shipping;
