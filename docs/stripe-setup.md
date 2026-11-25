# Turning on payments (Stripe)

The broker at `https://api.sponsorjobs.ai` sells the plans that renew automatically and the extra
interview packs. It stays switched off ("billing is not configured") until it has a few Stripe
keys. This page walks you through it, one click at a time.

What is on sale (decided 2026-11-25). The script creates exactly these four prices:

| What | Price | Renews | Price id goes into |
| --- | --- | --- | --- |
| Student plan, monthly | $29 | every month | `STRIPE_PRICE_STUDENT_MONTHLY` |
| Standard plan, monthly | $49 | every month | `STRIPE_PRICE_STANDARD_MONTHLY` |
| 1 extra live interview | $12 | one-time | `STRIPE_PRICE_PACK_1` |
| 3 extra live interviews | $33 | one-time | `STRIPE_PRICE_PACK_3` |

Each plan includes 3 live interviews of 20 minutes a period. Student includes 40 tailored
applications a month, Standard 60.

Not sold any more: the every-3-months plans ($69 and $119), the 5-pack and the old one-time
passes. The script no longer creates them. Prices an earlier run made stay in Stripe, and their
Worker secrets stay where they are, so a subscription already on one keeps renewing. To sell the
every-3-months plans again: run `python3 scripts/stripe_setup.py --sell-quarterly`, set
`SELL_QUARTERLY` to `"1"` in `worker/wrangler.jsonc`, and deploy the Worker.

The pack prices changed (they were $9 and $24). Re-running the script makes the new $12 and $33
prices and points the Worker at them. Until it is re-run, the app would show $12 and $33 while
Stripe still charges the old amounts, so run it before the Worker with this change is deployed.

Plans renew until the person cancels. The student price is for students and graduates up to 24
months after graduating, checked once with a code sent to a school email (free, built into the
app; no paid verification service). Which price someone pays is decided by the broker, never by
the app.

**What Stripe costs:** no monthly fee. Each successful US card payment costs **2.9% + 30 cents**
(for example, $0.30 + $0.84 = $1.14 on the $29 plan). Cards issued outside the US cost an extra
1.5%. Stripe Billing (subscriptions) adds **0.7%** of each subscription payment on the
pay-as-you-go plan (about $0.20 on $29). You pay nothing while nobody is buying.

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

## 2. Try it in test mode first (fake money)

Run this from the repo folder with a **test** key (Stripe dashboard → turn on **Test mode** at the
top → **Developers** → **API keys** → **Secret key** → **Reveal**, starts with `sk_test_`):

```sh
cd ~/sponsorjobs
python3 scripts/stripe_setup.py --testers <your account id>
```

It asks for the key (typing is hidden), then makes everything in the table above, the customer
portal settings and the webhook, and stores all of it on the Worker. Running it again changes
nothing that is already right (no duplicate prices). In test mode only the account ids you list
see billing; pay with card `4242 4242 4242 4242`, any future date, any CVC.

To try a renewal without waiting a month, use Stripe's **test clocks** (Billing → Subscriptions →
open the test subscription → **Run simulation**) and move time past the renewal date.

## 3. Going live (your step, by hand)

Do this only when you are ready to take real money. I never run it.

1. Stripe dashboard → turn **Test mode** off → **Developers** → **API keys** → **Secret key** →
   **Reveal** (starts with `sk_live_`).
2. In Terminal:

   ```sh
   cd ~/sponsorjobs
   python3 scripts/stripe_setup.py
   ```

   Paste the live key when asked. It creates the four plan prices and the three pack prices in
   live mode, the customer portal settings and the live webhook, and stores the live key
   (`STRIPE_SECRET_KEY`), the webhook secret, the portal settings id and the seven price ids on
   the Worker. The live key always wins over the test key.
3. **Renewal reminders.** Stripe dashboard (live) → **Settings** → **Billing** → **Subscriptions
   and emails** → **Upcoming renewal events**: set it to **7 days**. Stripe then tells the broker
   7 days before each renewal and the broker emails the person the date, the price and how to
   cancel (through our own mailer, Resend). You do not need to turn on Stripe's own "upcoming
   renewal" customer emails, but it does no harm.
4. **Failed payments.** **Settings** → **Billing** → **Revenue recovery** (older dashboards: **Manage
   failed payments**): keep **Smart Retries** on and set
   "If all retries for a payment fail" to **Cancel the subscription**. The broker then ends the
   plan when Stripe gives up. Optionally turn on **Send emails when card payments fail**.
5. **Customer portal.** The script already made our portal settings (update card, invoices,
   cancel at period end, no plan switching). To check them: **Settings** → **Billing** →
   **Customer portal**. Nothing to change.
6. **Retire the old passes.** If `STRIPE_PRICE_PASS30` or `STRIPE_PRICE_PASS90` were ever set on
   the Worker, remove them so no app sells a one-time pass any more (people who bought one keep it
   until it ends):

   ```sh
   cd ~/sponsorjobs/worker
   npx wrangler secret delete STRIPE_PRICE_PASS30
   npx wrangler secret delete STRIPE_PRICE_PASS90
   ```
7. If billing was tried in test mode, also remove `STRIPE_TEST_SECRET_KEY` and
   `STRIPE_TEST_ACCOUNTS` the same way (optional: the live key already wins).

The database table the plans need (`migrations/0014_subscriptions_students.sql`) must be applied
before the broker code that uses it is deployed: `npx wrangler d1 migrations apply DB --remote`
from `~/sponsorjobs/worker`. That is part of the deploy step I will ask you to approve.

## 4. Check it works

1. Open <https://api.sponsorjobs.ai/billing/offers>. You should see four entries under `"plans"`.
   If you see `"plans": []`, the key or the plan prices are missing.
2. In the app, open **See plans**, tick the renewal box and subscribe with your own card. After
   paying, the app shows your plan and "Renews on ..., cancel anytime".
3. In the account menu, click **Cancel**. The app says the plan will not renew; Stripe →
   **Billing** → **Subscriptions** shows it "Cancels on ...". Click **Undo** in the app to put it
   back, or leave it cancelled.
4. Refund yourself: Stripe → **Payments** → the payment → **Refund**.
5. Stripe → **Developers** → **Webhooks** → your endpoint: every delivery should show `200 OK`. A
   `400` means the webhook signing secret is wrong: run the script again.

## What the broker does with each Stripe event

| Event | What happens |
| --- | --- |
| `checkout.session.completed` | A pack is granted (one-time), or a new plan is linked to the account and the consent time kept. |
| `invoice.paid` | A plan's period is paid: its interviews and packages refill, access runs to the period end. Once per invoice. |
| `invoice.payment_failed` | The plan shows "payment didn't go through, update your card". Access still ends with the paid period. |
| `invoice.upcoming` | The renewal reminder email (date, price, how to cancel), once per period. |
| `customer.subscription.updated` | Cancel at period end, resume, status changes, shown in the app. |
| `customer.subscription.deleted` | The plan ends now. |

## Doing it by hand instead of the script

If you ever need to: create each product in **Product catalog** → **+ Add product**. For a plan
choose **Recurring**, **Monthly**; for a pack choose **One-off**. Copy each price id (`price_...`) and store it
with `npx wrangler secret put <name from the table>` in `~/sponsorjobs/worker`. Create the webhook
in **Developers** → **Webhooks** for `https://api.sponsorjobs.ai/billing/webhook` with the six
event types in the table above plus `checkout.session.async_payment_succeeded`, and store its
signing secret as `STRIPE_WEBHOOK_SECRET`. The prices the app shows come from the broker, which
must match what is set in Stripe: if you change a price in Stripe, tell me and I will change the
labels to match.
