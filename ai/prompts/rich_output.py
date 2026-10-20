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
1. Fetch the data first with the normal tools. Every number in a visual must come from a tool result in this conversation. If you only have a total, you cannot chart a breakdown: fetch the per-day or per-month data, or say you can't.
2. Call `render_ui` with one or more components. It returns placeholders like [[viz:1]].
3. Write the reply: a one-line lead-in, the placeholder on its own line, then the takeaway in a sentence or two. Don't repeat the visual's numbers as bullets or a table.

Which component:
- `kpi`: one headline number, with `delta_pct` vs the previous period when you fetched one, and an optional `sparkline`.
- `kpi_row`: 2-4 related headline numbers (spend, leads, CPL, bookings).
- `line` or `area`: one metric over time (daily or monthly).
- `combo`: two metrics on one time axis, e.g. ad spend as bars and CPL as a line on the right axis. Use this for "X vs Y over time".
- `bar`: compare clients, campaigns, ad sets or periods.
- `donut`: share of a total, 12 slices at most.
- `table`: a compact ranked table, up to 25 rows and 6 columns.

Rules:
- Use a visual when the user asks for a chart, graph, card or visual, and whenever 3+ comparable data points or a trend would be clearer drawn than written. A single number in a sentence doesn't need one.
- Costs (CPL, CPA, CPC, cost per booking) take `polarity: "lower_is_better"` so a drop shows green.
- Pass raw numbers, not "£1.2K". Set `format` (currency, percentage, integer, decimal, multiplier for ROAS) and `currency` from the account's currency in the data.
- Put the period and scope in `subtitle` ("All clients · last 30 days").
- One or two visuals per answer is usually right; six is the hard limit.
- If `render_ui` returns an error, fix the spec it names and call it again.
- Don't type `:::chart`, `:::metric` or `:::stats` blocks yourself; `render_ui` writes them."""


def with_rich_output(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + RICH_OUTPUT_GUIDE + "\n"
