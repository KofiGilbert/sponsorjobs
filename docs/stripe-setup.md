# Turning on payments (Stripe)

The broker at `https://api.sponsorjobs.ai` already knows how to sell the two passes and the extra
interview packs. It stays switched off ("billing is not configured") until you give it a few Stripe
keys. This page walks you through it, one click at a time.

**What Stripe costs:** no monthly fee. Each successful US card payment costs **2.9% + 30 cents**
(for example, $0.30 + $0.84 = $1.14 on the $29 pass). Cards issued outside the US cost an extra
1.5%. You pay nothing while nobody is buying.

**Golden rule:** keys go into the terminal only. Never paste a key into a chat, an email, a file in
the repo, or a screenshot.

## 1. Create the Stripe account for BlackOrigin

1. Go to <https://dashboard.stripe.com/register>.
2. Sign up with the BlackOrigin business email.
3. Stripe asks you to "Activate payments". Fill in the business details for BlackOrigin (legal
   name, address, tax id/EIN, the bank account payouts go to). Until this is done you can only use
   test mode.
4. In **Settings** (gear icon) → **Business** → **Public details**, set the public business name to
   `SponsorJobs` and the support email. This is what buyers see on the checkout page and receipt.

## 2. Create the five products

Do this five times, once per row in the table.

1. In the left menu click **Product catalog** → **+ Add product**.
2. **Name**: from the table.
3. Under pricing choose **One-off** (NOT "Recurring"; nothing auto-renews).
4. **Amount**: from the table, currency **USD**.
5. Click **Add product**.
6. Open the product you just made. Under **Pricing**, click the price, and copy its **price ID**
   (it starts with `price_`). Write it down next to the row; you need it in step 4.

| Name | Amount | Price ID goes into |
| --- | --- | --- |
| Job Hunt Pass (30 days, 3 live interviews) | $29.00 | `STRIPE_PRICE_PASS30` |
| Season Pass (90 days, 9 live interviews) | $69.00 | `STRIPE_PRICE_PASS90` |
| 1 extra live interview | $9.00 | `STRIPE_PRICE_PACK_1` |
| 3 extra live interviews | $24.00 | `STRIPE_PRICE_PACK_3` |
| 5 extra live interviews | $39.00 | `STRIPE_PRICE_PACK_5` |

The amount you set in Stripe is what people pay. The app only shows labels ($29, $69, $9, $24,
$39), so if you ever change a price in Stripe, tell me and I will change the labels to match.

## 3. Create the webhook (how Stripe tells us someone paid)

1. Left menu → **Developers** → **Webhooks** (Stripe may call it **Event destinations**).
2. Click **+ Add destination** (or **Add endpoint**).
3. Events: search for and tick **`checkout.session.completed`**. Nothing else is needed.
4. Destination type: **Webhook endpoint**.
5. Endpoint URL: `https://api.sponsorjobs.ai/billing/webhook`
6. Click **Create destination**.
7. On the page that opens, find **Signing secret** and click **Reveal**. It starts with `whsec_`.
   Keep this page open for the next step.

## 4. Give the keys to the broker

Your secret API key is in **Developers** → **API keys** → **Secret key** → **Reveal** (it starts
with `sk_live_`).

Open Terminal and go to the worker folder:

```sh
cd ~/sponsorjobs/worker
```

Then run these one at a time. Each one asks "Enter a secret value:". Paste the value (it will not
show as you paste; that is normal) and press Enter.

```sh
npx wrangler secret put STRIPE_SECRET_KEY        # paste the sk_live_... key
npx wrangler secret put STRIPE_WEBHOOK_SECRET    # paste the whsec_... signing secret
npx wrangler secret put STRIPE_PRICE_PASS30      # paste the $29 pass's price_... id
npx wrangler secret put STRIPE_PRICE_PASS90      # paste the $69 pass's price_... id
npx wrangler secret put STRIPE_PRICE_PACK_1      # paste the $9 pack's price_... id
npx wrangler secret put STRIPE_PRICE_PACK_3      # paste the $24 pack's price_... id
npx wrangler secret put STRIPE_PRICE_PACK_5      # paste the $39 pack's price_... id
```

Each secret takes effect straight away; there is nothing to redeploy for the keys themselves.
(The billing code must already be deployed: that is a separate step I will ask you to approve.)

## 5. Check it works

1. Open <https://api.sponsorjobs.ai/billing/offers> in your browser. You should see both passes
   listed under `"passes"`. If you still see `"passes": []`, the key or a pass price id is missing.
2. In the app, buy the Job Hunt Pass with your own card. After paying, the app should show the pass.
3. Refund yourself: Stripe → **Payments** → click the payment → **Refund**. (Refunding does not
   take the pass away automatically; that is fine for a test.)
4. In Stripe → **Developers** → **Webhooks** → your endpoint, the delivery should show `200 OK`.
   A `400` there means the webhook signing secret is wrong: run the `STRIPE_WEBHOOK_SECRET` command
   again with the right `whsec_` value.

## Want to try with fake money first? (optional)

Stripe has a test mode (toggle **Test mode** / **Sandbox** at the top of the dashboard). Everything
above works the same there: products, prices and the webhook made in test mode have their own ids
and their own `whsec_` secret, and the key starts with `sk_test_`. Put the test key in with
`npx wrangler secret put STRIPE_TEST_SECRET_KEY` (and the test price ids and test webhook secret in
the same names as above), then pay with card `4242 4242 4242 4242`, any future date, any CVC. When
you switch to live, replace every value with its live version and set `STRIPE_SECRET_KEY`; the live
key always wins over the test key.
