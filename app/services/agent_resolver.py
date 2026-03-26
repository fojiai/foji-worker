"""
Resolve which Agent handles an inbound WhatsApp message.

Maps the recipient phone_number_id (Meta's identifier) to an active Agent
that has WhatsApp enabled and is configured for that number.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent import Agent

logger = logging.getLogger(__name__)


def resolve_agent_by_phone(db: Session, phone_number_id: str) -> Agent | None:
    """
    Look up an active, WhatsApp-enabled Agent by its Meta phone_number_id.

    Returns None if no matching agent is found (non-fatal — message is ignored).
    """
    stmt = (
        select(Agent)
        .where(
            Agent.whats_app_enabled == True,          # noqa: E712
            Agent.whats_app_phone_number_id == phone_number_id,
            Agent.is_active == True,                  # noqa: E712
        )
        .limit(1)
    )
    agent = db.scalars(stmt).first()

    if not agent:
        logger.warning("No active WhatsApp agent found for phone_number_id=%s", phone_number_id)

    return agent
