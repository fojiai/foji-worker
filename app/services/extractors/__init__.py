"""Route by content type to the correct extractor."""

from app.services.extractors import docx_extractor, pdf_extractor, pptx_extractor, xlsx_extractor

_CONTENT_TYPE_MAP = {
    "application/pdf": pdf_extractor,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": docx_extractor,
    "application/msword": docx_extractor,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": pptx_extractor,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": xlsx_extractor,
    "application/vnd.ms-excel": xlsx_extractor,
}

_EXTENSION_MAP = {
    ".pdf": pdf_extractor,
    ".docx": docx_extractor,
    ".doc": docx_extractor,
    ".pptx": pptx_extractor,
    ".xlsx": xlsx_extractor,
    ".xls": xlsx_extractor,
}


def get_extractor(content_type: str, file_name: str):
    ext = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return (
        _CONTENT_TYPE_MAP.get(content_type.lower())
        or _EXTENSION_MAP.get(ext)
    )
