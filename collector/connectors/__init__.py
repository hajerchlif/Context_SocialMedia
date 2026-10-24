from .bluesky import BlueskyConnector
from .lemmy import LemmyConnector
from .stackexchange import StackExchangeConnector
from .mastodon import MastodonConnector
from .official import MetaConnector, TikTokConnector, XConnector
from .reddit import RedditConnector
from .telegram_web import TelegramWebConnector
from .youtube import YouTubeConnector

ALL = {
    "bluesky": BlueskyConnector,
    "mastodon": MastodonConnector,
    "lemmy": LemmyConnector,
    "stackexchange": StackExchangeConnector,
    "reddit": RedditConnector,
    "telegram": TelegramWebConnector,
    "youtube": YouTubeConnector,
    "x": XConnector,
    "meta": MetaConnector,
    "tiktok": TikTokConnector,
}
