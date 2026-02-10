# Dodo Payments – Test mode integration

Stripe has been removed. This doc lists what you need from **Dodo Payments** to run in **test mode** only (same idea as Stripe test mode).

## Required details (test mode)

Get these from your Dodo dashboard or Dodo developer docs and set them in `.env`:

| Variable | Description | Example (test) |
|----------|-------------|----------------|
| **DODO_API_KEY** | Secret API key for server-side calls (test/sandbox key). | `dodo_test_...` or similar |
| **DODO_WEBHOOK_SECRET** | Secret used to verify webhook signatures (test webhook endpoint). | From Dodo “Webhooks” → create endpoint → copy signing secret |
| **DODO_PRODUCT_OR_PLAN_ID** | Product or plan ID for your Pro subscription in test catalog. | e.g. `plan_pro_monthly` or product UUID |
| **DODO_BASE_URL** | Base URL for Dodo API in test/sandbox. | e.g. `https://api-sandbox.dodopayments.com` or `https://api.dodopayments.com/v1` |

Optional but recommended:

- **FRONTEND_URL** – Already used for success/cancel and portal return URLs (e.g. `http://localhost:3000` or `https://logicdm.app`).

## Same idea as Stripe test mode

- Use **test/sandbox** API key and base URL (no live charges).
- Use **test** webhook secret for the webhook you register in the Dodo dashboard.
- Create a **test** product/plan for “Pro” and use its ID in `DODO_PRODUCT_OR_PLAN_ID`.
- When you go live, switch to live API key, live base URL, live webhook secret, and live product/plan ID.

## Backend expectations (for implementation)

The backend is stubbed to work with a typical MoR API. You may need to adjust once you have Dodo’s real API docs.

1. **Create checkout**
   - **Endpoint:** e.g. `POST {DODO_BASE_URL}/checkout/sessions` (path may differ).
   - **Body:** `customer_email`, product/plan id, `success_url`, `cancel_url`, `metadata` (e.g. `user_id`, `user_email`).
   - **Response:** `checkout_url` (or `url` / `redirect_url`) and `session_id` (or `id` / `checkout_id`).

2. **Verify session (after redirect)**
   - **Endpoint:** e.g. `GET {DODO_BASE_URL}/checkout/sessions/{session_id}`.
   - **Response:** `status` or `payment_status` (e.g. `paid`), `subscription_id` (or `subscription`), `metadata` with `user_id`.

3. **Customer portal**
   - **Endpoint:** e.g. `POST {DODO_BASE_URL}/portal/sessions`.
   - **Body:** `subscription_id`, `return_url`.
   - **Response:** `portal_url` (or `url` / `redirect_url`).

4. **Webhook**
   - **URL to register in Dodo (test):** `https://your-backend.com/webhooks/dodo`
   - **Verification:** Backend verifies signature using `DODO_WEBHOOK_SECRET` (e.g. HMAC-SHA256 of body; header name may be `X-Dodo-Signature` or similar).
   - **Events to handle:** subscription/order created, updated, cancelled (exact names from Dodo docs). Backend maps these to internal subscription status and `plan_tier`.

## Where to configure

- **Backend:** `.env` (see `.env.example`). Do not commit real keys.
- **Dodo dashboard:** Create test product/plan, test webhook endpoint (URL above), and copy API key + webhook secret into `.env`.

## After you have the real API

1. Update `app/api/routes/dodo.py`: replace placeholder URLs and request/response field names with Dodo’s actual paths and JSON shape.
2. Update `app/api/routes/webhooks.py`: align event types and payload parsing with Dodo’s webhook payload (and their signature format).
3. Re-test checkout, verify-checkout, portal, and webhooks in test mode before switching to live.
