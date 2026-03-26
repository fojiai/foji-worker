from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Agent(Base):
    __tablename__ = "Agents"

    id: Mapped[int] = mapped_column("Id", Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column("CompanyId", Integer, ForeignKey("Companies.Id"))
    name: Mapped[str] = mapped_column("Name", String(200))
    is_active: Mapped[bool] = mapped_column("IsActive", Boolean)
    whats_app_enabled: Mapped[bool] = mapped_column("WhatsAppEnabled", Boolean, default=False)
    whats_app_phone_number_id: Mapped[str | None] = mapped_column("WhatsAppPhoneNumberId", String, nullable=True)
    agent_token: Mapped[str] = mapped_column("AgentToken", String(64))
    created_at: Mapped[datetime] = mapped_column("CreatedAt", DateTime)
    updated_at: Mapped[datetime] = mapped_column("UpdatedAt", DateTime)
