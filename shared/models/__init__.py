"""SQLAlchemy ORM-модели, по одной на таблицу.

Схема зеркалит ``docs/04-data-model.md`` точно. Любое расхождение здесь с
документом — баг: правится код, не документ.
"""

from shared.models.admin_audit import AdminAudit
from shared.models.delivery import Delivery
from shared.models.inbound_sms import InboundSms
from shared.models.phone_number import PhoneNumber
from shared.models.service_state import ServiceState
from shared.models.team import Team
from shared.models.telegram_link import TelegramLink
from shared.models.user import (
    ALL_ROLES,
    ROLE_GROUP_LEADER,
    ROLE_GROUP_MEMBER,
    ROLE_SUPER_ADMIN,
    User,
)

__all__ = [
    "ALL_ROLES",
    "ROLE_GROUP_LEADER",
    "ROLE_GROUP_MEMBER",
    "ROLE_SUPER_ADMIN",
    "AdminAudit",
    "Delivery",
    "InboundSms",
    "PhoneNumber",
    "ServiceState",
    "Team",
    "TelegramLink",
    "User",
]
