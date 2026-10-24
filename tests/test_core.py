import datetime as dt
import json
from pathlib import Path

from collector.config import Settings
from collector.connectors.bluesky import BlueskyConnector
from collector.connectors.mastodon import MastodonConnector, strip_html
from collector.connectors.telegram_web import TelegramWebConnector
from collector.llm import parse_json
from collector.pseudo import Pseudonymizer

FIX = Path(__file__).parent / "fixtures"
P = Pseudonymizer("test-secret")


def settings():
    s = Settings()
    s.date_from = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    s.date_to = dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc)
    s.mastodon_instances = ["mastodon.social"]
    return s


def test_pseudo_stable_and_case_insensitive():
    assert P.uid("bluesky", "did:plc:ABC") == P.uid("bluesky", "did:plc:abc")
    assert P.uid("bluesky", "x") != P.uid("mastodon", "x")
    assert len(P.uid("reddit", "someone")) == 24
    assert Pseudonymizer("other").uid("reddit", "someone") != P.uid("reddit", "someone")


def test_rewrite_mentions_removes_handles():
    out = P.rewrite_mentions("bluesky", "hey @bob.bsky.social and @eve@mastodon.social!", {"bob.bsky.social": "did:plc:bob"})
    assert "bob" not in out and "eve" not in out
    assert f"@user_{P.uid('bluesky', 'did:plc:bob')}" in out
    assert "mail@example.org" in P.rewrite_mentions("x", "write to mail@example.org")


def test_bluesky_normalisation():
    c = BlueskyConnector(settings(), P)
    feed = json.loads((FIX / "bsky_feed.json").read_text())["feed"]
    b = c.post_to_batch(feed[0]["post"], "timeline")
    post = b.posts[0]
    assert post.author_uid == P.uid("bluesky", "did:plc:alice")
    assert "bob" not in post.text and "@user_" in post.text
    assert post.hashtags == ["ai"] and post.urls == ["https://ex.org"]
    assert post.media_type == "image" and post.media_alt_text == "a chart"
    assert post.mentions_uid == [P.uid("bluesky", "did:plc:bob")]
    assert post.root_post_uid == post.post_uid
    assert any(e.type == "mention" for e in b.edges)
    assert b.discovered_roots  # has replies

    r = c.post_to_batch(feed[1]["post"], "timeline", feed[1]["reason"])
    rp = r.posts[0]
    assert rp.parent_post_uid == post.post_uid and rp.root_post_uid == post.post_uid
    types = {e.type for e in r.edges}
    assert {"reply", "repost"} <= types


def test_mastodon_normalisation_and_optout():
    c = MastodonConnector(settings(), P)
    data = json.loads((FIX / "mastodon_status.json").read_text())
    b = c.status_to_batch(data[0], "mastodon.social", "search")
    p = b.posts[0]
    assert p.author_uid == P.uid("mastodon", "dan@fosstodon.org")
    assert "eve" not in p.text and "#Climate" in p.text
    assert p.mentions_uid == [P.uid("mastodon", "eve@mastodon.social")]
    assert p.hashtags == ["climate"] and p.media_alt_text == "glacier"
    assert c.status_to_batch(data[1], "mastodon.social", "search").posts == []  # indexable=false respected
    assert strip_html("<p>a<br>b &amp; c</p>") == "a\nb & c"


def test_telegram_parse():
    html = """<div class="tgme_channel_info_description">News channel</div>
    <div class="tgme_widget_message" data-post="chan/42"><div class="tgme_widget_message_text">Hello #World https://a.b</div>
    <time datetime="2024-06-01T10:00:00+00:00"></time></div>"""
    c = TelegramWebConnector(settings(), P)
    b, oldest = c.parse_page(html, "chan")
    assert oldest == 42 and b.posts[0].hashtags == ["world"]
    assert b.posts[0].author_uid == P.uid("telegram", "chan")


def test_llm_json_parsing():
    assert parse_json('<think>hmm</think>{"items": ["a", "b"]}') == {"items": ["a", "b"]}
    assert parse_json('```json\n{"x": 1}\n```') == {"x": 1}
    assert parse_json('Sure! here: {"scores": {"0": 0.9}} done') == {"scores": {"0": 0.9}}
    assert parse_json("no json") is None


def test_llm_roles_with_fake_ollama(monkeypatch):
    from collector.llm import LocalLLM

    calls = []

    class FakeResp:
        def __init__(self, content):
            self._c = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": self._c}}

    llm = LocalLLM("http://ollama:11434", "qwen3:14b")

    def fake_post(url, json):
        calls.append((url, json))
        sys_prompt = json["messages"][0]["content"]
        if "strict annotator" in sys_prompt:
            return FakeResp('{"scores": {"0": 0.9, "1": "0.1"}}')
        return FakeResp('<think>x</think>{"items": ["#climate", "r/energy", "climate", "x"]}')

    monkeypatch.setattr(llm.http, "post", fake_post)
    assert llm.propose_queries("climate", "d", ["k"], "mastodon") == ["climate", "energy"]
    assert calls[0][0].endswith("/api/chat") and calls[0][1]["format"] == "json" and calls[0][1]["think"] is False
    assert llm.score_accounts("climate", "d", [{"id": "0"}, {"id": "1"}]) == {"0": 0.9, "1": 0.1}
