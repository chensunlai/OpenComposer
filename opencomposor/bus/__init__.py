"""Message bus module for decoupled channel-agent communication."""

from opencomposor.bus.events import InboundMessage, OutboundMessage
from opencomposor.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
