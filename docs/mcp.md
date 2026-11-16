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

The connector has 113 tools in the families below. The full list, generated from the server, is in the [appendix](#tool-reference). In plain terms:

- **Check-in** (`watch`): `check_my_accounts` gives one verdict per client (who needs you and why). `get_fired_alerts` and `explain_alert` cover what went off.
- **Clients** (`group`, `client`, `summary`, `compare`, `multi_window`, `meta`, `meta_live`, `ghl`, `hp`, `leads`, `unified_leads`, `attribution`, `call_analysis`, `custom_metrics`): find a client, read its notes and targets, metrics, period comparisons, Meta and GoHighLevel numbers, leads and call recordings, tracking status, and your own custom metrics. You can add or remove client notes and set targets.
- **Birdy Media Buying Agent** (`stock_agent`): its overview, runs, cards, lessons and results; apply, dismiss or edit a card; undo a line; post the weekly report; change its settings and Slack template.
- **Your custom agents** (`agent`, `custom_agent`): list agents and set the default; read playbooks, activity, proposals and memory; accept or reject proposals, tune rules, targets and overrides, add memory, start a dry run, turn an agent on or off.
- **Suggestions** (`suggestions`): read, apply, deny or undo the weekly suggestions.
- **Creatives** (`creatives`): find ads and ad accounts, list and open creative tests, draft a test, post it, scale a winner.
- **Alerts** (`alert`): list, create and edit alerts.
- **Slack reports** (`slack_reports`): list, create, edit and delete reports; send a test.
- **Refresh** (`refresh`): refresh one client's data and check where it stands.

The server also offers ready-made prompts: a weekly client review, "why did CPL jump?", prep for a client call, and the agent's week. **Settings → Connect to AI** has a daily check-in prompt you can paste into a scheduled Claude task.

### Changes to live ads need a confirm code

Tools that change live ads or campaigns, or post to Slack, work in two steps so nothing happens by surprise:

1. The app calls the tool. Birdy changes nothing and returns a **preview** (what would happen) and a one-time **confirm code**.
2. The app shows you the preview. Only after you say yes does it call the tool again with the code.

A confirm code:

- is valid for **10 minutes**;
- can be used **once**;
- is **bound to the exact arguments** of the preview, for the same user and tool. If the second call asks for different lines, another card or anything else, it is refused and nothing changes. Ask for a new preview.

If a name is ambiguous ("Acme" matches "Acme Dental" and "Acme Law"), the tool returns the candidates and changes nothing.

## Alerts and emails

You can have Birdy email you when an alert fires. Turn on **Also email me** in the alert form on the Alerts page, or ask Claude to create the alert with email as a notification channel.

- The email goes to your account email.
- You get **one email each time the alert fires** (when it goes from not triggered to triggered), not one per check. If it recovers and fires again, you get another.
- The email has an **Ask Claude about this** link that opens Claude with the alert already described, so you can go straight to "why did this happen?" if the Birdy connector is added.
- The same link is added to Slack alert messages.

**Timing.** The alert job runs **hourly** (at 15 minutes past each hour). An email can arrive up to an hour after the problem started.

## What the connector can't do

By design, an outside AI app can't:

- touch **billing**, plans or credits;
- connect or remove **integrations** (Meta, GoHighLevel, Slack and so on);
- change **account settings** or install tracking;
- use any **admin** tool;
- run the **refresh-all** cycle (it can refresh one client at a time).

Connecting an app uses none of your credits. Connector calls are limited to 120 a minute per user.

## Revoking access

Open **Settings → Connect to AI**. Every connected app (and every developer token) is listed with its access level and when it was last used. Click **Disconnect** on a connection and it stops working at once: the very next call with that app's current access is refused, not an hour later. You can reconnect any time.

If your subscription lapses, connector calls stop with a message to reactivate.

## Known limits (v1)

- **Creative tests can't start from new uploads.** `draft_creative_test` works from existing ads (a "revive" of an old creative), not from a file you attach in chat.
- **GoHighLevel refresh may need a second call.** If `refresh_status` shows **stale** for GoHighLevel, the refresh stopped partway: call `refresh_client_data` again. A GoHighLevel refresh started from Claude can also lag by up to about 10 minutes before it begins.
- **The Slack test preview leaves out the "Birdy's take" line.** The preview says one is written at send time; the real message includes it.
- **Ask Birdy can't set email when it creates an alert.** Alerts created in Birdy's own chat are in-app only. Switch **Also email me** on afterwards in the alert form, or create the alert from Claude with `notification_channels`.
- Alert emails need the sender to be configured on the server (`RESEND_API_KEY`); without it alerts still fire in the app and Slack.

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
| Subscription | External calls need an `active` or `trialing` subscription. No credits are charged. |
| Internal calls | Ask Birdy's own orchestrator uses `type: "access"` tokens, skips these two gates and keeps its credit metering. |

`user_id` is always taken from the verified token, never from a tool argument.

A token with no Birdy scope (neither `birdy.read` nor `birdy.write`) is refused for every tool, reads included. Admins viewing as a user cannot mint manual tokens.

### Error shapes

Raise `ToolError` for not-found and bad input. Return `{"ok": false, "error": "..."}` for a refused business outcome (for example a rejected live change). Both are free of charge and are logged as errors. A top-level `error` key without `ok` is accepted for older tools, but new tools should use the two shapes above.

### Server settings

| Env var | Meaning |
|---|---|
| `API_PUBLIC_URL` | Public URL of the API. The OAuth issuer, and `{API_PUBLIC_URL}/mcp` is the connector link. Default `https://birdy-backend.vercel.app`. |
| `FRONTEND_URL` | Where the consent page lives. Default `https://birdy-beta.vercel.app`. |
| `RESEND_API_KEY` | Sends alert emails. Without it, emails are skipped. |
| `EMAIL_FROM` | Sender. Default `Birdy <alerts@birdy.ai>`; the domain must be verified in Resend. |

### Checking it locally

```bash
API_PUBLIC_URL=http://localhost:8000 FRONTEND_URL=http://localhost:3005 uvicorn main:app --port 8000
MCP_LIVE_CHECK=1 python scripts/mcp_live_check.py
```

The script creates one throwaway user in the database named in `.env`, runs the real flow with a FastMCP client (including a read-only token, refresh rotation and revoking), and deletes everything it created. It is outside `tests/` so `pytest` never runs it.

### Tool reference

Generated from the registered tools with `python scripts/mcp_tool_table.py`. "make changes + confirm code" tools use the two-step flow above.


<!-- 113 tools; generated by scripts/mcp_tool_table.py -->

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
| `graduate_rule` | make changes | Answer a graduation offer from get_agent_offers: let the rule run on its own (and tell the owner, or not), or keep asking first. |
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
| `update_agent_settings` | make changes | Change the Birdy Media Buying Agent's settings and which clients it watches. |

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
