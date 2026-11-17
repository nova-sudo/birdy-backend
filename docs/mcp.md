# Birdy for Claude, ChatGPT and Cursor

Birdy has a connector (an MCP server) that lets the AI app you already use read your Birdy data and, if you allow it, make changes. Ask Claude "how are my clients doing today?" and it checks your accounts, flags the ones that need you, and can pause, scale or swap ads once you confirm.

This page is written for the account owner. The [developer appendix](#developer-appendix) at the end covers the OAuth flow, tokens and limits.

## Connect

Birdy's connector link is `API_PUBLIC_URL/mcp`. In production that is:

```
https://birdy-backend.vercel.app/mcp
```

You can copy it from **Settings → Connect to AI** in Birdy. Only the account owner can connect an app, and the account needs an active or trial subscription.

When you connect, your browser opens a Birdy page. Log in if asked, check the app's name and the site it will send you back to, pick what it can do (see [View only or make changes](#view-only-or-make-changes)) and click **Allow**.

### Claude (claude.ai) and Claude Desktop

1. Open **Settings → Connectors** and choose **Add custom connector**.
2. Name it **Birdy** and paste the link above.
3. Click **Connect**, log in to Birdy and choose what Claude can do.

### ChatGPT

1. Open **Settings → Connectors → Advanced** and turn on **Developer mode**.
2. Choose **Create**, name it **Birdy**, paste the link and pick **OAuth** for authentication.
3. Click **Create**, then log in to Birdy and choose what ChatGPT can do.

### Cursor

1. Open **Settings → MCP** and choose **Add new MCP server**.
2. Name it **Birdy**, set the type to **HTTP** (streamable) and paste the link.
3. Click **Login** next to Birdy, then log in and choose what Cursor can do.

### Claude Code

```bash
claude mcp add --transport http birdy https://birdy-backend.vercel.app/mcp
```

Start Claude Code, run `/mcp`, pick **birdy** and choose **Authenticate**. Log in to Birdy in the browser and choose what Claude Code can do.

### Any other MCP client: a manual token

If an app can't do the browser login, create a token in **Settings → Connect to AI → Create token for developers**. Give it a name, choose view only or make changes, and copy the token straight away (it is shown once). Send it as `Authorization: Bearer <token>` to the connector link. Manual tokens last 365 days and are listed and revoked next to your other connections.

## View only or make changes

Every connection has one of two access levels, chosen on the Birdy consent page:

| Access | The app can |
|---|---|
| **View my data** (`birdy.read`) | Read your clients, metrics, leads, agent runs and cards, alerts, reports and settings. It can't change anything. |
| **View and make changes** (`birdy.write`) | Everything above, plus create or edit alerts, notes, targets and Slack reports, change your agent's settings, and (with your confirmation) apply changes to live ads. |

A view-only connection that tries to change something is refused with a clear message. You can't upgrade a connection in place: disconnect it and connect again with the other access level.

## What it can do

The connector has 119 tools in the families below. The full list, generated from the server, is in the [appendix](#tool-reference). In plain terms:

- **Check-in** (`watch`): `check_my_accounts` gives one verdict per client (who needs you and why). `get_fired_alerts` and `explain_alert` cover what went off.
- **Clients** (`group`, `client`, `summary`, `compare`, `multi_window`, `meta`, `meta_live`, `ghl`, `hp`, `leads`, `unified_leads`, `attribution`, `call_analysis`, `custom_metrics`): find a client, read its notes and targets, metrics, period comparisons, Meta and GoHighLevel numbers, leads and call recordings, tracking status, and your own custom metrics. You can add or remove client notes and set targets.
- **Birdy Media Buying Agent** (`stock_agent`): its overview, runs, cards, lessons and results; apply, dismiss or edit a card; undo a line; post the weekly report; change its settings and Slack template.
- **Your custom agents** (`agent`, `custom_agent`): list agents and set the default; read playbooks, activity, proposals and memory; accept or reject proposals, tune rules, targets and overrides, add memory, start a dry run, turn an agent on or off.
- **Suggestions** (`suggestions`): read, apply, deny or undo the weekly suggestions.
- **Creatives** (`creatives`): find ads and ad accounts, list and open creative tests, draft a test, post it, scale a winner.
- **Alerts** (`alert`): list, create and edit alerts.
- **Slack reports** (`slack_reports`): list, create, edit and delete reports; send a test.
- **Reply to leads** (`leads`): `send_lead_message` sends a real SMS or email to a lead through GoHighLevel, from the client's own number or address. It can't be undone, so it uses the confirm code flow below: the preview shows who it goes to (phone or email partly hidden), the channel, the subject and the exact message. Send the same text in the second call or it is refused. A message can be up to 1600 characters.
- **Report designer** (`report_designer`): `preview_report` shows a Slack report as the real message with your numbers. `propose_report` saves a draft of the schedule and the whole Slack message. Your live report does not change until you save the draft in Birdy's report designer.
- **Refresh** (`refresh`): refresh one client's data and check where it stands.

The server also offers ready-made prompts, shown in the AI app's prompt menu as **Monday review**, **What changed?**, **Call prep**, **Agent recap**, **Daily check-in** and **Fix my worst client**. **Settings → Connect to AI** has a daily check-in prompt you can paste into a scheduled Claude task.

### How Birdy sounds and looks in the AI app

Set in `ai/mcp/voice.py` (picked from `mocks/mcp-flavour`):

- **Voice.** The server instructions ask the assistant to answer like a senior media buyer who talks straight: at most three opening bullets, several clients sorted worst first as Act / Watch / Fine, a plain reason when a number moved, and at most three numbered next steps the owner can approve with "do 1". Weekly recaps and the daily check-in use a morning brief (headline, The win, The worry, The number).
- **Icon and website.** The server lists the framed mascot (`{API_PUBLIC_URL}/birdy-mcp-icon.png`) and Birdy's web address, for apps that show them.
- **Tool titles.** Every tool has a plain-English title taken from its activity label in the present ("Checking alerts"), shown on the tool-call chip. A new label must start with a verb in `voice._ING` (a test checks).
- **Sign-off.** Each successful external result ends with a text line `Birdy: see it in Birdy <link> · this request cost 0.24 credits (412.1 left)`. Look-ups (not changes) also carry a one-paragraph answer-style reminder, because AI apps don't reliably pass the server instructions to the model but it always reads tool results. It is added after the charge, so it is never charged for. Refused and failed calls get none, and internal calls (Birdy's own chat) never do.
- **Refusals** say what happened in Birdy's words and that no credits were used.
- **First hello.** `get_started` returns the active clients Birdy can see, the connection's access, what Birdy can do and three questions to try. The instructions ask the assistant to call it on the first Birdy request in a chat.

### Changes to live ads need a confirm code

Tools that change live ads or campaigns, or post to Slack, work in two steps so nothing happens by surprise:

1. The app calls the tool. Birdy changes nothing and returns a **preview** (what would happen) and a one-time **confirm code**.
2. The app shows you the preview. Only after you say yes does it call the tool again with the code.

A confirm code:

- is valid for **10 minutes**;
- can be used **once**;
- is **bound to the exact arguments** of the preview, for the same user and tool. If the second call asks for different lines, another card or anything else, it is refused and nothing changes. Ask for a new preview.

If a name is ambiguous ("Acme" matches "Acme Dental" and "Acme Law"), the tool returns the candidates and changes nothing.

## Credits and the activity log

### What a request costs

Every request from a connected app uses Birdy credits. The price depends on how much data comes back:

```
base fee + per-10k-characters rate x (response characters / 10,000) + live fee
```

The live fee only applies when a change to live ads is actually carried out with a valid confirm code. The total is rounded to 2 decimals and capped per call. The admin markup is not applied: the rates are exactly what the user pays.

| Setting | Default | Meaning |
|---|---|---|
| `mcp_base_fee` | 0.2 | Every charged request. |
| `mcp_per_10k_chars` | 1.0 | Per 10,000 characters of the response. |
| `mcp_live_fee` | 1.0 | Added when a live change runs after a confirm code. |
| `mcp_max_per_call` | 10 | The most one request can cost. |

The first step of a live tool (the preview) is a normal request, with no live fee.

**Free.** A call that is refused or fails costs nothing: view-only access, no subscription, out of credits, the rate limit, a bad or expired confirm code, a changed state, a tool that raises an error, or a result with `ok: false` or a top-level `error`.

**Out of credits.** When credit enforcement is on, a call is refused before it runs and the message tells the owner to top up in **Settings → Billing**. When enforcement is off, calls are metered but not blocked. The charge comes out of the account owner's balance and shows in the usual credit usage history (`source: "mcp"`, `feature: "mcp_request"`). If charging fails, the request still succeeds. Features that already meter AI use (lead summary, call analysis, lesson shaping) keep doing so on top.

**Admin settings.** An admin sets the four values in the credits settings with `PUT /api/admin/credits/config` (`mcp_base_fee`, `mcp_per_10k_chars`, `mcp_live_fee`, `mcp_max_per_call`). Each is a number from 0 to 100. Changes apply with no deploy.

### Activity log

**Settings → Connect to AI**, below **Your connections**, has an activity log: a table of what connected apps did. Each row shows the time, the app, what happened, the kind (look-up, change, preview, live change or refused) and the credits. The header says how many requests and credits the period covers. You can filter by app, kind, client and period (7, 30 or 90 days), and **Export CSV**.

Click a row to open a side panel with the details: the app and its access level, the tool and client, the result, what was asked, what Birdy did, and the credit breakdown (request, data, live). A preview and the change it led to link to each other. When the entry is about an agent card, a suggestion or a creative test, the panel links to that page in Birdy. Nothing is undone from the log. On mobile the panel fills the screen.

The log never stores tokens or confirm codes. Phone numbers and emails are partly hidden. Message bodies are kept, so you can see exactly what was sent to a lead. Entries are kept for **180 days**, then removed. Too many requests in a minute (the rate limit) show as one row per minute with a count.

## Alerts and emails

You can have Birdy email you when an alert fires. Turn on **Also email me** in the alert form on the Alerts page, or ask Claude to create the alert with email as a notification channel.

- The email goes to your account email.
- You get **one email each time the alert fires** (when it goes from not triggered to triggered), not one per check. If it recovers and fires again, you get another.
- The email has an **Ask Claude about this** link that opens Claude with the alert already described, so you can go straight to "why did this happen?" if the Birdy connector is added.
- The same link is added to Slack alert messages.

**Timing.** The alert job runs **hourly** (at 15 minutes past each hour). An email can arrive up to an hour after the problem started.

## What the connector can't do

By design, an outside AI app can't:

- touch **billing** or plans, or top up credits;
- connect or remove **integrations** (Meta, GoHighLevel, Slack and so on);
- change **account settings** or install tracking;
- use any **admin** tool;
- run the **refresh-all** cycle (it can refresh one client at a time).

Connector requests use credits (see [Credits and the activity log](#credits-and-the-activity-log)); connecting an app does not. Connector calls are limited to 120 a minute per user.

## Revoking access

Open **Settings → Connect to AI**. Every connected app (and every developer token) is listed with its access level and when it was last used. Click **Disconnect** on a connection and it stops working at once: the very next call with that app's current access is refused, not an hour later. You can reconnect any time.

If your subscription lapses, connector calls stop with a message to reactivate. If you run out of credits, they stop with a message to top up.

## Known limits (v1)

- **Creative tests can't start from new uploads.** `draft_creative_test` works from existing ads (a "revive" of an old creative), not from a file you attach in chat.
- **GoHighLevel refresh may need a second call.** If `refresh_status` shows **stale** for GoHighLevel, the refresh stopped partway: call `refresh_client_data` again. A GoHighLevel refresh started from Claude can also lag by up to about 10 minutes before it begins.
- **The Slack test preview leaves out the "Birdy's take" line.** The preview says one is written at send time; the real message includes it.
- **Ask Birdy can't set email when it creates an alert.** Alerts created in Birdy's own chat are in-app only. Switch **Also email me** on afterwards in the alert form, or create the alert from Claude with `notification_channels`.
- Alert emails need the sender to be configured on the server (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`); without it alerts still fire in the app and Slack.

---

## Developer appendix

### The flow

Birdy is an OAuth 2.1 authorization server and an MCP resource server in one FastAPI app. The MCP endpoint is streamable HTTP at `{API_PUBLIC_URL}/mcp`.

1. **Discovery.** `GET /.well-known/oauth-protected-resource/mcp` names the authorization server; `GET /.well-known/oauth-authorization-server` lists the `authorization_endpoint`, `token_endpoint`, `registration_endpoint` and `revocation_endpoint`. An unauthenticated call to `/mcp` returns `401` with a `WWW-Authenticate` header that points at the resource metadata.
2. **Dynamic client registration** (`POST` to the registration endpoint). Public clients (`token_endpoint_auth_method: none`) are supported.
3. **Authorize** with PKCE (S256 only). Birdy stores the request and redirects the browser to `FRONTEND_URL/connect/authorize?request=<id>`.
4. **Consent.** The page loads `GET /api/oauth/requests/{id}` (needs the Birdy login cookie) and submits `POST /api/oauth/requests/{id}/decision` with `{ "allow": true, "access": "read" | "write" }`. The POST must be `application/json` from an allowed origin (CSRF guard). An admin impersonating a user can't approve. Approval requires an active or trialing subscription. The response is `{ "redirect_to": ... }` carrying the authorization code.
5. **Token.** Exchange the code for tokens at the token endpoint. Codes are single use.
6. **Call `/mcp`** with `Authorization: Bearer <access_token>`.

`scripts/mcp_live_check.py` runs this whole flow against a local server (see below).

### Tokens, scopes and limits

| | |
|---|---|
| Scopes | `birdy.read`, `birdy.write` (a write token also has read). Every tool is tagged `read` or `write`; live tools also carry `live`. |
| Access token | JWT with `type: "mcp"`, 1 hour. |
| Refresh token | Opaque, rotates on every use, 90 days. Using an old one a second time is refused. |
| Manual token | Created in Settings → Connect to AI, `DEFAULT_EXPIRY_DAYS = 365`. |
| Revocation | Every call re-checks the grant, so revoking in Birdy (`DELETE /api/mcp/connections/{id}`) takes effect on the next call. |
| Rate limit | 120 calls per minute per user for external tokens (counted in Mongo). |
| Subscription | External calls need an `active` or `trialing` subscription. |
| Credits | Every successful external call is charged (see [Credits and the activity log](#credits-and-the-activity-log)). With enforcement on, a call with no credits left is refused. Refused and failed calls are free. |
| Internal calls | Ask Birdy's own orchestrator uses `type: "access"` tokens, skips these gates, the activity log and the request charge, and keeps its own credit metering. |

`user_id` is always taken from the verified token, never from a tool argument.

A token with no Birdy scope (neither `birdy.read` nor `birdy.write`) is refused for every tool, reads included. Admins viewing as a user cannot mint manual tokens.

### Refusals

A refused call is free, is logged with a reason and sends an `mcp_refused` event. The reasons:

| Reason | Why |
|---|---|
| `no_scope` | The token has neither `birdy.read` nor `birdy.write`. |
| `view_only` | A view-only token called a tool that makes changes. |
| `no_subscription` | The subscription is not `active` or `trialing`. |
| `out_of_credits` | Enforcement is on and the balance is 0 or less. |
| `rate_limit` | More than 120 calls in a minute. Logged as one row per minute, with a count. |
| `bad_code` | A live tool got a missing, expired, used or mismatched confirm code. |
| `state_changed` | The confirm code was fine, but the thing it was previewed on has changed since. Ask for a new preview. |

### Error shapes

Raise `ToolError` for not-found and bad input. Return `{"ok": false, "error": "..."}` for a refused business outcome (for example a rejected live change). Both are free of charge and are logged as errors. A top-level `error` key without `ok` is accepted for older tools, but new tools should use the two shapes above.

### Server settings

| Env var | Meaning |
|---|---|
| `API_PUBLIC_URL` | Public URL of the API. The OAuth issuer, and `{API_PUBLIC_URL}/mcp` is the connector link. Default `https://birdy-backend.vercel.app`. |
| `FRONTEND_URL` | Where the consent page lives. Default `https://birdy-beta.vercel.app`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE` | SMTP server for alert emails (nodemailer-style). Gmail: `smtp.gmail.com`, `465`, `true`. |
| `SMTP_USER`, `SMTP_PASS` | SMTP login. For Gmail, the address and a [Google app password](https://myaccount.google.com/apppasswords). Without host, user and password, emails are skipped. |
| `POSTHOG_API_KEY` | Project key for server-side PostHog events. Without it, no events are sent. |
| `POSTHOG_HOST` | PostHog host. Default `https://us.i.posthog.com`. |
| `EMAIL_FROM` | Sender. Default `Birdy <SMTP_USER>`. Gmail only sends as the login address or a verified alias. |

### Activity API

All endpoints need the Birdy login and only return the signed-in user's own entries. Entries live in the `mcp_activity` collection, one per external call, with a TTL index of 180 days (`services/mcp_activity.py`).

| Endpoint | What it does |
|---|---|
| `GET /api/mcp/activity` | A page of entries, newest first, 50 at most. Filters: `app` (grant or token id), `kind` (`lookup`, `change`, `preview`, `live`, `refused`), `client_group_id`, `from`, `to`, `confirmed_after` (the entries that confirmed a given preview). Default period is the last 7 days. Pass the returned `next_cursor` as `cursor` for the next page. The first page also returns `totals` (requests and credits for the whole filtered period). |
| `GET /api/mcp/activity/apps` | The apps seen in the log, including disconnected ones (marked `revoked`). |
| `GET /api/mcp/activity/{id}` | One entry in full (redacted args, result excerpt, credits, `confirmed_after`, `link`). `404` for anyone but the owner. |
| `GET /api/mcp/activity.csv` | The filtered list as CSV: time, app, kind, summary, client, result, credits. No arguments. At most 10,000 rows. |

Logging, charging and events run after the tool returns and never change its result. The charge and the log write share a 300 ms budget. If it runs out, a warning is logged and the result is still returned.

### PostHog events

`services/posthog_events.py` sends events from the server (fire-and-forget, 2 s timeout) when `POSTHOG_API_KEY` is set. The `distinct_id` is the Birdy user id. No message text, arguments or client names are sent.

| Event | When |
|---|---|
| `mcp_connected` | A grant is approved, or a manual token is created. |
| `mcp_disconnected` | A connection is revoked. |
| `mcp_tool_called` | Each external call (tool, kind, result status, credits, response size, duration, app name). |
| `mcp_live_confirmed` | A live change ran after a valid confirm code. |
| `mcp_refused` | A refused call (tool and reason). |

### Deploy order

Deploy the backend before the frontend: the admin credits page expects the new MCP rate fields from the backend.

### Checking it locally

```bash
API_PUBLIC_URL=http://localhost:8000 FRONTEND_URL=http://localhost:3005 uvicorn main:app --port 8000
MCP_LIVE_CHECK=1 python scripts/mcp_live_check.py
```

The script creates one throwaway user in the database named in `.env`, runs the real flow with a FastMCP client (including a read-only token, refresh rotation and revoking), and deletes everything it created. It is outside `tests/` so `pytest` never runs it.

### Tool reference

Generated from the registered tools with `python scripts/mcp_tool_table.py`. "make changes + confirm code" tools use the two-step flow above.


<!-- 119 tools; generated by scripts/mcp_tool_table.py -->

### agent

| Tool | Access | What it does |
|---|---|---|
| `list_agents` | view | List the user's agents with id, name, kind (stock or custom), status and which one is the default. |
| `set_default_agent` | make changes | Make one agent the default for every agent tool, across all of the user's connections. |

### alert

| Tool | Access | What it does |
|---|---|---|
| `create_alert` | make changes | Create a new alert that monitors a metric and triggers when a condition is met. |
| `get_alerts` | view | List the user's alerts with their conditions, status, and trigger history. |
| `update_alert` | make changes | Update an existing alert. |

### attribution

| Tool | Access | What it does |
|---|---|---|
| `attribution_overview` | view | One tracking row per client across all of the user's clients: tracking method, whether the script is installed and last seen, whether an ad click was seen, tracked leads and ad-attributed leads. |
| `attribution_status` | view | Whether one client's lead tracking is set up and working: is the Birdy tracking script live, has an ad click been seen, how many leads are attributed to an ad so far, plus the setup checklist (which stages are done and the next step to take). |
| `funnel` | view | One client's landing-page funnel: views, form starts and opt-ins with the drop-off between stages, the previous period for comparison, and per-day rows. |
| `get_closes_by_ad` | view | Which campaigns, ad sets or ads produce the closes (won GoHighLevel opportunities), with revenue, leads, close rate, spend and cost per close, best first. |
| `leads_by_ad` | view | Leads attributed to each Meta ad for one client, by the ad that brought the person in (via Birdy's tracking script), most leads first. |

### call_analysis

| Tool | Access | What it does |
|---|---|---|
| `analyze_call_recordings` | make changes | Download, transcribe (Whisper) and summarize up to 15 of a client's call recordings, newest first, and return a condensed summary and outcome per call to diagnose what is going wrong (agent performance, lead quality, objections, no-answers). |
| `get_call_recordings_summary` | view | Count the call recordings available for one client in a date window, without touching any audio: total calls, how many have recordings, recorded minutes, the split by agent and direction, and how many are already transcribed. |

### client

| Tool | Access | What it does |
|---|---|---|
| `add_client_note` | make changes | Add a note to a client, for something the data cannot show: a call, a change of plan, why a month looked odd. |
| `delete_client_note` | make changes | Delete one note from a client. |
| `get_client` | view | One client's profile: id, name, targets (cost per lead and others), ad account currency, which integrations are connected (Meta, GoHighLevel, call center), when its Meta and GoHighLevel data last refreshed, and its latest 10 notes. |
| `get_client_notes` | view | All notes on one client, newest first. |
| `set_client_target` | make changes | Set one or more of a client's targets, the goals the dashboard and agents measure the client against. |

### compare

| Tool | Access | What it does |
|---|---|---|
| `compare_periods` | view | Compare two date presets side-by-side with absolute and percentage change for each metric. |

### creatives

| Tool | Access | What it does |
|---|---|---|
| `draft_creative_test` | make changes | Create or update a DRAFT creative test in Birdy. |
| `find_ads` | view | Search the owner's cached Meta ads across their ad accounts by name, ad code (CT-0042) or text, with 30-day spend, leads and CPL. |
| `get_creative_test` | view | One creative test in full: its targets, creative and per-account ads with post state, Meta status and score. |
| `list_ad_accounts` | view | The owner's Meta ad accounts, with the client names that share each one. |
| `list_adsets` | view | The ad sets of one ad account, each flagged with whether it can serve a destination (lead_form, website or whatsapp). |
| `list_creative_tests` | view | The owner's creative tests with code, status, verdict and summary, newest first. |
| `post_creative_test` | make changes + confirm code | Post a drafted creative test to Meta. |
| `scale_creative_test` | make changes + confirm code | Add more ad accounts to a creative test that is already posted. |

### custom_agent

| Tool | Access | What it does |
|---|---|---|
| `accept_proposal` | make changes | Say yes to a proposal: a rule change is applied to the rule (a new rule version); a "make it a rule?" suggestion is saved as a lesson. |
| `activate_agent` | make changes + confirm code | Switch a custom agent on, so it runs on its schedule and acts on its clients' live Meta ads (what the owner allowed). |
| `add_agent_exception` | make changes | Teach a custom agent an exception that applies only to some ads. |
| `add_agent_memory` | make changes | Teach a custom agent a general fact or preference about how to run the owner's ads, in the owner's words. |
| `deactivate_agent` | make changes | Turn the Birdy Media Buying Agent off. |
| `delete_agent_memory` | make changes | Make a custom agent forget one thing it remembers (an id from get_agent_memory). |
| `get_agent_activity` | view | What a custom agent's owner decided on its work items, newest first: approved, denied and auto-run actions, with whether each can still be undone. |
| `get_agent_claims` | view | Who acts on each client a custom agent watches: this agent, another agent, or nobody. |
| `get_agent_confidence` | view | How ready a custom agent is to be switched on: its score, whether it is `ready`, and what is `missing`. |
| `get_agent_history` | view | How far each watched client's Meta ad history has been pulled for a custom agent. |
| `get_agent_impact` | view | A custom agent's impact for one month: what it paused or changed, the spend it saved and how that moved. |
| `get_agent_memory` | view | Everything a custom agent remembers: general facts and exceptions (with their client, ad name and weekday filters), oldest first. |
| `get_agent_offers` | view | Offers waiting on the owner: rules the agent has earned the right to run on its own (answer with graduate_rule), and notices that it slowed itself down. |
| `get_agent_playbook` | view | A custom agent's playbook: each rule's params, mode and per-client overrides, each client's CPL target and currency, and which setup steps are done. |
| `get_agent_proposals` | view | The rule changes (and "make it a rule?" suggestions) a custom agent proposes from what the owner denied, newest first. |
| `get_dry_run` | view | Poll a dry run started with start_dry_run: its status, and its result (what the agent would have done over the last 30 days) once done. |
| `graduate_rule` | make changes + confirm code | Answer a graduation offer from get_agent_offers: let the rule run on its own (and tell the owner, or not), or keep asking first. |
| `reject_proposal` | make changes | Say no to a proposal. |
| `set_agent_schedule` | make changes | Set the daily time a custom agent runs and reports. |
| `set_agent_slack_channel` | make changes | Choose the Slack channel a custom agent posts to, by channel id or name. |
| `set_client_override` | make changes | Override one rule param for a single client only (for example a looser cpl_multiple for one client). |
| `set_client_target_for_agent` | make changes | Set a client's cost-per-lead target, in that client's own currency. |
| `set_rule_param` | make changes | Change one number of a rule for every client (for example pause_over_target cpl_multiple to 2). |
| `start_dry_run` | make changes | Start a dry run: replay the last 30 days to see what a custom agent would have done with its current rules. |
| `undo_auto_action` | make changes + confirm code | Reverse something a custom agent did on its own in Meta, within 24 hours of it running (get_agent_activity marks which items `can_undo`; suggestion_id is the item's suggestion_id). |

### custom_metrics

| Tool | Access | What it does |
|---|---|---|
| `compute_custom_metric` | view | Evaluate a custom metric formula across client groups. |
| `create_custom_metric` | make changes | Create a new custom metric (formula) for the user. |
| `delete_custom_metric` | make changes | Delete a custom metric by id. |
| `list_available_metric_fields` | view | List every built-in metric ID the user can reference in a custom formula, along with valid operators, dashboards, and format types. |
| `list_custom_metrics` | view | List all user-defined custom metrics (formulas). |
| `update_custom_metric` | make changes | Update one or more fields on an existing custom metric. |

### ghl

| Tool | Access | What it does |
|---|---|---|
| `get_ghl_contacts` | view | Get GoHighLevel CRM contacts with name, email, phone, tags, source, and opportunity data. |
| `get_ghl_opp_stats_monthly` | view | Return month-by-month GHL opp stats for a given year. |
| `get_ghl_opp_stats_windowed` | view | Compute GHL opportunity stats (won/lost/open/abandoned, won_revenue, total_opportunities) for an ARBITRARY date window — not restricted to the 13 cached presets. |
| `get_ghl_opportunity_stats` | view | Get accurate opportunity statistics per client group: won, lost, open, abandoned counts, won revenue, and conversion rate — sourced directly from the GHL Opportunities API and pre-cached per date preset. |
| `get_ghl_tag_breakdown` | view | Get tag breakdown per client group — shows which GHL tags are applied to contacts and how many contacts each tag has. |
| `get_tag_rollup_by_campaign` | view | Get tag counts rolled up by Meta campaign/adset/ad. |

### group

| Tool | Access | What it does |
|---|---|---|
| `get_client_groups` | view | List all client groups (business clients) for the user. |

### hp

| Tool | Access | What it does |
|---|---|---|
| `get_call_center_stats` | view | HotProspector call-center stats per client for a date window: total, inbound and outbound calls, transfers, answered calls, talk time, leads called, plus connect_rate and leads_called_rate. |

### leads

| Tool | Access | What it does |
|---|---|---|
| `get_call_transcript` | view | The transcript of one recorded call with a lead, as plain text. |
| `get_lead` | view | Everything Birdy knows about one lead: contact details, opportunities and their stages, ad attribution (campaign, ad, form answers), calls, a newest-first journey timeline (ad click, form fill, site visits, calls, appointments, stage changes) and a short AI summary of the lead. |
| `get_lead_messages` | view | The live GoHighLevel conversation with one lead: SMS and email messages oldest first, plus call records that are not already logged. |
| `send_lead_message` | make changes + confirm code | Send a real SMS or email to a lead through GoHighLevel, from the client's own GoHighLevel number or address. |

### meta

| Tool | Access | What it does |
|---|---|---|
| `get_ad_insights` | view | Get Facebook individual ad-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM, quality rankings). |
| `get_adset_insights` | view | Get Facebook ad set-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM). |
| `get_campaign_insights` | view | Get Facebook campaign-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM, results/leads). |
| `get_facebook_leads` | view | Get Facebook leads with contact info (name, email, phone) and which ad/campaign generated them. |

### meta_live

| Tool | Access | What it does |
|---|---|---|
| `get_meta_insights_live` | view | Fetch campaign, adset, or ad insights DIRECTLY from Meta's API for any arbitrary date range. |
| `get_meta_insights_monthly` | view | Return month-by-month Meta ad insights (spend, impressions, clicks, results/leads) for a given year. |
| `get_meta_leads_live` | view | Fetch leads DIRECTLY from Meta's API for any arbitrary date range. |

### multi_window

| Tool | Access | What it does |
|---|---|---|
| `get_daily_metrics` | view | Meta ad spend, leads, clicks, impressions, CPL and CTR per calendar day for the last N days, per client plus a combined series. |
| `get_metrics_by_day_windows` | view | Spend, leads, GHL opportunity stats (won/lost/open), revenue, ROAS, profit and CPL across several day windows ending today, in one call. |

### refresh

| Tool | Access | What it does |
|---|---|---|
| `refresh_client_data` | make changes | Start a fresh pull of one client's ad data from Meta, its GoHighLevel leads and opportunities, or both. |
| `refresh_status` | view | Where a client's data refresh stands: for Meta and for GoHighLevel, its status ("idle", "running", "complete", "error", or GoHighLevel "stale"), the last error if any, and for Meta the job's progress (status, current step, presets done of total). |

### report_designer

| Tool | Access | What it does |
|---|---|---|
| `get_report_draft` | view | The owner's current report draft and their saved (live) report, either of which may be null. |
| `list_report_clients` | view | The owner's active clients as the report designer sees them: each one's id, name, which data sources are connected (Meta, GoHighLevel, HotProspector) and whether targets are set. |
| `list_report_metrics` | view | What a Slack report can contain: the metrics (id, label, data source, whether available and whether a target comparison exists), the periods, the placeholder syntax (e.g. |
| `preview_report` | view | Render a report as the real Slack message with the owner's own numbers (sample figures when they have none yet). |
| `propose_report` | make changes | Save a Slack report as the owner's DRAFT: the schedule plus the WHOLE Block Kit message. |

### slack_reports

| Tool | Access | What it does |
|---|---|---|
| `create_slack_report` | make changes | Create a Slack report: a message Birdy posts to Slack on a schedule or when something happens. |
| `delete_slack_report` | make changes | Delete a Slack report and its send history. |
| `list_slack_reports` | view | The owner's Slack reports, the default daily/weekly brief first: each one's id, name, whether it is on, when it sends (a schedule or an event), its Slack channel, its content template and a one-line summary of each block. |
| `send_test_slack_report` | make changes + confirm code | Post a Slack report to its channel right now as a test, with today's real figures (event reports get sample event details). |
| `update_slack_report` | make changes | Change a Slack report. |

### stock_agent

| Tool | Access | What it does |
|---|---|---|
| `add_agent_lesson` | make changes | Teach the agent something, in the owner's words (for example "never pause the brand-name ad"). |
| `apply_all_cards` | make changes + confirm code | Apply every open card in one report (run) to the clients' live Meta ads. |
| `apply_card` | make changes + confirm code | Apply a card's proposed changes to the client's live Meta ads: pause, scale or swap the ads on the lines. |
| `dismiss_card` | make changes | Decline what is left on a card. |
| `edit_card` | make changes | Edit an open card before deciding: keep only the given lines (the others are removed) and optionally rewrite its additional changes text. |
| `get_agent_card` | view | One card in full: the client's numbers, every proposed line with its state, and the agent's reasoning and next steps. |
| `get_agent_cards` | view | The agent's cards (proposed changes per client), newest first, summarised. |
| `get_agent_lessons` | view | Everything the Birdy Media Buying Agent has learned from the owner (overruled lessons included), oldest first. |
| `get_agent_month` | view | The whole-month report of the Birdy Media Buying Agent: per client the month's leads, spend, CPL and zone, and how many cards were decided. |
| `get_agent_outcomes` | view | The agent's scoreboard: moves won and lost, and its most recent scored changes (up to 20 rows; `more_rows` is true when older ones are cut; won/lost/moves cover all of them). |
| `get_agent_overview` | view | The Birdy Media Buying Agent at a glance: its status and settings, its latest report, and how many cards wait on the owner. |
| `get_agent_run` | view | One report in full: its cards (summarised), follow-ups, scoreboard summary, what the agent learned and open rule suggestions. |
| `get_agent_runs` | view | Past reports (runs) of the Birdy Media Buying Agent, newest first, without per-client detail. |
| `get_agent_slack_template` | view | One of the agent's Slack message templates: the saved one (or default), the defaults and the placeholders it may use. |
| `post_weekly_report` | make changes + confirm code | Post a report's weekly summary to the agency's Slack channel. |
| `reset_agent_slack_template` | make changes | Go back to the default for one of the agent's Slack message templates. |
| `set_agent_slack_template` | make changes | Replace one of the agent's Slack message templates. |
| `undo_card_line` | make changes + confirm code | Reverse one applied line on the client's live Meta ads (only soon after it was applied). |
| `update_agent_settings` | make changes + confirm code | Change the Birdy Media Buying Agent's settings and which clients it watches. |

### suggestions

| Tool | Access | What it does |
|---|---|---|
| `apply_suggestion` | make changes + confirm code | Approve a suggestion and run its action in the owner's live Meta ads (for example pausing the ads it names). |
| `deny_suggestion` | make changes | Decline a suggestion with a reason. |
| `get_suggestions` | view | The owner's dashboard suggestions, newest first: what Birdy's agents found, with a title, description, key stats and whether there is an action to approve. |
| `undo_suggestion` | make changes + confirm code | Reverse an applied suggestion: turn back on exactly the ads it paused (or pause what it turned on) and reopen the suggestion. |

### summary

| Tool | Access | What it does |
|---|---|---|
| `get_account_summary` | view | Get a high-level dashboard summary across all or selected client groups. |

### unified_leads

| Tool | Access | What it does |
|---|---|---|
| `get_unified_lead_stats` | view | Get cross-source lead stats: totals per source (GHL/Meta/HP) and overlap counts (GHL-only, GHL+Meta matched, GHL+HP matched, all three). |
| `get_unified_leads` | view | Get leads with cross-source matching info. |

### watch

| Tool | Access | What it does |
|---|---|---|
| `check_my_accounts` | view | One verdict per client: which accounts need the owner's attention right now, and why. |
| `explain_alert` | view | Explain one alert: why it fired or didn't. |
| `get_fired_alerts` | view | The alerts that fired in the last N days (1-90, default 7), newest first. |
