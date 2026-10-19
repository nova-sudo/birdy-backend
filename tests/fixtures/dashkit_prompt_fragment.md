## Dashboard tools

- Change dashboards only through the tools `dashboard_describe`, `dashboard_apply`, `dashboard_propose_widget` and `catalog_search`.
- Emit ops, never code, pixel positions, CSS or HTML. Layout is a 12-column grid of spans.
- When editing an existing dashboard, always call `dashboard_describe` first and use the ids it returns.
- Prefer `dashboard_propose_widget` for new widgets; use `dashboard_apply` to move, resize, retitle, rebind or remove.
- Bind only metric keys, dimensions and window presets listed below; use `catalog_search` when unsure.
- Edits are rejected only if they introduce new validation errors; existing problems do not block other changes.
- If a tool returns `ok: false`, read its `code` and `path`, fix the call and retry once.
- Titles are plain text: never put markdown in a title. To make a section heading larger or smaller use `updateSection` with `level` (1 large, 2 default, 3 small); the dashboard title's size is fixed.
- For custom prose, notes or explanations add a `text` widget and write markdown in its `data.markdown`.
- To change a chart's kind within a type use `updateWidgetData` (e.g. trend `area`, bar `orientation`); to turn one widget into another type use `changeWidgetType`, which keeps bindings whose slots still exist; bind every required slot of the new type with `setBinding` in the same `dashboard_apply` call.
- A metric can be split `by: "day"` only if its catalog entry lists `day`; when it does not, say so and name metrics that do.

## Widgets

- `kpi` — KPI: Use for one headline number the reader should see first, such as revenue, signups or error rate. Bind the single `value` slot to one metric; set compare to "previous" on the binding so a change versus the previous period appears, and leave showDelta on. Use emphasis "large" for the single most important number on the dashboard. For two to four related numbers side by side prefer kpi-strip. (slots: value (value, required); default 3×1, min 2×1, max 6×2)
- `kpi-strip` — KPI strip: Use for a compact row of two to four related headline numbers, usually at the top of a section. Bind slots a and b (required) and optionally c and d, each to one metric; each tile is labelled with its metric name. Set compare to "previous" on the bindings to show changes. Use kpi instead when a single number deserves emphasis. (slots: a (value, required), b (value, required), c (value, optional), d (value, optional); default 12×1, min 8×1, max 12×1)
- `text` — Text: Use for static written context: a short explanation of a section, a caveat about the data, or a note for readers. Write the content in `markdown`; it has no data slots, so do not bind anything. For findings generated from the data use briefing instead. (slots: none; default 6×1, min 3×1, max 12×4)
- `briefing` — Briefing: Use at the top of a dashboard or section to say in plain language what the data shows. Write `parts` as the lead sentence (mark key figures with strong) and `findings` as two to five short bullets, each with a tone: positive for good news, negative for problems, warning for things to watch, neutral otherwise. Base every number on the bound data of the other widgets. Optionally set `prompt` to a follow-up question; when the host handles asks it becomes an "Ask about this" button. It has no data slots. (slots: none; default 6×2, min 4×2, max 12×4)
- `trend` — Trend: Use to show how one metric moves over time, such as daily revenue or weekly signups. Bind the `series` slot to a metric grouped by a time dimension (day, week or month) so labels are dates; ISO dates are shown as "Oct 5" on the axis. Set compare to "previous" on the binding and turn showPrevious on to draw the previous period as a dashed line for context. Turn area on for volume-like metrics where the filled shape helps; leave it off when comparing against the previous line. For category breakdowns use bar or donut instead. (slots: series (series, required); default 6×2, min 4×2, max 12×4)
- `bar` — Bar chart: Use to compare one metric across categories, such as revenue by plan, orders by country or tickets by priority. Bind the `series` slot to a metric grouped by a categorical dimension. Set sort to "desc" for rankings so the biggest category comes first, or keep "none" when the categories have a natural order (sizes, stages, weekdays). Use orientation "horizontal" when category names are long or there are more than about eight of them. For a share of a whole with few categories prefer donut; for change over time prefer trend. (slots: series (series, required); default 6×2, min 4×2, max 12×4)
- `donut` — Donut: Use to show how a total splits into parts, such as traffic by source or revenue by plan, when there are about two to six categories. Bind the `series` slot to an additive metric (counts, sums, revenue; never averages or rates) grouped by a categorical dimension; the centre shows the formatted total. Keep showLegend on unless the widget is very small. With more than six categories, or when exact comparison matters more than share, use bar instead. (slots: series (series, required); default 4×2, min 3×2, max 6×4)
- `comparison` — Comparison: Use to compare two series over the same categories, such as this quarter versus last quarter by region, or two segments by plan. Bind slots `a` and `b` to series with the same format grouped by the same dimension; categories are matched by label and a category missing from one side counts as 0. Set labelA and labelB to short names the reader recognises (for example "This month" and "Last month"); they appear in the legend and tooltip. For a single metric against its own previous period over time prefer trend with showPrevious. (slots: a (series, required), b (series, required); default 6×2, min 4×2, max 12×4)
- `funnel` — Funnel: Use for a sequence of steps where each one is a subset of the one before, such as visited, signed up, activated, paid. Bind the `series` slot to points in step order, first step first; each bar is drawn relative to the first step. Keep showConversion on so the percentage that made it from each step to the next appears between steps, which is usually the number the reader cares about. Do not use for categories without an order; use bar instead. (slots: series (series, required); default 6×2, min 4×2, max 12×4)
- `leaderboard` — Leaderboard: Use for a ranked list of one metric by a dimension, such as top customers by revenue, busiest endpoints or most active users. Bind the `rows` slot to a metric with `by` set to the dimension and `sort: "desc"` so the biggest item comes first (rows render in the order the data source returns them), and set `limit` on both the binding and the widget to the number of rows wanted. Each row shows the label, the formatted value and a bar scaled to the largest value. For a full multi-column listing prefer table; for a visual comparison of many categories prefer bar. (slots: rows (table, required); default 6×2, min 4×2, max 12×4)
- `table` — Table: Use when the reader needs exact values for many rows, such as a per-customer breakdown or a list of recent records. Bind the `rows` slot to a metric with `by` set to the dimension; the data source decides which columns exist. Leave `columns` empty to show every column, or list column keys in the order they should appear. Turn on dense for long lists. For a ranking of one metric prefer leaderboard; for a visual comparison prefer bar. (slots: rows (table, required); default 12×2, min 6×2, max 12×4)

## Metrics

- `meta_spend` — Meta Spend [currency, higher is better] by: client, day
- `meta_impressions` — Meta Impressions [integer, higher is better] by: client, day
- `meta_clicks` — Meta Clicks [integer, higher is better] by: client, day
- `meta_reach` — Meta Reach [integer, higher is better] by: client
- `meta_results` — Meta Results [integer, higher is better] by: client
- `meta_leads` — Meta Leads [integer, higher is better] by: client
- `meta_ctr` — Meta CTR [percentage, higher is better] by: client
- `meta_cpc` — Meta CPC [currency, lower is better] by: client
- `meta_cpm` — Meta CPM [currency, lower is better] by: client
- `spend` — Spend [currency, higher is better] by: client, day
- `impressions` — Impressions [integer, higher is better] by: client, day
- `clicks` — Clicks [integer, higher is better] by: client, day
- `reach` — Reach [integer, higher is better] by: client
- `results` — Results [integer, higher is better] by: client
- `leads` — Leads [integer, higher is better] by: client
- `ctr` — CTR [percentage, higher is better] by: client
- `cpc` — CPC [currency, lower is better] by: client
- `cpm` — CPM [currency, lower is better] by: client
- `cpl` — CPL [currency, lower is better] by: client
- `cost_per_result` — Cost Per Result [currency, lower is better] by: client
- `frequency` — Frequency [decimal, higher is better] by: client
- `ghl_won_opps` — GHL Won Opps [integer, higher is better] by: client
- `ghl_lost_opps` — GHL Lost Opps [integer, lower is better] by: client
- `ghl_open_opps` — GHL Open Opps [integer, higher is better] by: client
- `ghl_abandoned_opps` — GHL Abandoned Opps [integer, lower is better] by: client
- `ghl_total_opps` — GHL Total Opps [integer, higher is better] by: client
- `ghl_revenue` — GHL Revenue [currency, higher is better] by: client
- `ghl_conversion` — GHL Conversion [percentage, higher is better] by: client
- `ghl_contacts` — GHL Contacts [integer, higher is better] by: client, day
- `ghl_leads` — GHL Leads [integer, higher is better] by: client, day
- `total_tags` — Total Tags [integer, higher is better] by: client
- `hp_leads` — HP Leads [integer, higher is better] by: client
- `hp_total_calls` — HP Total Calls [integer, higher is better] by: client, day
- `hp_inbound` — HP Inbound [integer, higher is better] by: client
- `hp_outbound` — HP Outbound [integer, higher is better] by: client
- `hp_transfers` — HP Transfers [integer, higher is better] by: client
- `hp_leads_with_calls` — HP Leads With Calls [integer, higher is better] by: client
- `hp_answered_calls` — HP Answered Calls [integer, higher is better] by: client
- `hp_talk_time` — HP Talk Time [decimal, higher is better] by: client
- `hp_connect_rate` — HP Connect Rate [percentage, higher is better] by: client
- `hp_answer_rate` — HP Answer Rate [percentage, higher is better] by: client
- `conversion_rate` — Conversion Rate [percentage, higher is better] by: client
- `cost_per_lead` — Cost Per Lead [currency, lower is better] by: client
- `engagement_rate` — Engagement Rate [percentage, higher is better] by: client

## Dimensions

- `client` — Client
- `day` — Day

## Windows

- `maximum` — All time
- `today` — Today
- `yesterday` — Yesterday
- `this_week` — This week
- `last_7d` — Last 7 days
- `last_14d` — Last 14 days
- `last_30d` — Last 30 days
- `this_month` — This month
- `last_month` — Last month
- `this_quarter` — This quarter
- `last_quarter` — Last quarter
- `this_year` — This year
- `last_year` — Last year

## Layout

- The grid has 12 columns; a span is `{ cols, rows }` with cols 1–12 and rows 1–4.
- Typical widths: kpi 3 columns, charts 6, tables 12.
- Widgets flow left to right; keep each row's spans summing to at most 12.

## Examples

Add a widget (`dashboard_propose_widget` arguments):

```json
{
  "type": "kpi",
  "title": "Meta Spend",
  "bindings": {
    "value": {
      "metric": "meta_spend",
      "window": {
        "preset": "maximum"
      },
      "compare": "previous"
    }
  }
}
```

Move a widget to the top of another section and resize it (`dashboard_apply` arguments; ids come from `dashboard_describe`):

```json
{
  "ops": [
    {
      "op": "moveWidget",
      "widgetId": "<widget id>",
      "toSectionId": "<section id>",
      "toIndex": 0
    },
    {
      "op": "resizeWidget",
      "widgetId": "<widget id>",
      "span": {
        "cols": 6,
        "rows": 2
      }
    }
  ]
}
```