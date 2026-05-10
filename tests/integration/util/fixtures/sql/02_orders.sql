-- 02_orders.sql — UC2, UC3: daily fulfillment aggregate with SLA anomaly row.

-- Daily fulfillment summary (30 rows, 1 anomalous day for UC2/UC3 SLA detection)
CREATE TABLE orders.daily_fulfillment_summary (
    summary_date          DATE PRIMARY KEY,
    region                VARCHAR(50) NOT NULL,
    total_orders          INTEGER NOT NULL,
    total_revenue_cents   BIGINT NOT NULL,
    avg_fulfillment_hours NUMERIC(6,2),
    notes                 TEXT,
    created_at            TIMESTAMP DEFAULT NOW()
);

INSERT INTO orders.daily_fulfillment_summary (summary_date, region, total_orders, total_revenue_cents, avg_fulfillment_hours, notes) VALUES
('2025-01-01', 'EU',      142, 324580,  18.4, NULL),
('2025-01-02', 'EU',      138, 310250,  17.9, NULL),
('2025-01-03', 'EU',      155, 358020,  18.7, NULL),
('2025-01-04', 'EU',      148, 339010,  18.2, NULL),
('2025-01-05', 'EU',      160, 372060,  19.1, NULL),
('2025-01-06', 'EU',      145, 331040,  18.3, NULL),
('2025-01-07', 'EU',      152, 349030,  18.6, NULL),
('2025-01-08', 'EU',      140, 320090,  17.8, NULL),
('2025-01-09', 'EU',      158, 365070,  18.9, NULL),
('2025-01-10', 'EU',      143, 328020,  18.1, NULL),
('2025-01-11', 'EU',      150, 345050,  18.5, NULL),
('2025-01-12', 'EU',      147, 337080,  18.3, NULL),
('2025-01-13', 'EU',      156, 359040,  18.8, NULL),
('2025-01-14', 'EU',      141, 323060,  17.9, NULL),
('2025-01-15', 'EU',       12,  27588,  72.1, 'Warehouse system outage — orders queued, fulfillment severely delayed.'),
('2025-01-16', 'EU',      153, 352010,  18.6, NULL),
('2025-01-17', 'EU',      149, 341070,  18.4, NULL),
('2025-01-18', 'EU',      161, 374030,  19.2, NULL),
('2025-01-19', 'EU',      144, 330090,  18.2, NULL),
('2025-01-20', 'EU',      157, 362050,  18.7, NULL),
('2025-01-21', 'EU',      146, 335020,  18.3, NULL),
('2025-01-22', 'EU',      151, 347080,  18.5, NULL),
('2025-01-23', 'EU',      139, 318040,  17.7, NULL),
('2025-01-24', 'EU',      154, 355060,  18.7, NULL),
('2025-01-25', 'EU',      148, 340030,  18.4, NULL),
('2025-01-26', 'EU',      159, 368090,  19.0, NULL),
('2025-01-27', 'EU',      142, 326050,  18.2, NULL),
('2025-01-28', 'EU',      155, 357020,  18.6, NULL),
('2025-01-29', 'EU',      147, 338070,  18.3, NULL),
('2025-01-30', 'EU',      150, 344010,  18.5, NULL);

COMMENT ON TABLE orders.daily_fulfillment_summary IS 'One row per calendar day summarising order volume and revenue across the EU fulfillment region. The anomalous row on 2025-01-15 (warehouse outage) is intentional test data for SLA threshold detection in UC2/UC3.';

COMMENT ON COLUMN orders.daily_fulfillment_summary.summary_date IS 'Calendar date (UTC) this row covers. Primary key — one row per day per region.';
COMMENT ON COLUMN orders.daily_fulfillment_summary.region IS 'Fulfillment region code. EU covers all five supported European markets.';
COMMENT ON COLUMN orders.daily_fulfillment_summary.total_orders IS 'Count of distinct customer orders confirmed on this date.';
COMMENT ON COLUMN orders.daily_fulfillment_summary.total_revenue_cents IS 'Sum of order values in euro-cents (integer) to avoid floating-point rounding errors in aggregations.';
COMMENT ON COLUMN orders.daily_fulfillment_summary.avg_fulfillment_hours IS 'Mean time in hours from order confirmation to first carrier scan, averaged across all orders on this date.';
COMMENT ON COLUMN orders.daily_fulfillment_summary.notes IS 'Optional free-text annotation for unusual days (outages, holidays, campaigns). NULL on normal trading days.';
COMMENT ON COLUMN orders.daily_fulfillment_summary.created_at IS 'Timestamp when this summary row was written by the nightly ETL job (UTC).';
