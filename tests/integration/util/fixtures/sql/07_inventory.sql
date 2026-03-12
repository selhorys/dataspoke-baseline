-- 07_inventory.sql — UC4: Imazon warehouse book stock.

CREATE TABLE inventory.book_stock (
    stock_id        SERIAL PRIMARY KEY,
    isbn            VARCHAR(17) NOT NULL,
    warehouse       VARCHAR(20) NOT NULL CHECK (warehouse IN ('WH-East','WH-West','WH-Central')),
    quantity_on_hand INTEGER NOT NULL DEFAULT 0,
    reorder_point   INTEGER NOT NULL DEFAULT 10,
    last_restock    DATE,
    updated_at      TIMESTAMP DEFAULT NOW()
);

INSERT INTO inventory.book_stock (isbn, warehouse, quantity_on_hand, reorder_point, last_restock) VALUES
('9780000000001', 'WH-East',    45, 15, '2024-12-01'),
('9780000000001', 'WH-West',    32, 15, '2024-11-20'),
('9780000000002', 'WH-East',    28, 10, '2024-12-05'),
('9780000000003', 'WH-West',    55, 20, '2024-11-15'),
('9780000000004', 'WH-East',    18, 10, '2024-12-10'),
('9780000000004', 'WH-Central', 22, 10, '2024-12-08'),
('9780000000005', 'WH-East',    12, 10, '2024-11-25'),
('9780000000006', 'WH-West',    38, 15, '2024-12-12'),
('9780000000007', 'WH-East',    41, 15, '2024-11-30'),
('9780000000008', 'WH-Central', 60, 25, '2024-12-15'),
('9780000000009', 'WH-East',    35, 15, '2024-12-01'),
('9780000000010', 'WH-West',    27, 10, '2024-11-18'),
('9780000000012', 'WH-East',    20, 10, '2024-12-10'),
('9780000000014', 'WH-East',    33, 15, '2024-12-05'),
('9780000000015', 'WH-West',    25, 10, '2024-12-08'),
('9780000000020', 'WH-Central', 48, 20, '2024-12-12'),
('9780000000022', 'WH-East',    15, 10, '2024-12-15'),
('9780000000024', 'WH-East',    30, 15, '2024-12-18'),
('9780000000025', 'WH-West',     8,  10, '2024-11-10'),  -- below reorder point
('9780000000027', 'WH-East',    42, 15, '2024-12-20'),
('9780000000028', 'WH-Central', 55, 25, '2024-12-22'),
('9780000000029', 'WH-East',    19, 10, '2024-12-25'),
('9780000000030', 'WH-East',    50, 20, '2025-01-02'),
('9780000000030', 'WH-West',    35, 20, '2025-01-02'),
('9780000000016', 'WH-Central', 65, 30, '2024-12-28');
