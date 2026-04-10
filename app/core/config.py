import os
from functools import lru_cache

import boto3
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_ssm_parameters() -> None:
    """If AWS_SSM_PREFIX is set, fetch all params under that prefix and inject as env vars.
    Same concept as FojiApi's AddSystemsManager — SSM becomes the config source in deployed envs.
    Locally, AWS_SSM_PREFIX is not set, so .env / env vars are used instead."""
    prefix = os.environ.get("AWS_SSM_PREFIX")
    if not prefix:
        return

    region = os.environ.get("AWS_REGION", "us-east-1")
    ssm = boto3.client("ssm", region_name=region)

    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, WithDecryption=True):
        for param in page["Parameters"]:
            # /foji/dev/worker/DATABASE_URL → DATABASE_URL
            env_name = param["Name"].removeprefix(prefix)
            os.environ.setdefault(env_name, param["Value"])


# Load SSM before pydantic reads env vars
_load_ssm_parameters()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    aws_region: str = "us-east-1"
    aws_s3_bucket: str = Field(default="", validation_alias=AliasChoices("aws_s3_bucket", "aws_s3_bucket_name"))
    aws_dynamodb_table: str = "foji-chats-dev"
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
