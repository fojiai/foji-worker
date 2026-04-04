# foji-worker

Background processing for the Foji platform. Handles file text extraction, WhatsApp message relay, and nightly analytics aggregation.

## Tech

- Python 3.12
- AWS Lambda (3 functions)
- pdfplumber, python-docx, openpyxl for file extraction
- tiktoken for text chunking

## Lambda Functions

| Function | Trigger | Purpose |
|----------|---------|---------|
| file-extraction | SQS | Extract text from PDF/DOCX/PPTX/XLSX, chunk, upload to S3 |
| whatsapp | SQS | Resolve agent, call AI API, reply via Meta Cloud API |
| analytics | EventBridge (daily) | Scan DynamoDB, aggregate stats, upsert to PostgreSQL |

## Environment

Config is loaded from AWS SSM Parameter Store by prefix (`AWS_SSM_PREFIX`). For local dev, use a `.env` file.

## Deploy

- **Dev**: Push to `main` triggers deploy via GitHub Actions to Lambda.
- **Prod**: Manual `workflow_dispatch` with confirmation.
