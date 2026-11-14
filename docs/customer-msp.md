# Customer MSP service

The main WebUI exposes **Support** for tickets, support minutes,
per-app support subscriptions and payments. **Apps** exposes application requests,
launcher availability and Basic, Plus and Premium support requests.
All six topics have Dutch and English interactive guides.

## Connection and identity

Configure `SP_CONTROL_KEY_FILE` with the private, mode-0600 JSON **single control
key entry** returned by enrollment. Never use a reporter or ingest key.
Use `SP_CONTROL_URL` for a remote control API, or `SP_CONTROL_SOCKET` with an
absolute Unix socket path for a colocated installation. Socket transport preserves
HMAC authentication and signed acting-user headers; redirects are refused.
The installation key binds the organisation. No browser-supplied identity, role,
organisation, URL or key is forwarded. Known policy users and bootstrap admins
can use self service; anonymous and unrecognised accounts cannot.

Import the corresponding organisation membership artifact into the control API.
Organisation or billing administrators can see hours/payments and change app
availability and support. Members create and read their own tickets; department
and organisation scopes retain the central service's visibility rules. Internal
MSP notes are never included in the customer response.

## Availability and support

### Shared app proposals

Organisation administrators can use **Apps > App aanvragen / Request app** to
request a non-binding proposal for the whole organisation, selected departments,
or the current environment. The local server validates department keys against
the installation policy; the signed installation fixes the organisation and the
central API checks the organisation administrator role again. Browser identity,
organisation IDs, prices, payment status and deployment targets are refused.

The page-level request button includes customer apps that have no launch tile yet,
including Twenty CRM, RAGFlow and SynthPulse Office. Its catalogue intersects the
central app list with the local registry's explicit `requestable` flag. It carries
only app IDs and names, never destinations or launch permissions. Internal routing,
connector administration and tracing consoles are excluded from this chooser.
An organisation administrator can request a customer app without gaining platform
access or activating a local module. Users without the central request permission
receive no request catalogue. Existing app cards offer the same form where allowed.

`POST /api/msp/app-intake` forwards to the separate `/v1/customer/apps/{id}/intake`
route. Matching open app/scope requests share one native ticket across the
organisation's installations. A later request adds its note to that ticket.
Durable request keys prevent retries from adding another note or ticket, even
after closure. Open requests appear on the app card and in Support. Text survives
a failed submission; signing out or changing the visible account discards it.

Intake grants no app access, creates no deployment, order or invoice, and spends
no support hours. The existing paid app fulfillment endpoint and pay-then-submit
service offer workflow retain their payment requirement. The proposal must agree
which teams share one installation, which data remains private, app permissions,
and any capacity, licence or maintenance costs. Do not quote one copy per
department merely because several departments use a central app.

An installation without the signed MSP connection links to the authenticated
Cloud functionality request page. It does not infer an organisation from an
email address or reuse an HQ control key.

Availability is stored per organisation and app. Turning an app off removes its
launch destination from `/api/apps`, across reloads and users. It preserves files
and subscriptions. This is a launcher setting, not infrastructure shutdown or
revocation of direct access to an external application. Existing app/module/role
gates still apply. Once managed, an unavailable control API fails closed.

App and support requests create real MSP service tickets. Repeated pending
requests are deduplicated. Each app has Basic, Plus and Premium levels with
unset prices by default. The MSP sets the name, scope and monthly price excluding
VAT in Cockpit **App support**. Approval requires a real configured price, an
active customer contract and staff confirmation that the customer agreed.
A requested change retains the old active level and price until approved.
Cancellation is separate from disabling the app. Recording an agreement does
not provision an app, grant extra support hours or start recurring collection.
Recurring invoicing remains the MSP finance process.

Payment links appear only for invoiced requests with live Mollie payments.
No key, prices or contracts are invented. Mollie setup and payment verification
remain in the MSP administration; visiting the return URL does not settle an invoice.

## Verification

Run `./scripts/test.sh` with `tests/test_msp_service.py`,
`tests/test_apps_launcher.py`, `tests/test_org_modules_api.py`,
`tests/test_control_plane_client.py` and the feature-guide coverage tests.
Use disposable, isolated WebUI and control API state for browser tests. Do not
submit synthetic tickets, subscriptions or payments to production.

## Base package and Agentic Consultant

Support starts with the base package and weekly Agentic Consultant capacity.
The base status comes from the existing contract or an explicit package request.
Organisation/billing admins can request the base package, request consultant
capacity in half-day increments (0.5 to 7 days per week), change that capacity
or request consultant cancellation. Requests create real MSP tickets and never
schedule a consultant or collect money. The previous agreement remains active
while a change is pending. Consultant days are not converted to support minutes
or monthly day totals.

Cockpit Packages lets MSP admins record the agreed monthly rate, scope and
effective date and confirm customer acceptance. An active base customer contract
is required for activation. Stale ticket decisions fail closed. App support stays
separate, with its own Basic, Plus and Premium subscriptions. The interactive
features are `webui.msp.package` and `webui.msp.consultant`.

## Compact overview and prepaid requests

Support sits below Apps and above Settings, including when a saved navigation
order exists. The overview leads with available hours and open tickets. Package,
consultant and automation details expand on demand; invoice history is collapsed.
Paid service offers appear in Payments. HQ prepares a scoped, priced offer
against an existing accounting invoice. The customer pays through Mollie, checks
the payment, then explicitly submits the request. Payment alone creates no ticket.
Top-ups add hours once after verified payment and Add hours. New support tickets
and reopened support tickets require available hours; invoice questions and
existing ticket communication remain accessible. Draft ticket text is preserved.
An absent price or payment connection is never presented as a free purchase.

### Payment visibility

The navigation, page title and guides use **Support**. Its **Payments / Betalingen**
tab starts with a visible payment overview containing every scoped billing request,
including paid service offers and purchased hours. It shows the invoice reference,
description, VAT-inclusive amount, creation date and current status. All, Outstanding
and Paid filters keep larger histories usable; outstanding means an invoiced request
that has not been marked paid. Failed or expired payment attempts retain their visible
status. Only verified live open/pending checkout links are shown.

Priced offers and their pay-then-submit workflow remain below this overview. Financial
visibility still requires organisation or billing administrator rights. This display
change does not initiate payments or create accounting invoices.

### Annual support per scheduled job

Job details expose implementation hours and a support switch for MSP managers.
There is no hourly-rate input or monetary preview in this panel; saving preserves
an existing stored rate, while new jobs remain unpriced. Annual support is calculated by the server as 15% of implementation
hours, rounded to whole minutes; its price is 15% of the implementation value, rounded
to cents. For example, 20 hours at EUR 100 gives 3 annual support hours at EUR 300/year.
Saving enabled support adds the hours and annual amount once to the support total
and draft agreements. An unknown rate remains unpriced, never a free offer.
Only explicitly designated MSP managers can configure implementation hours and support state. Customer administrators have read-only controls and cannot enable,
edit or disable support through the API. HQ admins grant/revoke this separate
installation-scoped capability through POST /api/control/commercial/job-support/access
with org_id, installation_id, email and enabled. Customer/billing roles alone never
grant MSP access. Revocation takes effect immediately. Saved legacy terms remain
until the MSP changes their implementation basis. HQ can also disable support directly. Scheduler pause/delete operations do
not cancel the independent support commitment. Accepted agreements and funded hours
are preserved; draft additions are not spendable credits.

### Original Mollie invoice PDFs

Paid live requests expose Invoice (PDF). The authenticated proxy retrieves the
original customer Sales Invoice PDF using server-only credentials after matching
the live payment, profile and amount. Missing invoices and unavailable PDFs are
reported explicitly. This is retrieval, not automatic invoice creation or external
delivery: the invoice must already exist in Mollie Invoicing with the payment source
reference. The existing accounting invoice is never duplicated. Documents are bounded
to 512 KiB to fit the signed proxy response limit.
