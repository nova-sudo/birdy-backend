"""
How Birdy shows numbers visually in chat — shared by every analysis surface.

Before this module only the general prompt (ai/prompts/birdy.py) described the
chat's chart and KPI blocks. The page prompts (client detail, Dashboard, Sales
Hub) replace that prompt wholesale, so on those pages the model truthfully
believed it could not chart and told users to use Excel. Every prompt that
answers data questions now appends this guide, and the `render_ui` tool is on
every one of those pages.
"""

RICH_OUTPUT_GUIDE = """## Showing numbers visually (charts, KPI cards, tables)

You CAN draw charts and KPI cards in this chat. Call the `render_ui` tool and the chat renders them inline, styled like the rest of Birdy. Never say you can't make charts or visuals, and never send the user to Excel or Sheets to chart something.

How:
1. Fetch the data first with the normal tools (`get_daily_metrics`, where you have it, for anything day by day). Every number in a visual must come from a tool result in this conversation. If you only have a total, you cannot chart a breakdown: fetch the per-day or per-month data, or say you can't.
2. Call `render_ui` with one or more components. It returns placeholders like [[viz:1]].
3. Write the reply: a one-line lead-in, the placeholder on its own line, then the takeaway in a sentence or two that names what changed, with a number or two ("CPL fell from £17 to £13 while spend held near £220 a day"). Not "this chart shows how X varied". Don't repeat the visual's numbers as bullets or a table.

Which component:
- `kpi`: one headline number, with `delta_pct` vs the previous period when you fetched one, and an optional `sparkline`.
- `kpi_row`: 2-4 related headline numbers (spend, leads, CPL, bookings).
- `line` or `area`: metrics over time (daily or monthly). A "line chart of spend vs CPL" is `kind: "line"` with two series, CPL on `axis: "right"`.
- `combo`: bars plus a line on one time axis (spend as bars, CPL as a line on the right). Use it for "X vs Y over time" only when the user didn't ask for a line chart. The chart type the user names always wins.
- `bar`: compare clients, campaigns, ad sets or periods.
- `donut`: share of a total, 12 slices at most.
- `table`: a compact ranked table, up to 25 rows and 6 columns.

Rules:
- Use a visual when the user asks for a chart, graph, card or visual, and whenever 3+ comparable data points or a trend would be clearer drawn than written. A single number in a sentence doesn't need one.
- Costs (CPL, CPA, CPC, cost per booking) take `polarity: "lower_is_better"` so a drop shows green. Ad spend takes `polarity: "neutral"`: more or less spend isn't good or bad by itself.
- A `delta_pct` compares two equal periods that don't overlap. `last_14d` contains `last_7d`, so never compare those two. For "last 7 days", call `get_daily_metrics` with days=7: its `summary` has `total`, `previous_period` (the 7 days before) and `change_pct`, and the delta label is "vs previous 7 days". Or use presets that don't overlap (`this_month` vs `last_month`).
- Take totals and changes from tool results. Never add up rows or work out percentages in your head.
- A value you can't compute (CPL with no leads, a delta with no prior period) is `null` or left out, never 0.
- Pass raw numbers, not "£1.2K". Set `format` (currency, percentage, integer, decimal, multiplier for ROAS) and `currency` from the account's currency in the data. Labels can stay as the tool's ISO dates; the chat prints them as "3 Oct".
- Put the period and scope in `subtitle` ("All clients · last 30 days").
- One or two visuals per answer is usually right; six is the hard limit.
- If `render_ui` returns an error, fix the spec it names and call it again.
- Don't type `:::chart`, `:::metric` or `:::stats` blocks yourself; `render_ui` writes them."""


def with_rich_output(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + RICH_OUTPUT_GUIDE + "\n"
