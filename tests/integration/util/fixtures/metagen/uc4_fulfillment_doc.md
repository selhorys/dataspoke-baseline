# Imazon Fulfillment Process Guide

## Overview

Imazon's fulfillment pipeline converts a customer order into a physical delivery.
It spans five operational stages: order placement, payment authorization, fulfillment
center routing, pick-and-pack, and carrier handoff. Two additional post-delivery
stages handle tracking updates and returns processing.

## Stage 1 — Order Placement

When a customer completes checkout, the storefront emits a `placed` event on the
`imazon.orders.events` Kafka topic. Each event carries a unique `event_id` that is
monotonically assigned within the stream, an `order_id` that groups all lifecycle
events for the same purchase, and a `timestamp` in ISO 8601 UTC format recording
the exact moment the customer confirmed the basket.

The `items` array in the `placed` event lists every `edition_id` and `qty` pair
ordered. The `total` field records the basket value in the customer's billing
currency at the time of placement.

An `order_id` is the primary join key between the orders domain and the shipping
domain. It appears unchanged in `shipping.carrier_status.order_id` (PostgreSQL)
and in `imazon.shipping.updates.order_id` (Kafka).

## Stage 2 — Payment Authorization

After placement, the payment service validates the customer's payment method and
reserves funds. This step is synchronous and completes before the `confirmed` event
is emitted. If authorization fails, a `cancelled` event is emitted instead and no
fulfillment is triggered.

## Stage 3 — Fulfillment Center Routing

On receipt of the `confirmed` event, the warehouse management system assigns the
order to a fulfillment center. The `confirmed` event carries a `warehouse` field
that records the fulfillment center code (e.g. `WH-East`, `WH-West`, `WH-Central`).
Routing decisions are based on customer geography, stock availability, and carrier
SLA windows.

The `customers.eu_profiles` table stores one row per EU-registered customer. The
`user_id` column is the stable customer identifier referenced throughout the order
pipeline. The `country` column (ISO 3166-1 alpha-2) determines VAT regime and
regulatory jurisdiction for the order. The `tier` column (`free`, `prime`,
`prime_plus`) controls shipping speed entitlements and discount eligibility that
the routing algorithm respects.

## Stage 4 — Pick and Pack

Warehouse operatives locate the edition in the storage grid using the `edition_id`
from the order items array. Each physical unit is picked and moved to a packing
station. The pack step selects box size, inserts protective material, and generates
a shipping label. Label generation triggers the `shipped` event on the
`imazon.orders.events` stream.

The `shipped` event carries a `carrier` field (e.g. `UPS`, `FedEx`, `DHL`) and a
`tracking` field that stores the carrier-assigned tracking number. This tracking
number matches `tracking_number` in `shipping.carrier_status` and
`imazon.shipping.updates.tracking_number`, allowing cross-domain joins.

## Stage 5 — Carrier Handoff

The parcel is tendered to the carrier at the warehouse dock. From this moment the
carrier owns physical custody. Imazon records the handoff by attaching metadata to
the `shipped` event. The carrier then emits scan events independently, which are
consumed by the `imazon.shipping.updates` Kafka topic.

## Stage 6 — Tracking Updates

Carrier scan events (pickup, in-transit, out-for-delivery, delivered) flow into
the `imazon.shipping.updates` Kafka topic. Each update carries the `order_id`,
`tracking_number`, `status`, `location`, and `event_ts` timestamp. The `status`
field values follow the carrier's lifecycle progression: `in_transit`,
`out_for_delivery`, `delivered`, `failed_attempt`, `return_to_sender`.

The `event_ts` column records the UTC timestamp at which the carrier scanned the
parcel. This is the authoritative timestamp for SLA measurement and customer
notifications — it differs from the `timestamp` on the orders.events stream, which
records the storefront action time rather than the physical handling time.

## Stage 7 — Delivery Confirmation

When the carrier delivers the parcel, a `delivered` event is emitted on the
`imazon.orders.events` stream. The `signed_by` field records the name of the
recipient or proxy who accepted delivery. After a configurable holding period
(default 30 days), the order record transitions to `archived` status.

## Returns Processing

Customers can initiate returns within 30 days of delivery (extended to 60 days for
`prime_plus` tier members as recorded in `customers.eu_profiles.tier`). A return
request generates a return merchandise authorization (RMA) number and triggers
a reverse-logistics pickup. The return parcel travels back through the carrier
network and arrives at the returns processing center.

## Key Fields Reference

| Field | Source | Semantics |
|---|---|---|
| `order_id` | orders.events, shipping.carrier_status, shipping.updates | Shared primary join key across all fulfillment domains |
| `event_type` | orders.events | Lifecycle stage: placed, confirmed, shipped, delivered |
| `event_ts` | shipping.updates | UTC timestamp of carrier scan; used for SLA calculation |
| `status` | shipping.updates, shipping.carrier_status | Carrier scan status |
| `user_id` | customers.eu_profiles | Stable customer identifier, FK to reviews and order history |
| `email` | customers.eu_profiles | Customer contact email (GDPR-classified PII) |
| `tier` | customers.eu_profiles | Subscription tier controlling shipping SLA and discounts |
| `country` | customers.eu_profiles | ISO 3166-1 alpha-2 billing country; determines VAT and GDPR jurisdiction |
| `consent_marketing` | customers.eu_profiles | GDPR Article 6(1)(a) marketing opt-in flag |

## GDPR Compliance Notes

The `customers.eu_profiles` table is subject to GDPR Article 17 (right to erasure)
and Article 20 (data portability). The `email` field is classified as PII and must
never appear in log output or analytics exports without explicit anonymization.
The `consent_marketing` field gates all promotional dispatch. The `birth_year`
field is used only for age-gating and is stored at year granularity to minimize
PII exposure.

## Monitoring and SLA

Fulfillment SLA is measured from the `timestamp` on the `placed` event to the
`event_ts` on the `delivered` scan in `imazon.shipping.updates`. SLA breaches are
reported per `warehouse` (from the `confirmed` event) and per `carrier` (from the
`shipped` event). The `tier` field from `customers.eu_profiles` determines the
applicable SLA window: free (7 days), prime (2 days), prime_plus (next day).
