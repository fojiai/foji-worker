from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class AgentFile(Base):
    __tablename__ = "AgentFiles"

    id: Mapped[int] = mapped_column("Id", Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column("AgentId", Integer, ForeignKey("Agents.Id"))
    file_name: Mapped[str] = mapped_column("FileName", String(500))
    file_size_bytes: Mapped[int] = mapped_column("FileSizeBytes", BigInteger)
    s3_key: Mapped[str] = mapped_column("S3Key", String(1000))
    content_type: Mapped[str] = mapped_column("ContentType", String(100))
    processing_status: Mapped[str] = mapped_column("ProcessingStatus", String(20))
    extraction_version: Mapped[int] = mapped_column("ExtractionVersion", Integer, default=0)
    extracted_at: Mapped[datetime | None] = mapped_column("ExtractedAt", DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column("ErrorMessage", String, nullable=True)
    s3_raw_text_key: Mapped[str | None] = mapped_column("S3RawTextKey", String(1000), nullable=True)
    s3_normalized_text_key: Mapped[str | None] = mapped_column("S3NormalizedTextKey", String(1000), nullable=True)
    s3_chunks_key: Mapped[str | None] = mapped_column("S3ChunksKey", String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column("CreatedAt", DateTime)
    updated_at: Mapped[datetime] = mapped_column("UpdatedAt", DateTime)
