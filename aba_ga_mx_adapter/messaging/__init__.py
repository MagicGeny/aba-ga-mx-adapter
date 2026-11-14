"""RabbitMQ messaging package."""

from .rabbitmq import GatewayStoppedError, RabbitMqGateway

__all__ = ["GatewayStoppedError", "RabbitMqGateway"]
