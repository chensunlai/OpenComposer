"""Chat channels module with plugin architecture."""

from opencomposer.channels.base import BaseChannel
from opencomposer.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
