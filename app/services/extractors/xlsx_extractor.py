import openpyxl


def extract(file_path: str) -> str:
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet_texts: list[str] = []
    for sheet in wb.worksheets:
        rows: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            row_text = " | ".join(str(v) for v in row if v is not None and str(v).strip())
            if row_text:
                rows.append(row_text)
        if rows:
            sheet_texts.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(sheet_texts)
