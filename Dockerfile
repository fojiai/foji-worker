# ─────────────────────────────────────────────────────────────────────────────
# foji-worker — AWS Lambda container image
#
# Each Lambda function uses the same image; the CMD is overridden per function
# in the Lambda configuration (or terraform/CDK):
#
#   file_extraction → app.handlers.file_extraction.handler
#   whatsapp        → app.handlers.whatsapp.handler
#   analytics       → app.handlers.analytics.handler
# ─────────────────────────────────────────────────────────────────────────────
FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

# Install OS-level dependencies needed by pdfplumber / psycopg2
RUN dnf install -y \
    gcc \
    libpq-devel \
    && dnf clean all

# Copy dependency manifest first (layer cache friendly)
COPY pyproject.toml .

# Install Python dependencies directly into Lambda's site-packages path
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        sqlalchemy \
        psycopg2-binary \
        pydantic-settings \
        boto3 \
        pdfplumber \
        python-docx \
        python-pptx \
        openpyxl \
        tiktoken \
        httpx \
        openai

# Copy application source
COPY app/ app/

# Default handler — overridden per Lambda function config
CMD ["app.handlers.file_extraction.handler"]
