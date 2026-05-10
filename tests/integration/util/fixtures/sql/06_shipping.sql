-- 06_shipping.sql — UC3: carrier scan events providing Kafka-to-PostgreSQL cross-dataset signal.
-- order_id values overlap with imazon.orders.events and imazon.shipping.updates Kafka topics.

CREATE TABLE shipping.carrier_status (
    event_id        SERIAL PRIMARY KEY,
    tracking_number VARCHAR(30)  NOT NULL,
    order_id        VARCHAR(20)  NOT NULL,
    carrier         VARCHAR(20)  NOT NULL CHECK (carrier IN ('UPS','FedEx','DHL')),
    status          VARCHAR(30)  NOT NULL CHECK (status IN ('label_created','picked_up','in_transit','out_for_delivery','delivered','delayed','exception')),
    location        VARCHAR(100),
    event_time      TIMESTAMP    NOT NULL
);

INSERT INTO shipping.carrier_status (tracking_number, order_id, carrier, status, location, event_time) VALUES
('1Z999AA10001', 'ORD-2024-00001', 'UPS',   'picked_up',        'New York, NY',          '2024-11-01 18:00:00'),
('1Z999AA10001', 'ORD-2024-00001', 'UPS',   'in_transit',       'Philadelphia, PA',      '2024-11-02 06:00:00'),
('1Z999AA10001', 'ORD-2024-00001', 'UPS',   'delivered',        'Boston, MA',            '2024-11-04 14:30:00'),
('FX100200300',  'ORD-2024-00002', 'FedEx', 'picked_up',        'Los Angeles, CA',       '2024-11-02 20:00:00'),
('FX100200300',  'ORD-2024-00002', 'FedEx', 'in_transit',       'Phoenix, AZ',           '2024-11-03 08:00:00'),
('FX100200300',  'ORD-2024-00002', 'FedEx', 'delivered',        'Denver, CO',            '2024-11-05 11:00:00'),
('DH5001001',    'ORD-2024-00003', 'DHL',   'picked_up',        'Chicago, IL',           '2024-11-04 07:00:00'),
('DH5001001',    'ORD-2024-00003', 'DHL',   'in_transit',       'Detroit, MI',           '2024-11-05 12:00:00'),
('DH5001001',    'ORD-2024-00003', 'DHL',   'delivered',        'Cleveland, OH',         '2024-11-06 16:20:00'),
('1Z999AA10002', 'ORD-2024-00004', 'UPS',   'picked_up',        'Houston, TX',           '2024-11-05 06:00:00'),
('1Z999AA10002', 'ORD-2024-00004', 'UPS',   'in_transit',       'Dallas, TX',            '2024-11-06 10:00:00'),
('1Z999AA10002', 'ORD-2024-00004', 'UPS',   'delivered',        'Austin, TX',            '2024-11-07 13:45:00'),
('FX100200301',  'ORD-2024-00005', 'FedEx', 'picked_up',        'Seattle, WA',           '2024-11-06 07:00:00'),
('FX100200301',  'ORD-2024-00005', 'FedEx', 'in_transit',       'Portland, OR',          '2024-11-07 09:00:00'),
('FX100200301',  'ORD-2024-00005', 'FedEx', 'delivered',        'San Francisco, CA',     '2024-11-08 10:30:00'),
('FX100200304',  'ORD-2024-00015', 'FedEx', 'in_transit',       'Oakland, CA',           '2024-11-17 22:00:00'),
('FX100200304',  'ORD-2024-00015', 'FedEx', 'delayed',          'Customs, CA',           '2024-11-18 10:00:00'),
('FX100200304',  'ORD-2024-00015', 'FedEx', 'out_for_delivery', 'San Jose, CA',          '2024-11-19 08:00:00'),
('FX100200304',  'ORD-2024-00015', 'FedEx', 'delivered',        'San Jose, CA',          '2024-11-19 14:15:00'),
('1Z999AA10010', 'ORD-2024-00035', 'UPS',   'in_transit',       'Louisville, KY',        '2024-12-06 18:00:00'),
('1Z999AA10010', 'ORD-2024-00035', 'UPS',   'delayed',          'Louisville, KY',        '2024-12-07 06:00:00'),
('1Z999AA10010', 'ORD-2024-00035', 'UPS',   'out_for_delivery', 'Cincinnati, OH',        '2024-12-08 07:30:00'),
('1Z999AA10010', 'ORD-2024-00035', 'UPS',   'delivered',        'Columbus, OH',          '2024-12-08 15:00:00'),
('FX100200308',  'ORD-2024-00039', 'FedEx', 'label_created',    'Shipping label created', '2024-12-09 14:00:00'),
('DH5001009',    'ORD-2024-00045', 'DHL',   'label_created',    'Awaiting pickup',        '2024-12-15 12:00:00'),
('1Z999AA10011', 'ORD-2024-00050', 'UPS',   'out_for_delivery', 'Memphis, TN',           '2024-12-21 07:30:00'),
('1Z999AA10011', 'ORD-2024-00050', 'UPS',   'delivered',        'Nashville, TN',         '2024-12-21 15:00:00'),
('1Z999AA10012', 'ORD-2024-00063', 'UPS',   'delayed',          'Atlanta, GA',           '2025-01-03 10:00:00'),
('1Z999AA10012', 'ORD-2024-00063', 'UPS',   'in_transit',       'Charlotte, NC',         '2025-01-04 14:00:00'),
('1Z999AA10012', 'ORD-2024-00063', 'UPS',   'delivered',        'Raleigh, NC',           '2025-01-06 11:00:00');

COMMENT ON TABLE shipping.carrier_status IS 'Individual scan events emitted by logistics carriers as parcels move through the delivery network. order_id joins to imazon.orders.events.order_id and imazon.shipping.updates.order_id in the Kafka fixture, providing the primary Kafka-to-PostgreSQL cross-dataset signal for UC3.';

COMMENT ON COLUMN shipping.carrier_status.event_id IS 'Synthetic primary key — auto-incrementing sequence within this table.';
COMMENT ON COLUMN shipping.carrier_status.tracking_number IS 'Carrier-assigned parcel tracking number. Matches the tracking field in the imazon.shipping.updates Kafka topic.';
COMMENT ON COLUMN shipping.carrier_status.order_id IS 'Imazon order identifier. Matches order_id in imazon.orders.events and imazon.shipping.updates Kafka events, enabling cross-platform join inference.';
COMMENT ON COLUMN shipping.carrier_status.carrier IS 'Logistics carrier name. Constrained to the three carriers used in the EU fulfillment region: UPS, FedEx, DHL.';
COMMENT ON COLUMN shipping.carrier_status.status IS 'Delivery lifecycle stage at the time of this scan event. Follows the carrier''s standard progression: label_created → picked_up → in_transit → out_for_delivery → delivered; delayed and exception break the normal sequence.';
COMMENT ON COLUMN shipping.carrier_status.location IS 'Human-readable city and state/country where the scan occurred. NULL if the carrier scan system did not report a location.';
COMMENT ON COLUMN shipping.carrier_status.event_time IS 'UTC timestamp of the carrier scan event. Multiple rows per order_id track the full parcel journey.';
