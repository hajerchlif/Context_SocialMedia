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


def test_lemmy_normalisation():
    from collector.connectors.lemmy import LemmyConnector, acct_of
    c = LemmyConnector(settings(), P)
    creator = {"id": 1, "name": "ann", "actor_id": "https://lemmy.ml/u/ann", "bot_account": False}
    post = {"post": {"id": 10, "name": "Heat waves", "body": "thoughts @bob@lemmy.world?", "ap_id": "https://lemmy.ml/post/10",
                     "published": "2024-07-01T10:00:00Z", "url": None},
            "creator": creator, "community": {"name": "climate", "actor_id": "https://lemmy.ml/c/climate"},
            "counts": {"score": 5, "comments": 2}}
    b = c.post_to_batch(post, "search")
    p = b.posts[0]
    assert acct_of(creator) == "ann@lemmy.ml" and p.community == "climate@lemmy.ml"
    assert "bob" not in p.text and b.discovered_roots
    cv = {"comment": {"id": 7, "ap_id": "https://x.y/comment/7", "path": "0.5.7", "content": "agree",
                      "published": "2024-07-02T10:00:00Z"},
          "creator": {"name": "cy", "actor_id": "https://x.y/u/cy"}, "post": post["post"]}
    cb = c.comment_to_batch(cv, "thread", {5: ("https://lemmy.ml/comment/5", P.uid("lemmy", "https://lemmy.ml/u/ann"))})
    assert cb.posts[0].parent_post_uid == P.uid("lemmy", "https://lemmy.ml/comment/5")
    assert cb.edges[0].type == "reply" and cb.edges[0].dst_uid == p.author_uid


def test_stackexchange_normalisation():
    from collector.connectors.stackexchange import StackExchangeConnector
    c = StackExchangeConnector(settings(), P)
    q = {"question_id": 3, "title": "Why?", "body": "<p>Because &amp; so</p>", "tags": ["Voting"],
         "creation_date": 1700000000, "answer_count": 1, "owner": {"account_id": 42, "user_id": 9}}
    b = c._question(q, "politics", "search")
    assert b.posts[0].text == "Why?\n\nBecause & so" and b.posts[0].hashtags == ["voting"]
    assert b.posts[0].author_uid == P.uid("stackexchange", "acct:42") and b.discovered_users[0][0] == "politics|9"
    a = {"answer_id": 4, "body": "<p>ok</p>", "creation_date": 1700000100, "owner": {"account_id": 43, "user_id": 8}}
    ab = c._answer(a, "politics", b.posts[0].post_uid, b.posts[0].author_uid, "thread")
    assert ab.posts[0].parent_post_uid == b.posts[0].post_uid and ab.edges[0].type == "reply"
    assert c._site_tag("politics:elections", "tag") == [("politics", "elections")]


def test_month_windows_and_cap():
    from collector.balance import cap_batch, month_windows, root_months
    from collector.models import Batch, PostRec
    w = month_windows(dt.datetime(2023, 11, 15, tzinfo=dt.timezone.utc), dt.datetime(2024, 2, 3, tzinfo=dt.timezone.utc))
    assert [m for m, _, _ in w] == ["2023-11", "2023-12", "2024-01", "2024-02"]
    assert w[0][1].day == 15 and w[-1][2].day == 3
    def post(i, author, y, m, root=None):
        return PostRec(f"p{i}", "x", author, "t", dt.datetime(y, m, 1, tzinfo=dt.timezone.utc), root_post_uid=root or f"p{i}")
    b = Batch(posts=[post(i, "a", 2024, 1) for i in range(5)] + [post(9, "a", 2024, 2), post(10, "b", 2024, 1)],
              discovered_roots=[(f"r{i}", f"p{i}") for i in range(5)])
    c = cap_batch(b, 2, by_author=True)
    assert len(c.posts) == 4  # 2 of a/2024-01, 1 of a/2024-02, 1 of b/2024-01
    assert len(c.discovered_roots) == 2
    assert len(cap_batch(b, 2).posts) == 3  # per month only
    rb = Batch(posts=[post(1, "a", 2024, 3), post(2, "b", 2024, 5, root="p0"), post(3, "c", 2024, 4, root="p0")])
    assert root_months(rb) == {"p1": "2024-03", "p0": "2024-04"}


def test_mastodon_snowflake():
    from collector.connectors.mastodon import MastodonConnector
    ts = dt.datetime(2023, 6, 1, tzinfo=dt.timezone.utc)
    sid = int(MastodonConnector.snowflake(ts))
    assert (sid >> 16) / 1000 == ts.timestamp()
