from .bluesky import BlueskyConnector
from .lemmy import LemmyConnector
from .stackexchange import StackExchangeConnector
from .mastodon import MastodonConnector
from .official import MetaConnector, TikTokConnector, XConnector
from .reddit import RedditConnector
from .telegram_api import TelegramApiConnector
from .telegram_web import TelegramWebConnector
from .youtube import YouTubeConnector



def TelegramConnector(settings, pseudo):
    """Official API when TELEGRAM_API_ID/HASH are set and a session exists, else the web preview."""
    if settings.telegram_api_id and settings.telegram_api_hash:
        api = TelegramApiConnector(settings, pseudo)
        ok, why = api.enabled()
        if ok:
            return api
        import logging
        logging.getLogger(__name__).warning("[telegram] API not used (%s) — falling back to t.me/s web preview", why)
    return TelegramWebConnector(settings, pseudo)


ALL = {
    "bluesky": BlueskyConnector,
    "mastodon": MastodonConnector,
    "lemmy": LemmyConnector,
    "stackexchange": StackExchangeConnector,
    "reddit": RedditConnector,
    "telegram": TelegramConnector,
    "youtube": YouTubeConnector,
    "x": XConnector,
    "meta": MetaConnector,
    "tiktok": TikTokConnector,
}
