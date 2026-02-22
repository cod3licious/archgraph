# E-Commerce Platform — Unit Descriptions

---

## api.auth

### api.auth.authenticate_request
Validates the incoming JWT token from the Authorization header. Calls `@core.cache.get` to check the token blacklist, then verifies the signature and expiry. Raises `AuthError` on failure.

### api.auth.get_current_user
Decodes the JWT payload and loads the full user record. Calls `@core.db.fetch_one` with the user id extracted from the token. Returns a `UserContext` object used downstream by all handlers.

### api.auth.issue_token
Issues a signed JWT for an authenticated user. Accepts a user id and scopes list, sets expiry to 24 hours, signs with the application secret. Calls `@core.cache.set` to register the token for blacklist tracking.

### api.auth.revoke_token
Adds the given token's jti claim to the blacklist. Calls `@core.cache.set` with a TTL matching the token's remaining lifetime so the cache entry expires automatically.

### api.auth.require_scope
Decorator factory that wraps a route handler and checks that the current user's token includes the required scope string. Calls `@api.auth.get_current_user` internally.

### api.auth.refresh_token
Accepts a valid (non-expired) refresh token, validates it via `@core.cache.get`, issues a new access token via `@api.auth.issue_token`, and revokes the old one via `@api.auth.revoke_token`.

---

## api.products

### api.products.list_products
HTTP GET handler for `/products`. Parses query parameters (page, page_size, category, sort). Calls `@catalog.search.search_products` and returns a paginated JSON response.

### api.products.get_product
HTTP GET handler for `/products/{id}`. Calls `@catalog.products.get_product_by_id` and `@catalog.pricing.get_effective_price`. Returns combined product detail with current price.

### api.products.create_product
HTTP POST handler for `/products`. Validates the request body against the product schema. Calls `@catalog.products.create_product` and `@catalog.inventory.set_stock_level`. Requires `catalog:write` scope via `@api.auth.require_scope`.

### api.products.update_product
HTTP PATCH handler for `/products/{id}`. Applies partial updates by calling `@catalog.products.update_product`. Requires `catalog:write` scope.

### api.products.delete_product
HTTP DELETE handler for `/products/{id}`. Calls `@catalog.products.delete_product` and `@catalog.inventory.remove_product`. Requires `catalog:admin` scope via `@api.auth.require_scope`.

### api.products.get_stock
HTTP GET handler for `/products/{id}/stock`. Calls `@catalog.inventory.get_stock_level` and returns available quantity and reservation status.

### api.products.update_stock
HTTP PATCH handler for `/products/{id}/stock`. Calls `@catalog.inventory.set_stock_level`. Requires `catalog:write` scope via `@api.auth.require_scope`.

---

## api.orders

### api.orders.get_cart
HTTP GET handler for `/cart`. Retrieves the current user's cart via `@orders.cart.get_cart`. Calls `@api.auth.get_current_user` to identify the user.

### api.orders.add_to_cart
HTTP POST handler for `/cart/items`. Validates the item payload and calls `@orders.cart.add_item`. Calls `@catalog.inventory.reserve_stock` to place a soft reservation.

### api.orders.remove_from_cart
HTTP DELETE handler for `/cart/items/{item_id}`. Calls `@orders.cart.remove_item` and `@catalog.inventory.release_reservation`.

### api.orders.checkout
HTTP POST handler for `/orders`. Calls `@orders.checkout.initiate_checkout` and `@payments.gateway.charge`. Returns the created order id on success.

### api.orders.get_order
HTTP GET handler for `/orders/{id}`. Calls `@orders.order_mgmt.get_order` and checks ownership via `@api.auth.get_current_user`.

### api.orders.list_orders
HTTP GET handler for `/orders`. Calls `@orders.order_mgmt.list_orders_for_user` with the user id from `@api.auth.get_current_user`. Supports pagination.

### api.orders.cancel_order
HTTP POST handler for `/orders/{id}/cancel`. Calls `@orders.order_mgmt.cancel_order` and `@payments.refunds.issue_refund` if a charge was already captured.

---

## api.payments

### api.payments.get_invoice
HTTP GET handler for `/invoices/{id}`. Calls `@payments.invoicing.get_invoice` and checks ownership via `@api.auth.get_current_user`.

### api.payments.list_invoices
HTTP GET handler for `/invoices`. Returns all invoices for the current user. Calls `@payments.invoicing.list_invoices_for_user` and `@api.auth.get_current_user`.

### api.payments.download_invoice_pdf
HTTP GET handler for `/invoices/{id}/pdf`. Calls `@payments.invoicing.render_invoice_pdf` and streams the result as a binary response.

### api.payments.request_refund
HTTP POST handler for `/orders/{id}/refund`. Validates the refund request, then calls `@payments.refunds.issue_refund`. Requires `payments:refund` scope via `@api.auth.require_scope`.

### api.payments.get_payment_status
HTTP GET handler for `/orders/{id}/payment`. Calls `@payments.gateway.get_charge_status` and returns a normalised status string.

---

## catalog.products

### catalog.products.get_product_by_id
Loads a single product record from persistent storage. Calls `@core.db.fetch_one` with the products table. Returns `None` if not found.

### catalog.products.list_products
Returns a slice of products matching optional filter criteria. Calls `@core.db.fetch_many` with constructed WHERE clauses for category, status, and price range.

### catalog.products.create_product
Inserts a new product row. Validates uniqueness of the SKU by calling `@core.db.fetch_one` before insert. Calls `@core.db.execute` and `@core.events.publish` with a `product.created` event.

### catalog.products.update_product
Applies a partial update to an existing product. Calls `@core.db.execute` with an UPDATE statement. Publishes a `product.updated` event via `@core.events.publish`.

### catalog.products.delete_product
Soft-deletes a product by setting `deleted_at`. Calls `@core.db.execute` and publishes `product.deleted` via `@core.events.publish`.

### catalog.products.get_products_by_ids
Bulk-loads a list of products by id list. Calls `@core.db.fetch_many` with an IN clause. Used by search and recommendation paths.

### catalog.products.rebuild_product_index
Re-indexes all active products into the search cache. Iterates via `@core.db.fetch_many` and calls `@core.cache.set` for each product document.

---

## catalog.search

### catalog.search.search_products
Full-text search over the product catalog. Checks `@core.cache.get` for a cached result keyed by query hash. On miss, calls `@catalog.products.list_products` with keyword filters and stores the result via `@core.cache.set`.

### catalog.search.suggest_products
Returns autocomplete suggestions for a partial query string. Calls `@core.cache.get` keyed on the prefix. Falls back to `@catalog.products.list_products` with a prefix filter.

### catalog.search.get_similar_products
Returns products similar to a given product id. Calls `@catalog.products.get_product_by_id` to load the source product, then `@catalog.products.list_products` filtered by matching category and tags.

### catalog.search.trending_products
Returns the current trending product list. Reads a pre-computed list from `@core.cache.get`. The list is refreshed by a background job outside this module.

### catalog.search.full_text_search
Low-level full-text search implementation. Tokenises the query and scores products. Also reads cart affinity data from `@orders.cart.get_active_carts` to boost frequently carted items.

### catalog.search.invalidate_search_cache
Clears all search-related cache keys when product data changes. Calls `@core.cache.delete_pattern`.

---

## catalog.pricing

### catalog.pricing.get_effective_price
Returns the effective sale price for a product, applying any active promotions. Calls `@catalog.pricing.get_promotions` and `@catalog.inventory.get_stock_level` (to apply low-stock surcharge rules).

### catalog.pricing.get_promotions
Loads active promotions from `@core.db.fetch_many`. Filters to those overlapping the current timestamp.

### catalog.pricing.apply_discount_code
Validates and applies a discount code to a price. Looks up the code via `@core.db.fetch_one` and checks usage limits.

### catalog.pricing.calculate_cart_total
Computes the total for a list of cart line items. Calls `@catalog.pricing.get_effective_price` for each item and sums results including tax.

### catalog.pricing.get_tax_rate
Returns the applicable tax rate for a given country code. Calls `@core.db.fetch_one` against the tax_rates table. Results are cached via `@core.cache.get` and `@core.cache.set`.

### catalog.pricing.set_base_price
Updates the base price for a product. Calls `@core.db.execute` and invalidates the pricing cache for that product via `@core.cache.delete_pattern`.

---

## catalog.inventory

### catalog.inventory.get_stock_level
Returns the current on-hand quantity and reservation count for a product. Calls `@core.db.fetch_one` against the inventory table.

### catalog.inventory.set_stock_level
Sets the absolute on-hand stock for a product. Calls `@core.db.execute`. Publishes a `stock.updated` event via `@core.events.publish`.

### catalog.inventory.reserve_stock
Places a soft reservation on a given quantity for a product. Calls `@core.db.execute` to increment the reservation counter. Raises `InsufficientStockError` if not enough available.

### catalog.inventory.release_reservation
Decrements the reservation counter for a product. Called when a cart item is removed or a checkout fails. Calls `@core.db.execute`.

### catalog.inventory.commit_reservation
Converts a soft reservation to a confirmed deduction on checkout. Calls `@core.db.execute` in a transaction. Publishes `stock.committed` via `@core.events.publish`.

### catalog.inventory.remove_product
Removes the inventory record for a deleted product. Calls `@core.db.execute` to delete the inventory row.

### catalog.inventory.low_stock_products
Returns products whose on-hand quantity is below their reorder threshold. Calls `@core.db.fetch_many` with a WHERE clause comparing quantity to threshold.

---

## orders.cart

### orders.cart.get_cart
Loads the active cart for a user. Calls `@core.db.fetch_one` for the cart header and `@core.db.fetch_many` for line items. Returns an assembled `Cart` object.

### orders.cart.get_active_carts
Returns all carts that have been modified in the last 24 hours. Calls `@core.db.fetch_many` filtered by `updated_at`. Used for analytics and affinity scoring.

### orders.cart.add_item
Adds a product to the user's cart or increments quantity if already present. Calls `@core.db.execute` for upsert logic. Calls `@catalog.pricing.get_effective_price` to snapshot the price at add time.

### orders.cart.remove_item
Removes a line item from the cart. Calls `@core.db.execute` to delete the cart_item row.

### orders.cart.update_item_quantity
Updates the quantity of a cart line item. Calls `@core.db.execute` and re-snapshots the price via `@catalog.pricing.get_effective_price`.

### orders.cart.apply_discount_code
Applies a discount code to the cart. Calls `@catalog.pricing.apply_discount_code` to validate, then stores the code on the cart header via `@core.db.execute`.

### orders.cart.clear_cart
Deletes all line items and resets the cart header. Calls `@core.db.execute`. Called after a successful checkout.

### orders.cart.get_cart_total
Calculates and returns the cart total. Calls `@catalog.pricing.calculate_cart_total` with the cart's line items.

---

## orders.checkout

### orders.checkout.initiate_checkout
Orchestrates the checkout flow. Calls `@orders.cart.get_cart`, `@catalog.inventory.commit_reservation`, `@catalog.pricing.calculate_cart_total`, and `@orders.order_repo.create_order`. Returns a pending order id.

### orders.checkout.validate_checkout
Pre-flight validation before payment. Checks that all cart items are still in stock via `@catalog.inventory.get_stock_level` and that prices haven't changed significantly via `@catalog.pricing.get_effective_price`.

### orders.checkout.apply_checkout_discount
Applies a last-minute discount to the checkout session. Calls `@orders.cart.apply_discount_code` and recalculates the total.

### orders.checkout.confirm_checkout
Finalises the order after payment capture. Calls `@orders.order_repo.update_order_status` to set status to `confirmed`, then `@orders.cart.clear_cart` and `@core.events.publish` with `order.confirmed`.

### orders.checkout.fail_checkout
Handles a failed payment during checkout. Calls `@catalog.inventory.release_reservation` to return reserved stock, then `@orders.order_repo.update_order_status` to set status to `failed`.

### orders.checkout.get_checkout_summary
Returns a summary of the pending checkout for display. Calls `@orders.order_repo.find_by_id` and `@catalog.pricing.calculate_cart_total`.

---

## orders.order_mgmt

### orders.order_mgmt.get_order
Loads a full order with line items. Calls `@orders.order_repo.find_by_id` and enriches with product details from `@catalog.products.get_products_by_ids`.

### orders.order_mgmt.list_orders_for_user
Returns paginated orders for a user. Calls `@orders.order_repo.find_by_user` with limit/offset. Enriches each order with a status label.

### orders.order_mgmt.cancel_order
Cancels an order if it is still in a cancellable state. Calls `@orders.order_repo.find_by_id` to check status, then `@orders.order_repo.update_order_status` and `@catalog.inventory.release_reservation`.

### orders.order_mgmt.update_order_address
Updates the shipping address on an unshipped order. Calls `@orders.order_repo.update_order_status` (reuses the general update path) and `@core.events.publish` with `order.address_updated`.

### orders.order_mgmt.mark_order_shipped
Marks the order as shipped and records the tracking number. Calls `@orders.order_repo.update_order_status` and publishes `order.shipped` via `@core.events.publish`. Also triggers `@core.email.send` with a shipment notification.

### orders.order_mgmt.get_order_timeline
Returns a chronological list of status changes for an order. Calls `@orders.order_repo.find_by_id` and reads the `status_history` JSON column.

---

## orders.order_repo

### orders.order_repo.create_order
Inserts a new order header and its line items in a single transaction. Calls `@core.db.execute` repeatedly within a db transaction.

### orders.order_repo.find_by_id
Fetches an order header and line items by order id. Calls `@core.db.fetch_one` for the header and `@core.db.fetch_many` for items.

### orders.order_repo.find_by_user
Fetches all orders for a user id with pagination. Calls `@core.db.fetch_many` with a user_id filter and ORDER BY created_at DESC.

### orders.order_repo.update_order_status
Updates the `status` field and appends an entry to the `status_history` JSON column. Calls `@core.db.execute`.

### orders.order_repo.delete_order
Hard-deletes an order and its line items. Only used in test teardown and admin tooling. Calls `@core.db.execute`.

### orders.order_repo.find_orders_by_status
Fetches all orders with a given status, used for background processing queues. Calls `@core.db.fetch_many` with a status filter.

---

## payments.gateway

### payments.gateway.charge
Initiates a payment charge for a given amount and payment method token. Calls the external payment provider API and stores the result via `@core.db.execute`. Publishes `payment.charged` via `@core.events.publish`.

### payments.gateway.capture_charge
Captures a previously authorised charge. Calls the provider API and updates the charge record via `@core.db.execute`.

### payments.gateway.void_charge
Voids an uncaptured authorisation. Calls the provider API and marks the charge as voided via `@core.db.execute`.

### payments.gateway.get_charge_status
Loads the stored charge record for an order. Calls `@core.db.fetch_one` against the charges table.

### payments.gateway.handle_webhook
Processes inbound payment provider webhook events. Validates the signature, then routes to `@payments.gateway.capture_charge` or `@payments.gateway.void_charge` depending on event type. Publishes the result via `@core.events.publish`.

### payments.gateway.list_charges_for_order
Returns all charge attempts for a given order id. Calls `@core.db.fetch_many` against the charges table.

---

## payments.refunds

### payments.refunds.issue_refund
Initiates a refund for a given charge. Calls `@payments.gateway.get_charge_status` to verify the charge is capturable, then calls the provider API and records the result via `@core.db.execute`. Publishes `payment.refunded` via `@core.events.publish`.

### payments.refunds.get_refund_status
Loads the refund record for an order. Calls `@core.db.fetch_one` against the refunds table.

### payments.refunds.list_refunds_for_order
Returns all refund records for a given order id. Calls `@core.db.fetch_many`.

### payments.refunds.partial_refund
Issues a partial refund for a specific line item amount. Calls `@payments.gateway.get_charge_status` and the provider API, then records via `@core.db.execute`.

### payments.refunds.void_pending_refunds
Cancels any pending refund records when an order is hard-deleted. Calls `@core.db.execute` to update status to `voided`.

---

## payments.invoicing

### payments.invoicing.generate_invoice
Creates an invoice record for a completed order. Calls `@core.db.fetch_one` to load the order, `@catalog.pricing.get_tax_rate` for the applicable rate, and `@core.db.execute` to insert the invoice. Also reads the billing user from `@api.auth.get_current_user` to populate the invoice header.

### payments.invoicing.get_invoice
Loads an invoice by id. Calls `@core.db.fetch_one` against the invoices table.

### payments.invoicing.list_invoices_for_user
Returns all invoices for a user, paginated. Calls `@core.db.fetch_many` filtered by user_id.

### payments.invoicing.render_invoice_pdf
Renders an invoice to PDF using a template engine. Calls `@payments.invoicing.get_invoice` to load data, then renders and returns raw bytes.

### payments.invoicing.send_invoice_email
Sends the invoice PDF to the customer by email. Calls `@payments.invoicing.render_invoice_pdf` and `@core.email.send`.

### payments.invoicing.void_invoice
Marks an invoice as void. Calls `@core.db.execute` to update the status field and publishes `invoice.voided` via `@core.events.publish`.

---

## core.db

### core.db.fetch_one
Executes a SELECT query expected to return zero or one row. Manages connection acquisition from the pool and row mapping to a dict. Returns `None` on empty result.

### core.db.fetch_many
Executes a SELECT query and returns all matching rows as a list of dicts. Applies limit/offset for pagination support.

### core.db.execute
Executes an INSERT, UPDATE, or DELETE statement. Returns the number of affected rows. Manages auto-commit for single-statement operations.

### core.db.execute_in_transaction
Executes a list of SQL statements atomically within a single transaction. Rolls back on any exception and re-raises.

### core.db.get_connection
Acquires a raw connection from the pool for advanced use cases. Callers are responsible for commit/rollback and return.

### core.db.health_check
Runs a lightweight `SELECT 1` to verify database connectivity. Used by the health endpoint and startup probes.

---

## core.cache

### core.cache.get
Retrieves a value from the cache by key. Returns `None` on miss. Deserialises the stored JSON blob.

### core.cache.set
Stores a value in the cache under the given key with an optional TTL in seconds. Serialises to JSON before storage.

### core.cache.delete
Deletes a single cache key. No-op if the key does not exist.

### core.cache.delete_pattern
Deletes all keys matching a glob pattern. Used for bulk invalidation of related entries (e.g. all pricing keys for a product).

### core.cache.exists
Checks whether a key exists in the cache without loading its value. Returns a boolean.

### core.cache.increment
Atomically increments an integer counter stored at a key. Used for rate limiting and usage tracking.

### core.cache.flush_namespace
Deletes all keys under a given namespace prefix. Used during integration test teardown.

---

## core.email

### core.email.send
Sends a transactional email. Accepts `to`, `subject`, `html_body`, and optional `attachments`. Queues the message via the configured provider SDK.

### core.email.send_bulk
Sends the same email to multiple recipients. Batches calls to `@core.email.send` respecting provider rate limits.

### core.email.render_template
Renders an email HTML body from a named Jinja2 template and a context dict. Returns the rendered string for use with `@core.email.send`.

### core.email.validate_address
Validates an email address format and optionally checks the domain's MX record. Returns a boolean.

---

## core.events

### core.events.publish
Publishes a domain event to the event bus. Accepts an event name and a payload dict. Serialises to JSON and writes to the configured broker topic. Also calls `@orders.order_repo.find_by_id` to enrich order-related events with order metadata before publishing.

### core.events.subscribe
Registers a handler function for a given event pattern. Used at application startup to wire up async consumers.

### core.events.get_event_history
Returns recent published events for a given topic, loaded from `@core.db.fetch_many` against the events log table.

### core.events.replay_events
Replays events from the log for a given topic and time range. Calls `@core.events.get_event_history` and re-publishes each via `@core.events.publish`.
