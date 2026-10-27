from urllib.parse import parse_qs, urlparse

from integrations.gohighlevel import GHLIntegration


def test_auth_url_requests_conversation_messages_scope():
    # GET /conversations/messages/export needs this scope; without it GHL answers 401.
    url = GHLIntegration("client", "secret", "https://example.test/cb").generate_auth_url()
    scopes = parse_qs(urlparse(url).query)["scope"][0].split(" ")
    assert "conversations/message.readonly" in scopes
    assert "conversations/message.write" in scopes
    assert "opportunities.readonly" in scopes
