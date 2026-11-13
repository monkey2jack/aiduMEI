"""Integration regressions: concurrent recall and credential-bearing webhook URLs."""
import base64

import pytest

from ducky import search_cache, write_policy


def _key():
    return search_cache.make_key(user_id="cache-test", bank_id="default", caller="cache-test",
                                 query="current setting", limit=5, before="", after="", session_id="")


def test_a_write_during_first_search_cannot_repopulate_a_current_cache(monkeypatch):
    monkeypatch.setenv("AIDUMEI_SEARCH_CACHE_TTL_S", "20")
    search_cache.clear()
    try:
        old_key = _key()
        # The first read is still in flight, so there are no cached entries yet.
        search_cache.invalidate_for_http("POST", "/add", "cache-test")
        search_cache.put(old_key, "cache-test", {"status": "ok", "recall_verdict": "found",
                                                "results": [{"memory": "old setting"}]})
        assert search_cache.get(_key(), "cache-test") is None
    finally:
        search_cache.clear()


@pytest.mark.parametrize("host", ["open.feishu.cn", "open.larksuite.com"])
def test_bot_webhook_credential_is_redacted_in_text_and_metadata(host):
    # Synthetic webhook fixture, never a real credential. release-scan:allow synthetic fixture
    url = "https://" + host + "/open-apis/bot/v2/hook/" + "11111111-2222-4333-8444-555555555555"
    text, kinds = write_policy.redact_forbidden("callback: " + url)
    assert url not in text and "webhook" in kinds
    assert url not in str(write_policy.redact_metadata({"callback": url}))


def test_encoded_webhook_is_also_redacted():
    # Synthetic webhook fixture, never a real credential. release-scan:allow synthetic fixture
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + "11111111-2222-4333-8444-555555555555"
    encoded = base64.b64encode(url.encode()).decode()
    text, kinds = write_policy.redact_forbidden(encoded)
    assert encoded not in text and kinds


def test_ordinary_document_url_is_unchanged():
    text = "See https://example.feishu.cn/docx/synthetic-document"
    assert write_policy.redact_forbidden(text) == (text, [])
