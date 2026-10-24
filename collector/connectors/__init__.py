from .bluesky import BlueskyConnector
from .mastodon import MastodonConnector
from .official import MetaConnector, TikTokConnector, XConnector
from .reddit import RedditConnector
from .telegram_web import TelegramWebConnector
from .youtube import YouTubeConnector

ALL = {
    "bluesky": BlueskyConnector,
    "mastodon": MastodonConnector,
    "reddit": RedditConnector,
    "telegram": TelegramWebConnector,
    "youtube": YouTubeConnector,
    "x": XConnector,
    "meta": MetaConnector,
    "tiktok": TikTokConnector,
}
