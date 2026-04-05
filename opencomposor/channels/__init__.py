"""Chat channels module with plugin architecture."""

from opencomposor.channels.base import BaseChannel
from opencomposor.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
