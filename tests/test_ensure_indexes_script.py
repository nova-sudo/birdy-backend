"""`python -m scripts.ensure_indexes` is the deploy step, so its exit code has to tell the truth."""
import pytest

from ai.agents import claims, store
from core import indexes
from scripts import ensure_indexes as script


class _Client:
    """The in-memory client under the name the script opens, with the close() it calls."""

    def __init__(self, inner):
        self._inner = inner
        self.closed = False

    def __getitem__(self, name):
        return self._inner[name]

    def close(self):
        self.closed = True


@pytest.fixture
def run(mock_mongo_client, monkeypatch):
    client = _Client(mock_mongo_client)
    monkeypatch.setattr(script, "AsyncIOMotorClient", lambda uri: client)
    monkeypatch.setattr(indexes, "_load_creators", lambda: [store.create_agent_indexes])
    return client


async def test_a_clean_pass_exits_zero(run):
    await script.main()  # no SystemExit
    assert run.closed


@pytest.mark.parametrize("step", ["migrate_agent_types", "migrate_claims"])
async def test_a_failed_agent_migration_exits_non_zero(run, monkeypatch, step, caplog):
    async def boom(db):
        raise RuntimeError("the migration broke")

    monkeypatch.setattr(claims if step == "migrate_claims" else store, step, boom)
    with pytest.raises(SystemExit) as e:
        await script.main()
    assert e.value.code == 1
    assert "create_agent_indexes" in caplog.text and "the migration broke" in caplog.text
    assert run.closed


async def test_the_pass_itself_still_returns_failures_instead_of_raising(mock_mongo_client, monkeypatch):
    """The app's startup and the cron route keep their tolerance: they get a result, not an exception."""
    async def boom(db):
        raise RuntimeError("the migration broke")

    async def other(client):
        other.ran = True

    monkeypatch.setattr(claims, "migrate_claims", boom)
    monkeypatch.setattr(indexes, "_load_creators", lambda: [store.create_agent_indexes, other])
    result = await indexes.ensure_indexes(mock_mongo_client)
    assert result["ok"] == 1 and "the migration broke" in result["failed"]["create_agent_indexes"]
    assert other.ran
