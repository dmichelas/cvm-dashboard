"""Parses CVM's Art. 11 "Negociação de Valores Mobiliários" filings (PDFs).

Two closely-related forms share the same ENET-generated layout:

  * "Posição Individual - Cia, Controladas e Coligadas" -- the company
    buying back its own shares (the Recompras tab). No governance group.
  * "Posição Consolidada" -- administrators and related persons (the
    Insiders tab), filed once per governance group (Controlador, Conselho
    de Administração, Diretoria, Conselho Fiscal, Órgãos Técnicos). CVM's
    open data structures this half into the VLMO CSV, but that bulk export
    lags and periodically freezes, so we parse the live PDF instead when a
    recent month is incomplete.

Both are auto-generated from the same web form, so the table layout is
consistent: a "Saldo Inicial" position block, a "Movimentações no Mês"
table (one row per trade), and a "Saldo Final" block, repeated once per
reporting entity. parse_position_pdf handles both; the movement rows are
identical and the consolidated form adds a "Grupo e Pessoas Ligadas"
checkbox line naming the role, which we read so the Insiders tab keeps its
per-role breakdown.
"""
import io
import re

import pdfplumber

SHARE_ASSETS = {"Ações", "Units", "BDR Patrocinados"}
TRADE_RE = re.compile(r"(Compra|Venda)")
CHECKED_RE = re.compile(r"\(\s*[Xx]\s*\)")

# Order matters: "Conselho Fiscal" and "Conselho de Administração" both
# contain "Conselho", and "Diretoria" is often OCR-split as "Direto ria".
# Each entry is (short key, keyword that uniquely identifies the label).
ROLE_MARKERS = [
    ("conselho_fiscal", "fisc"),
    ("conselho_administracao", "administ"),
    ("controlador", "controlador"),
    ("diretoria", "diret"),
    ("orgaos_tecnicos", "tecnic"),
]


def _clean(cell):
    return (cell or "").replace("\n", " ").strip()


def _br_num(s):
    s = _clean(s)
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _detect_role(row_text: str):
    """The governance group whose checkbox is marked on a 'Grupo e Pessoas
    Ligadas' line, as a short role key, or None if this isn't that line.

    Works off the marked '( X )' position: the label immediately after the
    checked box names the group. Falls back to a plain keyword scan when
    the checkbox and label land in different table cells.
    """
    lowered = row_text.lower()
    if "grupo e pessoas" not in lowered and not CHECKED_RE.search(row_text):
        return None
    # Prefer the label that follows the checked box.
    checked = CHECKED_RE.search(row_text)
    if checked:
        after = lowered[checked.end():checked.end() + 40]
        for key, kw in ROLE_MARKERS:
            if kw in after:
                return key
    return None


def parse_position_pdf(pdf_bytes: bytes, month: str, with_role: bool = False) -> list[dict]:
    """Returns trade records shaped like the insider records elsewhere in
    this pipeline (asset/movement/is_trade/qty/price/volume/ref, plus role
    when with_role), so the same monthly-aggregation and charting code can
    consume both buyback and insider filings.
    """
    records = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_role = None
            for table in page.extract_tables():
                for row in table:
                    row_text = " ".join(_clean(c) for c in row if c is not None)
                    if with_role:
                        found = _detect_role(row_text)
                        if found:
                            page_role = found
                    asset = _clean(row[0])
                    if asset not in SHARE_ASSETS:
                        continue
                    m = TRADE_RE.search(row_text)
                    if not m:
                        continue
                    nums = [_br_num(c) for c in row if _br_num(c) is not None]
                    if len(nums) < 3:
                        continue
                    qty, price, volume = nums[-3], nums[-2], nums[-1]
                    rec = {
                        "ref": month,
                        "asset": asset,
                        "movement": f"{m.group(1)} à vista",
                        "is_trade": True,
                        "qty": qty,
                        "price": price,
                        "volume": volume,
                    }
                    if with_role:
                        rec["role"] = page_role
                    records.append(rec)
    return records


# Backwards-compatible alias: the buyback path calls this name.
def parse_buyback_pdf(pdf_bytes: bytes, month: str) -> list[dict]:
    return parse_position_pdf(pdf_bytes, month, with_role=False)
