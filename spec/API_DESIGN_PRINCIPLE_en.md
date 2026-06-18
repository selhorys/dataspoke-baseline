# API Design Principle

This document defines the standard principles for RESTful API design. Follow these guidelines as
the default, but exceptions are acceptable when justified by specific technical requirements.

---

## 1. Standard Request & Response Formats

### 1. Basic Guide

All requests must follow these standards so the server can accurately identify and process
resources.

- **Declare Content-Type:** Include the `Content-Type: application/json` header on all write
  requests (POST, PUT, PATCH).
- **UTF-8 Encoding:** Always use UTF-8 encoding to prevent data corruption.
- **Field Naming Convention:** Request body field names must be consistent, just like URIs
  (e.g., choose either `snake_case` or `camelCase` as the team standard).
- **Date/Time Format:** Use the ISO 8601 standard (`YYYY-MM-DDTHH:mm:ssZ`) to avoid timezone
  confusion.

### 2. Response Format Guide

Responses must be structured so clients can immediately determine success and easily parse the data.

- **Use HTTP Status Codes:** Convey the response status via appropriate HTTP status codes
  (200 OK, 201 Created, 400 Bad Request, 404 Not Found, etc.) rather than embedding it only
  in the JSON body.
- **Standardize Error Responses:** Return a consistent error object when an error occurs.
  - e.g., `{"error_code": "INVALID_PARAMETER", "message": "The 'count' field must be an integer."}`

### 3. Separation of Content and Metadata

Within the response body, clearly separate the **data the client actually requested (Content)**
from the **information used for system processing (Metadata)**. This allows clients to handle
the core data model and supplementary information independently.

- **Content (requested data):** The resource data that is central to the business logic.
- **Metadata:** Includes pagination info, response time, API version, trace ID, etc.

#### Best Practice Example

When requesting a list of fruits, this example separates the `fruits` resource from all other
control information.

```json
{
    // Content: the requested resource data (list response for a collection path)
    "fruits": [
         {"name": "apple", "count": 5},
         {"name": "banana", "count": 3}
    ],

    // Metadata: control and supplementary information
    "offset": 5,
    "limit": 2,
    "total_count": 30,
    "resp_time": "2026-01-01T13:14:15.123+09:00"
}
```

---

## 2. Standard URI Structure

### 1. Resources must always use noun forms

URIs should focus on "What". The verb — "How" — is handled by the HTTP method.

- **Bad (Action-based):**
  - `POST /createNewUser`
  - `GET /get_order_list`
  - `DELETE /delete-post/42`

- **Good (Resource-based):**
  - `POST /user` (create a user)
  - `GET /order` (retrieve order list)
  - `DELETE /post/42` (delete post #42)

---

### 2. Follow the Classifier / Identifier structure

Use a hierarchical structure to clearly distinguish a resource's parent scope from its identifier.

- **Structure:** `/{classifier}/{identifier}/{sub-classifier}/{identifier}`
- **Example (e-commerce review system):**
  - `/product` (full product list)
  - `/product/p001` (the specific product with ID p001)
  - `/product/p001/review` (all reviews for product p001)
  - `/product/p001/review/rev99` (the specific review with ID rev99 under product p001)

---

### 3. Collection vs Single Resource

A resource path without an identifier returns a collection (list); a path with an identifier
returns a single object.

- **Example (payment history):**
  - `GET /payment`
  - **Response (List):**
    ```json
    [
      { "pay_id": "T01", "amount": 5000 },
      { "pay_id": "T02", "amount": 12000 }
    ]
    ```

- **Contrast:** `GET /payment/T01` (returns a single object `{ "pay_id": "T01", ... }`)

---

### 4. Use Meta-Classifiers (attr, method, event)

The purpose of this principle is to clearly separate plain data fields (Field), business logic
(Action), and state changes (History), making the nature of each API self-evident.

This approach is not mandatory, but is useful for organizing when many features or
characteristics come after a resource identifier.

#### 1. attr (Attributes): Separating state and configuration

Use this when you want to read or update only a specific group of attributes — such as
**metadata, configuration values, or permission states** — rather than fetching the entire
resource object. This reduces the overhead of transferring heavy objects in full.

- **Example (user settings):**
  - `GET /member/m_123/attr` : Retrieve only 'attribute' fields such as profile photo, marketing
    consent, and language preference.
  - `PATCH /member/m_123/attr` : Update only a specific attribute (e.g., enabling dark mode).

- **Example (device state):**
  - `GET /iot-device/dev_88/attr/battery` : Check a specific attribute group — the device's
    current battery level.

#### 2. method (Functional Actions): Business logic beyond simple CRUD

REST fundamentally deals with resource state, but real-world services have complex business
processes — such as **approval, recovery, or dispatch** — that are hard to express as simple
field updates. Placing these after `method` makes the intended action explicit.

- **Example (payment and order process):**
  - `POST /payment/pay_abc/method/approve` : Execute payment approval logic.
  - `POST /order/ord_555/method/calculate-tax` : Invoke tax calculation logic (returns the
    result only).

- **Example (account security):**
  - `POST /account/u_789/method/lock` : Force-lock an account due to a security threat.
  - `POST /account/u_789/method/unlock` : Unlock an account after identity verification.

#### 3. event (Lifecycle & Audit Logs): State changes over time

Resources change over time. Use `event` to track the **history of occurrences** on a specific
resource.

- **Example (delivery tracking):**
  - `GET /delivery/deliv_99/event` : Retrieve the full timeline log:
    [Picked up -> Hub arrived -> Out for delivery -> Delivered].

- **Example (document change history):**
  - `GET /document/doc_001/event` : Audit log of who modified or accessed this document and when.

- **Example (error log):**
  - `GET /server/srv_10/event` : History of system events and errors that occurred on the server.

- **Example (posting an event):**
  - `POST /project/proj_42/event/deployment` : Record a deployment occurrence against the
    project timeline.

---

### 5. URL Query Segments are for filtering, sorting, and pagination

Use query parameters to change how data is presented while keeping the resource's canonical
path intact. These three concerns share one binding convention across **every** list
endpoint, so that clients can page and sort any collection the same way.

- **Filtering:**
  - `/ticket?status=open&priority=high` (return only open, high-priority tickets)

- **Sorting — the standard `sort` parameter:**
  - Format: `sort=<field>_asc` or `sort=<field>_desc` — a field name with a direction
    suffix. `/product?sort=price_asc` sorts by price ascending.
  - Each endpoint **documents its own allowed sort fields and its default ordering** (the
    ordering applied when `sort` is omitted). The convention here fixes only the parameter
    name and value grammar; the permitted fields stay endpoint-specific.
  - Unless an endpoint documents otherwise, resource-list endpoints accept `sort` by
    `created_at`/`updated_at` and event-list endpoints by `occurred_at`, defaulting to
    newest-first.

- **Pagination — the standard envelope:**
  - Request: `offset` (start index, default `0`) and `limit` (page size, default `20`,
    max `1000`). `/log?offset=20&limit=10` retrieves 10 entries starting from the 21st.
  - Response: every collection response carries `offset`, `limit`, and `total_count` as
    metadata alongside the content key (see §1.3 *Separation of Content and Metadata*).
    `total_count` is the unpaged size of the filtered collection, letting clients render
    page counts without a second request.

Every list endpoint references this section rather than redefining its own paging or sort
grammar; deviations require explicit justification in the endpoint's feature spec.
