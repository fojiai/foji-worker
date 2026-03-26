"""
Text chunker — target ~300–800 tokens per chunk with overlap.

Uses tiktoken for accurate token counting (cl100k_base, same as GPT-4).
Falls back to char/4 estimate if tiktoken is unavailable.
"""

import logging
import re

logger = logging.getLogger(__name__)

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(text: str) -> int:
        return len(_enc.encode(text))

except Exception:
    logger.warning("tiktoken unavailable — using char/4 token estimate")

    def _count_tokens(text: str) -> int:
        return len(text) // 4


def normalize(raw_text: str) -> str:
    """
    Light normalization:
    - Collapse 3+ newlines → 2
    - Strip trailing whitespace per line
    - Collapse runs of spaces
    """
    text = re.sub(r"\n{3,}", "\n\n", raw_text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def chunk(text: str, target_tokens: int = 512, max_tokens: int = 800, overlap_tokens: int = 50) -> list[dict]:
    """
    Split text into chunks of ~target_tokens tokens with overlap.

    Returns list of dicts:
      { "chunk_index": int, "text": str, "token_count": int }
    """
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[dict] = []
    current_parts: list[str] = []
    current_tokens = 0
    overlap_buffer: list[str] = []

    def flush():
        nonlocal current_parts, current_tokens
        if not current_parts:
            return
        chunk_text = "\n\n".join(current_parts)
        token_count = _count_tokens(chunk_text)
        chunks.append({
            "chunk_index": len(chunks),
            "text": chunk_text,
            "token_count": token_count,
        })
        # Build overlap from the tail of current chunk
        overlap_parts: list[str] = []
        overlap_tok = 0
        for part in reversed(current_parts):
            t = _count_tokens(part)
            if overlap_tok + t > overlap_tokens:
                break
            overlap_parts.insert(0, part)
            overlap_tok += t
        current_parts = overlap_parts
        current_tokens = overlap_tok

    for para in paragraphs:
        para_tokens = _count_tokens(para)

        # Paragraph itself exceeds max_tokens — hard-split by sentences
        if para_tokens > max_tokens:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sentence in sentences:
                s_tokens = _count_tokens(sentence)
                if current_tokens + s_tokens > max_tokens:
                    flush()
                current_parts.append(sentence)
                current_tokens += s_tokens
                if current_tokens >= target_tokens:
                    flush()
        else:
            if current_tokens + para_tokens > max_tokens:
                flush()
            current_parts.append(para)
            current_tokens += para_tokens
            if current_tokens >= target_tokens:
                flush()

    flush()
    return chunks
