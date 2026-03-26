from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    aws_region: str = "us-east-1"
    aws_s3_bucket: str
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_dynamodb_table: str = "foji-chats-dev"
    openai_api_key: str = ""
    foji_ai_api_url: str = "http://localhost:8000"
    internal_api_key: str = ""
    meta_whatsapp_token: str = ""
    meta_phone_number_id: str = ""

    # Chunking
    chunk_target_tokens: int = 512
    chunk_max_tokens: int = 800
    chunk_overlap_tokens: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()
