## Dashboard tools

- Change dashboards only through the tools `dashboard_describe`, `dashboard_apply`, `dashboard_propose_widget` and `catalog_search`.
- Emit ops, never code, pixel positions, CSS or HTML. Layout is a 12-column grid of 48px rows.
- When editing an existing dashboard, always call `dashboard_describe` first and use the ids it returns.
- Prefer `dashboard_propose_widget` for new widgets; use `dashboard_apply` to move, resize, retitle, change the query (`setQuery`, `clearQuery`) or remove.
- Each data widget has one `query`: `metrics` (ordered, each `{ metric, label? }`), an optional `by` dimension, an optional `over` grain (`day`, `week` or `month`) and a `window`, plus optional `filters`, `compare: "previous"`, `sort` and `limit`. Types missing from the query table below take no query.
- Use only metric keys, dimensions and window presets listed below; use `catalog_search` when unsure.
- Edits are rejected only if they introduce new validation errors; existing problems do not block other changes.
- If a tool returns `ok: false`, read its `code` and `path`, fix the call and retry once.
- Titles are plain text: never put markdown in a title. Sections are `heading` widgets whose title is the heading text; to make one larger or smaller use `updateWidgetData` with `level` (1 large, 2 default, 3 small); the dashboard title's size is fixed.
- For custom prose, notes or explanations add a `text` widget and write markdown in its `data.markdown`.
- To change a chart's kind within a type use `updateWidgetData` (e.g. trend `chart`, chart `orientation`); to turn one widget into another type use `changeWidgetType`, which keeps the query when it fits the new type and otherwise keeps only the metrics and window and sets `by` or `over` to the type's default; check the result against the table below.
- Trend is always over time; use chart for clients.
- Funnel steps and table columns are `query.metrics` in order, labelled via `label`.
- A metric can be split `by` a dimension only if its catalog entry lists it, and shown `over` time only if it is marked `over time`; when it is not, say so and name metrics that are.
- A query that breaks its type's rules fails with `QUERY_SHAPE`, and the message names the rule; fix the query to fit the table below.

## Widgets

- `heading` — Heading: Use to start a group of related widgets; headings replace sections. The widget title is the heading text and must be plain text. Usually full width (12 columns) and 1 row tall, or 2 rows with a description. Set `level` to 1 for a large page-level heading, 3 for a small one, or leave it out for the default size; add a short `description` to explain the group. Place the group's widgets after it with `after`. It has no data slots, so do not bind anything. (default 12×1, min 3×1, max 12×3)
- `kpi` — KPI: Query shape: exactly one metric, no `by`, no `over`; the sparkline is drawn automatically (`spark` is on by default; set `spark: false` in the query to turn it off). Use for one headline number the reader should see first, such as revenue, signups or error rate. Set `compare: "previous"` to show the change versus the previous period; without it the change badge reads "—". Optional data: `sparkStyle` "line" or "bar", `goal` (a target for the whole window; the card says whether the value is on track) and `description` (how the metric is counted, shown as a tooltip on the label). For two to four related numbers side by side prefer kpi-strip. (default 3×3, min 2×3, max 6×4)
- `kpi-strip` — KPI strip: Query shape: 2 to 4 metrics, no `by`, no `over`. Use for a compact row of related headline numbers, usually at the top of a section; each tile shows the metric label, its total and, with `compare: "previous"`, the change versus the previous period (coloured by the metric's polarity). No sparklines. Use kpi instead when a single number deserves emphasis. (default 12×3, min 8×3, max 12×4)
- `text` — Text: Use for static written context: a short explanation of a section, a caveat about the data, or a note for readers. Write the content in `markdown`; it has no data slots, so do not bind anything. For findings generated from the data use briefing instead. (default 6×3, min 3×2, max 12×12)
- `briefing` — Briefing: Use at the top of a dashboard or section for a plain-language summary of what the board shows. It has no query and no content to write: the host generates the summary from the numbers on the board, caches it for a few hours and shows an "Updated" time with a Refresh button. Optionally set `focus` to a short phrase such as "lead quality" or "ad spend" to steer what the summary concentrates on; leave it out for a general summary. (default 6×5, min 4×4, max 12×10)
- `trend` — Trend: Query shape: trend runs only over time, so `over` ("day", "week" or "month") is required (it defaults to "day"); 1 to 4 metrics, or exactly one metric with `by` set to a dimension such as client to draw one series per top client (5 by default, at most 8 via `limit`). The window sets how far back it goes. Use it to show how metrics move, such as daily revenue or weekly signups. `chart` picks the drawing: "area" (default) for volume-like metrics, "line" for rates and averages or several series, "bar" for a few discrete periods. Set `compare: "previous"` on the query and turn `showPrevious` on to draw the previous period as a dashed series of the same colour; this only works with one metric and no `by`. All metrics on one trend must share a format (all money, all counts or all percentages) because they share one value axis; with a catalog the query is rejected otherwise, so put a metric of another format in its own widget. To compare categories at one point in time (no `over`), use chart instead. (default 6×6, min 4×4, max 12×12)
- `chart` — Chart: Query shape: 1 to 4 metrics, `by` required (the dimension to compare, such as "client"), no `over`; rows are the dimension values and each metric is a series. Use it to compare categories at one point in time, such as spend by client, or for parts of a whole as a donut. `chart` picks the drawing: "bar" (default) for comparing amounts, "line" or "area" for ordered categories, "donut" for shares. A donut takes exactly one metric and it must be additive (counts, sums, money; never rates, ratios or averages, which the catalog marks "not additive"), otherwise the query is rejected: use bar for those. A donut shows its four largest categories and folds the rest into "Other". All metrics on one chart must share a format (all money, all counts or all percentages) because they share one value axis; with a catalog the query is rejected otherwise, so put a metric of another format in its own widget. `orientation` "horizontal" lays bars sideways, good for long names; `stacked` stacks bar and area series. Set `sort` and `limit` on the query to rank the categories. For change over time use trend instead. (default 6×6, min 4×4, max 12×12)
- `funnel` — Funnel: Query shape: 1 to 8 metrics, no `by`, no `over`. Each metric is one step of the funnel, in order, first step first, and its `label` names the step. Steps typically narrow, such as impressions → clicks → leads → appointments; a step larger than the one before still draws, but check the order. Each step shows its value, its share of the first step (`showPercentOfFirst`) and its share of the previous step (`showPercentOfPrevious`); both are on by default. One metric is valid but only shows "Add at least 2 steps", so use two or more. Do not use for categories without an order; use chart instead. (default 6×6, min 4×4, max 12×10)
- `leaderboard` — Leaderboard: Query shape: 1 to 3 metrics, `by` required (the dimension to rank), no `over`. Rows are ranked by metric 0, which draws the bar; metrics 1 and 2 are optional extra values shown after it (for example revenue as the bar, then orders and conversion rate). Set the ranking in the query: `sort: { metric: <metric 0>, dir: "desc" }` and `limit` for how many rows to show. Without a `sort` the widget ranks by metric 0, biggest first, and without a `limit` it shows 10 rows; with a `sort` the rows render in the order the data source returns them. With extra metrics or a change, a muted heading names each column. `showRank` numbers the rows from 1; `showChange`, with `compare: "previous"`, adds each row's change in metric 0 versus the previous period, coloured by that metric's polarity. For a full multi-column listing prefer table; for a visual comparison of many categories prefer chart. (default 6×6, min 3×4, max 12×12)
- `table` — Table: Query shape: 1 to 20 metrics, and exactly one of `by` or `over`. Each metric is a column, in query order, after a label column that stays in view while the table scrolls sideways. `by: "<dimension>"` gives one row per dimension value (use `sort` and `limit` in the query to order and cap them); `over: "day" | "week" | "month"` gives one row per time bucket. Use it when the reader needs exact values for many rows or many metrics at once, such as a per-client breakdown with spend, leads and cost per lead. `totals` adds a last row from the whole-window totals (ratios are recomputed, never summed), and `dense` tightens the rows for long lists. Readers can re-sort by clicking a column header. For a ranking of one metric prefer leaderboard; for a visual comparison prefer chart. (default 12×6, min 6×4, max 12×16)

## Query rules

| Type | Metrics | by | over | Notes |
|---|---|---|---|---|
| `kpi` | exactly 1 | no | no | sparkline by default |
| `kpi-strip` | 2..4 | no | no |  |
| `trend` | 1..4 | optional | required | exactly 1 metric when `by` is set; type change defaults `over` to day |
| `chart` | 1..4 | required | no | type change defaults `by` to client |
| `funnel` | 1..8 | no | no |  |
| `leaderboard` | 1..3 | required | no | type change defaults `by` to client |
| `table` | 1..20 | optional | optional | exactly one of `by` / `over`; type change defaults `by` to client |

## Metrics

- `meta_spend` — Meta Spend [currency, higher is better] by: client, day; over time
- `meta_impressions` — Meta Impressions [integer, higher is better] by: client, day; over time
- `meta_clicks` — Meta Clicks [integer, higher is better] by: client, day; over time
- `meta_reach` — Meta Reach [integer, higher is better] by: client; not additive
- `meta_results` — Meta Results [integer, higher is better] by: client, day; over time
- `meta_leads` — Meta Leads [integer, higher is better] by: client, day; over time
- `meta_ctr` — Meta CTR [percentage, higher is better] by: client; over time; not additive
- `meta_cpc` — Meta CPC [currency, lower is better] by: client; over time; not additive
- `meta_cpm` — Meta CPM [currency, lower is better] by: client; over time; not additive
- `spend` — Spend [currency, higher is better] by: client, day; over time
- `impressions` — Impressions [integer, higher is better] by: client, day; over time
- `clicks` — Clicks [integer, higher is better] by: client, day; over time
- `reach` — Reach [integer, higher is better] by: client; not additive
- `results` — Results [integer, higher is better] by: client, day; over time
- `leads` — Leads [integer, higher is better] by: client, day; over time
- `ctr` — CTR [percentage, higher is better] by: client; over time; not additive
- `cpc` — CPC [currency, lower is better] by: client; over time; not additive
- `cpm` — CPM [currency, lower is better] by: client; over time; not additive
- `cpl` — CPL [currency, lower is better] by: client; over time; not additive
- `cost_per_result` — Cost Per Result [currency, lower is better] by: client; over time; not additive
- `frequency` — Frequency [decimal, higher is better] by: client; not additive
- `ghl_won_opps` — GHL Won Opps [integer, higher is better] by: client, day; over time
- `ghl_lost_opps` — GHL Lost Opps [integer, lower is better] by: client, day; over time
- `ghl_open_opps` — GHL Open Opps [integer, higher is better] by: client, day; over time
- `ghl_abandoned_opps` — GHL Abandoned Opps [integer, lower is better] by: client, day; over time
- `ghl_total_opps` — GHL Total Opps [integer, higher is better] by: client, day; over time
- `ghl_revenue` — GHL Revenue [currency, higher is better] by: client, day; over time
- `ghl_conversion` — GHL Conversion [percentage, higher is better] by: client; over time; not additive
- `ghl_contacts` — GHL Contacts [integer, higher is better] by: client, day; over time
- `ghl_leads` — GHL Leads [integer, higher is better] by: client, day; over time
- `total_tags` — Total Tags [integer, higher is better] by: client; not additive
- `hp_leads` — HP Leads [integer, higher is better] by: client
- `hp_total_calls` — HP Total Calls [integer, higher is better] by: client, day; over time
- `hp_inbound` — HP Inbound [integer, higher is better] by: client, day; over time
- `hp_outbound` — HP Outbound [integer, higher is better] by: client, day; over time
- `hp_transfers` — HP Transfers [integer, higher is better] by: client, day; over time
- `hp_leads_with_calls` — HP Leads With Calls [integer, higher is better] by: client; not additive
- `hp_answered_calls` — HP Answered Calls [integer, higher is better] by: client, day; over time
- `hp_talk_time` — HP Talk Time [decimal, higher is better] by: client, day; over time
- `hp_connect_rate` — HP Connect Rate [percentage, higher is better] by: client; not additive
- `hp_answer_rate` — HP Answer Rate [percentage, higher is better] by: client; over time; not additive
- `conversion_rate` — Conversion Rate [percentage, higher is better] by: client; over time; not additive
- `cost_per_lead` — Cost Per Lead [currency, lower is better] by: client; over time; not additive
- `engagement_rate` — Engagement Rate [percentage, higher is better] by: client; over time; not additive

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

- The grid has 12 columns and rows 48px tall. A size is `{ w, h }` in cells (`w` 1–12 columns, `h` rows); a position is `{ x, y }` with `x` 0–11 and `y` counted from 0 at the top; `x + w` is at most 12.
- Sections are `heading` widgets (usually full width, 1 row). To add widgets under a heading, use `after` (in `dashboard_propose_widget` or `addWidget`). Only the first widget under a heading takes the heading id as `after`; give each later widget the previous widget's id as `after`, so it goes to that widget's right when it fits, else on the next row (naming the heading again would put it above the earlier ones). Without `after`, a new widget starts a new row at the bottom.
- To rearrange, use `moveWidget` (`to: { x, y }`) for one widget or `setLayout` (`items: [{ widgetId, x, y, w, h }]`) for several at once; resize with `resizeWidget` (`size: { w, h }`) within the type's min and max. Widgets in the way are pushed down and gaps close up automatically.
- Typical widths: kpi 3 columns, charts 6, tables 12.

## Examples

Add a widget under a heading (`dashboard_propose_widget` arguments; the heading id comes from `dashboard_describe`). Funnel steps are the metrics in order, each named by its `label`:

```json
{
  "type": "funnel",
  "title": "Funnel",
  "after": "<heading id>",
  "query": {
    "metrics": [
      {
        "metric": "meta_spend",
        "label": "Meta Spend"
      },
      {
        "metric": "meta_impressions",
        "label": "Meta Impressions"
      },
      {
        "metric": "meta_clicks",
        "label": "Meta Clicks"
      }
    ],
    "window": {
      "preset": "maximum"
    }
  }
}
```

Change what a widget shows (`dashboard_apply` arguments; `setQuery` replaces the whole query):

```json
{
  "ops": [
    {
      "op": "setQuery",
      "widgetId": "<widget id>",
      "query": {
        "metrics": [
          {
            "metric": "meta_spend"
          }
        ],
        "over": "day",
        "window": {
          "preset": "maximum"
        },
        "compare": "previous"
      }
    }
  ]
}
```

Move a widget to the top-left corner and resize it (`dashboard_apply` arguments; ids come from `dashboard_describe`):

```json
{
  "ops": [
    {
      "op": "moveWidget",
      "widgetId": "<widget id>",
      "to": {
        "x": 0,
        "y": 0
      }
    },
    {
      "op": "resizeWidget",
      "widgetId": "<widget id>",
      "size": {
        "w": 6,
        "h": 6
      }
    }
  ]
}
```