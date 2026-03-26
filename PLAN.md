# foji-worker — Plan

## Role in the Foji AI Ecosystem

`foji-worker` is the **background processing service** for Foji AI. It handles tasks that should not block the main request-response cycle: extracting text from uploaded files, relaying WhatsApp conversations to agents, and aggregating usage analytics. It runs as AWS Lambda functions triggered by SQS messages and EventBridge schedules.

---

## Tech Stack

- **Python 3.12**
- **AWS Lambda** — compute (pay-per-invocation, near-free in dev)
- **AWS SQS** — job queue (FojiApi publishes, Lambda consumes)
- **AWS EventBridge Scheduler** — cron for daily analytics job
- **SQLAlchemy 2.0** — ORM (reads/writes AgentFile, audit records)
- **boto3** — S3, SQS, Lambda runtime
- **pdfplumber** — PDF text extraction
- **python-docx** — DOCX extraction
- **python-pptx** — PPTX extraction
- **openpyxl** — XLSX extraction
- **openai / google-generativeai** — Summarization of large files
- **Meta Cloud API** (httpx) — WhatsApp send/receive

---

## Architecture

```
app/
├── handlers/
│   ├── file_extraction.py     # Lambda handler: extract text from AgentFile
│   ├── whatsapp.py            # Lambda handler: relay WhatsApp messages
│   └── analytics.py           # Lambda handler: nightly aggregation
├── services/
│   ├── extractors/
│   │   ├── pdf_extractor.py
│   │   ├── docx_extractor.py
│   │   ├── pptx_extractor.py
│   │   └── xlsx_extractor.py
│   ├── summarizer.py          # AI summarization for large docs (>50k chars)
│   ├── whatsapp_service.py    # Meta Cloud API client
│   └── agent_resolver.py     # Map WhatsApp phone → Agent
├── models/                    # SQLAlchemy ORM (mirrors FojiApi schema, read/write)
│   ├── agent.py
│   ├── agent_file.py
│   └── company.py
├── core/
│   ├── config.py              # Pydantic Settings
│   └── database.py            # SQLAlchemy engine (sync, Lambda-friendly)
└── utils/
    ├── s3.py                  # Download file from S3
    └── retry.py               # Retry decorator for AI calls
```

---

## Jobs

### 1. File Extraction (SQS-triggered)

**Trigger**: FojiApi publishes to SQS when a file is uploaded to S3.

**Message payload**:
```json
{ "job": "extract_file", "agent_file_id": 123 }
```

**Flow**:
1. Load `AgentFile` from DB (status = `pending`)
2. Set status → `processing`
3. Download file from S3 to `/tmp/`
4. Route by `ContentType`:
   - PDF → `PdfExtractor`
   - DOCX → `DocxExtractor`
   - PPTX → `PptxExtractor`
   - XLSX → `XlsxExtractor`
5. Store result in `AgentFile.extracted_text`, set `extracted_at`
6. If `len(extracted_text) > 50_000` chars → call summarizer → store `summarized_text`
7. Set status → `ready`
8. On failure: set status → `failed`, store error message

**Daily fallback** (EventBridge, 2 AM UTC): Scan all `AgentFile` where `status = pending` or `status = failed` for retry.

---

### 2. WhatsApp Worker (SQS-triggered, Scale plan only)

**Trigger**: Meta Cloud API webhook → FojiApi validates + forwards to SQS.

**Message payload**:
```json
{
  "job": "whatsapp_message",
  "from_number": "+5511999999999",
  "message": "Qual o prazo para declarar IR?",
  "wa_message_id": "wamid.abc"
}
```

**Flow**:
1. `AgentResolver.find_by_phone(from_number)` → look up `Agent` by `WhatsAppPhoneNumberId`
2. Verify agent's company has Scale plan (HasWhatsApp = true)
3. Call `foji-ai-api` `POST /api/v1/chat` (non-streaming for WhatsApp)
4. Send response via Meta Cloud API `POST /messages`

**Note**: WhatsApp does not support SSE — use non-streaming chat call to `foji-ai-api`.

---

### 3. Analytics Aggregation (EventBridge, daily midnight UTC)

**Flow**:
1. Query DynamoDB `foji-chats-*` table for previous day's sessions per company
2. Count: sessions, messages, unique agent usage
3. Write aggregated row to PostgreSQL `analytics_daily` table (future dashboard use)

---

## SQS Queue Names

| Queue | Purpose |
|-------|---------|
| `foji-file-extraction-dev` | File extraction jobs |
| `foji-file-extraction-prod` | — |
| `foji-whatsapp-dev` | WhatsApp relay |
| `foji-whatsapp-prod` | — |

FojiApi publishes; Lambda functions consume with SQS trigger.

---

## Environment Variables

```
DATABASE_URL                   # PostgreSQL connection string
AWS_REGION
AWS_S3_BUCKET                  # foji-files-dev or foji-files-prod
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
DYNAMODB_CHAT_TABLE            # foji-chats-dev or foji-chats-prod
OPENAI_API_KEY                 # For summarization
FOJI_AI_API_URL                # Internal URL of foji-ai-api service
INTERNAL_API_KEY               # Shared secret for foji-ai-api calls
META_WHATSAPP_TOKEN            # Meta Cloud API token
META_PHONE_NUMBER_ID           # Meta sender phone number ID
```

---

## Deploy Target

**AWS Lambda** (one function per handler):
- `foji-file-extraction` — SQS trigger, 512 MB memory, 5 min timeout
- `foji-whatsapp` — SQS trigger, 256 MB memory, 30s timeout
- `foji-analytics` — EventBridge trigger, 256 MB memory, 5 min timeout

Packaged as Docker images (Lambda container support) → pushed to ECR.

**Cost in dev**: Lambda free tier = 1M invocations/month. Near-zero cost.

**CI/CD** (GitHub Actions):
- `.github/workflows/deploy-dev.yml` — on push to `main`
- `.github/workflows/deploy-prod.yml` — `workflow_dispatch`

---

## Connections to Other Services

| Service | How |
|---------|-----|
| `FojiApi` | Receives SQS messages published by FojiApi on file upload |
| `foji-ai-api` | Calls chat endpoint (non-streaming) for WhatsApp responses |
| PostgreSQL | Reads/writes `AgentFile` status, `extracted_text`, analytics |
| AWS S3 | Downloads uploaded files for extraction |
| AWS DynamoDB | Reads chat data for analytics aggregation |
| Meta Cloud API | Sends WhatsApp response messages |
