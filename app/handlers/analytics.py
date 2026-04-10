"""
Lambda handler: analytics

Triggered nightly by EventBridge Scheduler (e.g. 00:05 UTC daily).
Aggregates yesterday's chat activity from DynamoDB into a daily_stats
summary stored in PostgreSQL for the admin dashboard.

EventBridge payload (ignored — we always process "yesterday"):
  {} or { "date": "2026-03-19" }   # optional override for backfill

Flow:
  1. Determine target date (yesterday UTC, or override from event)
  2. Scan DynamoDB for all sessions with messages on that date
     (GSI: date-index on date_partition key)
  3. Aggregate per company:
       - total_sessions
       - total_messages
       - total_input_tokens
       - total_output_tokens
       - estimated_cost_usd  (from AIModels rates)
  4. Upsert into daily_stats table (PostgreSQL)
  5. Log summary

DynamoDB item shape (written by foji-ai-api):
  PK: session_id  SK: timestamp  (ISO-8601)
  date_partition: "2026-03-19"   (for GSI)
  company_id: int
  agent_id: int
  role: "user" | "assistant"
  content: str
  provider: str
  input_tokens: int
  output_tokens: int
  model_id: str

DailyStats PostgreSQL table (managed by FojiApi migrations):
  id, company_id, stat_date, sessions, messages,
  input_tokens, output_tokens, estimated_cost_usd,
  created_at, updated_at
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """AWS Lambda entry point — EventBridge nightly trigger."""
    try:
        target_date = _resolve_date(event)
        logger.info("Running analytics aggregation for date=%s", target_date)

        records = _scan_dynamo(target_date)
        if not records:
            logger.info("No DynamoDB records found for date=%s — nothing to aggregate", target_date)
            return {"status": "ok", "date": str(target_date), "companies": 0}

        aggregated = _aggregate(records)
        _upsert_stats(aggregated, target_date)

        logger.info(
            "Analytics done for date=%s: %d companies, %d total sessions",
            target_date,
            len(aggregated),
            sum(v["sessions"] for v in aggregated.values()),
        )
        return {"status": "ok", "date": str(target_date), "companies": len(aggregated)}

    except Exception:
        logger.exception("Analytics aggregation failed")
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_date(event: dict) -> date:
    """Return the target date from the event payload or default to yesterday."""
    if raw := event.get("date"):
        return date.fromisoformat(raw)
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def _scan_dynamo(target_date: date) -> list[dict]:
    """
    Fetch all assistant messages for target_date from DynamoDB.

    We filter on role=assistant to avoid double-counting (each exchange
    produces one user + one assistant message; we count by assistant).
    We also only count assistant messages for token/cost aggregation since
    that's where provider, input_tokens, and output_tokens are set.
    """
    settings = get_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    table = dynamodb.Table(settings.aws_dynamodb_table)

    date_str = str(target_date)
    items: list[dict] = []
    kwargs = {
        "FilterExpression": (
            Attr("date_partition").eq(date_str) & Attr("role").eq("assistant")
        )
    }

    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    logger.debug("DynamoDB scan returned %d assistant items for %s", len(items), date_str)
    return items


def _aggregate(records: list[dict]) -> dict[int, dict]:
    """
    Aggregate DynamoDB records by company_id.

    Returns:
      {
        company_id: {
          sessions: set → int,
          messages: int,
          input_tokens: int,
          output_tokens: int,
        }
      }
    """
    session_sets: dict[int, set] = defaultdict(set)
    stats: dict[int, dict] = defaultdict(lambda: {
        "sessions": 0,
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    })

    for item in records:
        company_id = int(item.get("company_id", 0))
        if not company_id:
            continue

        session_id = item.get("session_id", "")
        session_sets[company_id].add(session_id)

        stats[company_id]["messages"] += 1
        stats[company_id]["input_tokens"] += int(item.get("input_tokens", 0))
        stats[company_id]["output_tokens"] += int(item.get("output_tokens", 0))

    for company_id in stats:
        stats[company_id]["sessions"] = len(session_sets[company_id])

    return dict(stats)


def _upsert_stats(aggregated: dict[int, dict], target_date: date) -> None:
    """Upsert daily_stats rows into PostgreSQL."""
    db: Session = get_session()
    try:
        now = datetime.now(timezone.utc)

        for company_id, data in aggregated.items():
            db.execute(
                text(
                    """
                    INSERT INTO daily_stats
                        (company_id, stat_date, sessions, messages,
                         input_tokens, output_tokens, created_at, updated_at)
                    VALUES
                        (:company_id, :stat_date, :sessions, :messages,
                         :input_tokens, :output_tokens, :now, :now)
                    ON CONFLICT (company_id, stat_date)
                    DO UPDATE SET
                        sessions      = EXCLUDED.sessions,
                        messages      = EXCLUDED.messages,
                        input_tokens  = EXCLUDED.input_tokens,
                        output_tokens = EXCLUDED.output_tokens,
                        updated_at    = EXCLUDED.updated_at
                    """
                ),
                {
                    "company_id": company_id,
                    "stat_date": target_date,
                    "sessions": data["sessions"],
                    "messages": data["messages"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                    "now": now,
                },
            )

        db.commit()
        logger.debug("Upserted stats for %d companies on %s", len(aggregated), target_date)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
