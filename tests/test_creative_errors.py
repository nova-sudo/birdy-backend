import pytest

from integrations.facebook_utils.meta_creative_writer import MetaWriteError
from services.creatives.errors import explain


def E(code=None, subcode=None, user_msg=None, msg="boom"):
    return MetaWriteError(msg, code=code, subcode=subcode, user_msg=user_msg, step="create_ad")


@pytest.mark.parametrize("err,fix", [
    (E(100, user_msg="Lead form does not belong to this page"), "use_website_url"),
    (E(100, user_msg="The ad set is paused and cannot be used"), "pick_adset"),
    (E(100, user_msg="The ad set was archived"), "pick_adset"),
    (E(368, user_msg="Ad violates policy"), None),
    (E(100, subcode=1885183, user_msg="Policy block"), None),
    (E(4), "retry_later"), (E(17), "retry_later"), (E(613), "retry_later"),
    (E(190), "reconnect_meta"),
    (E(100, user_msg="Something else"), None),
    (E(100, user_msg="Image format not supported"), None),
    (E(100, user_msg="Invalid page_id"), None),
    (E(100, user_msg="Invalid parameter: lead form not found on this page"), "use_website_url"),
    (E(100, user_msg="Your video must be a thumbnail form for a page"), None),
])
def test_fix_mapping(err, fix):
    assert explain(err)["fix"] == fix


def test_policy_keeps_metas_message():
    assert explain(E(368, user_msg="Ad violates policy"))["message"] == "Ad violates policy"


def test_unknown_falls_back_to_raw_message():
    assert explain(E(msg="raw"))["message"] == "raw"
