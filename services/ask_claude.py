"""'Ask Claude about this' links: open claude.ai with a prompt about a Birdy item."""
import urllib.parse


def ask_claude_url(prompt: str) -> str:
    return "https://claude.ai/new?q=" + urllib.parse.quote(prompt)


def alert_prompt(alert: dict) -> str:
    return f'Using Birdy, explain my alert "{alert["name"]}" (alert id {alert["id"]}) and tell me what to do about it.'


def alert_ask_claude_block(alert: dict) -> dict:
    """A Slack actions block with a link button (Slack's url limit is 3000 chars; the prompt is short)."""
    return {"type": "actions", "elements": [{
        "type": "button", "text": {"type": "plain_text", "text": "Ask Claude about this"},
        "url": ask_claude_url(alert_prompt(alert)),
    }]}
