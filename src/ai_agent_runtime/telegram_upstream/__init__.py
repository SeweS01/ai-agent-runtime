"""Version-qualified adapters for an unchanged Telegram Agent checkout."""

from .adapter import TelegramUpstreamAdapter
from .topic_config import TopicConfigEntry, TopicConfigSnapshot

__all__ = ["TelegramUpstreamAdapter", "TopicConfigEntry", "TopicConfigSnapshot"]
