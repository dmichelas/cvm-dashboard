#!/usr/bin/env python3
"""Downloads CVM open data (VLMO + FCA + IPE) and builds static JSON for the dashboard.

Sources (CVM Open Data Portal, updated weekly by CVM):
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/vlmo_cia_aberta_con_{year}.zip
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip

Company buybacks ("Negociação de Valores Mobiliários pela própria companhia,
suas controladas e coligadas") are filed under the same Art. 11 rule as
insider disclosures, but CVM's own structured VLMO dataset only extracts
the insider half (Tipo="Posição Consolidada") into CSV -- the buyback half
(Tipo="Posição Individual - Cia, Controladas e Coligadas") only exists as a
PDF, linked from the general IPE filing index. See parse_buyback_pdf.py.

Needs pdfplumber (see requirements.txt) -- everything else is stdlib.
"""
import concurrent.futures
import csv
import datetime
import io
import json
import pathlib
import re
import time
import urllib.request
import zipfile

from parse_buyback_pdf import parse_buyback_pdf

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data"
BY_COMPANY_DIR = OUT_DIR / "by_company"

CURRENT_YEAR = datetime.date.today().year
YEARS = [CURRENT_YEAR, CURRENT_YEAR - 1]

VLMO_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/vlmo_cia_aberta_{year}.zip"
FCA_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip"
IPE_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip"
FRE_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/DADOS/fre_cia_aberta_{year}.zip"
FRE_YEARS = [CURRENT_YEAR, CURRENT_YEAR - 1, CURRENT_YEAR - 2]
BUYBACK_TIPO = "Posição Individual - Cia, Controladas e Coligadas"

TRADE_MOVEMENTS = {
    "Compra", "Compra à vista", "Compra à termo",
    "Venda", "Venda à vista", "Venda à termo",
}

SHARE_ASSETS = {"Ações", "Units", "BDR Patrocinados"}

# Real B3 tickers start with a letter, followed by 3 more alphanumeric
# characters (some, like B3SA3, embed a digit in the root) and 1-2 trailing
# digits for the share type (PETR4, TAEE11, B3SA3). CVM's FCA data
# occasionally has a data-entry error in Codigo_Negociacao (e.g. "ADR"
# typed in place of the real code, or stray junk like "0000") -- reject
# anything that doesn't fit this shape.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{3}\d{1,2}$")

# Confirmed CVM filing errors where Codigo_Negociacao is consistently wrong
# across every year on record (verified manually) -- corrected here since
# the regex filter above would otherwise drop the company entirely.
TICKER_OVERRIDES = {
    "03.853.896/0001-40": ["MRFG3"],  # Marfrig Global Foods -- CVM has "ADR" on file
}


def fetch_zip(url: str) -> zipfile.ZipFile:
    with urllib.request.urlopen(url) as resp:
        return zipfile.ZipFile(io.BytesIO(resp.read()))


def read_csv_member(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="latin-1", newline="")
        yield from csv.DictReader(text, delimiter=";")


def fetch_url(url: str, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def accumulate_monthly(bucket_dict: dict, month: str, qty: float, vol: float, is_buy: bool):
    """Gross (unsigned) sums are kept alongside the signed net so Preço Médio
    can be computed as total value / total quantity -- see the comment in
    build_monthly_rows for why the naive net/net ratio can blow up."""
    sign = 1.0 if is_buy else -1.0
    bucket = bucket_dict.setdefault(month, {"qty": 0.0, "val": 0.0, "gross_qty": 0.0, "gross_val": 0.0})
    bucket["qty"] += sign * qty
    bucket["val"] += sign * vol
    bucket["gross_qty"] += qty
    bucket["gross_val"] += vol


def load_buyback_filings(years: list[int], known_cnpjs: set[str]) -> tuple[dict, dict]:
    """cnpj -> [(month, pdf_url), ...] for the buyback-specific filing, plus
    cnpj -> company name (needed for companies that have buyback filings but
    no insider ones, so main() has a name to write even for those).

    Restricted to known_cnpjs (companies we already track via VLMO/FCA) to
    avoid spending requests on the long tail of unlisted/inactive filers.
    """
    filings: dict[str, list[tuple[str, str]]] = {}
    names: dict[str, str] = {}
    for year in years:
        url = IPE_URL.format(year=year)
        print(f"Downloading {url}")
        try:
            zf = fetch_zip(url)
        except Exception as e:
            print(f"  skip {year}: {e}")
            continue
        member = f"ipe_cia_aberta_{year}.csv"
        if member not in zf.namelist():
            continue
        for row in read_csv_member(zf, member):
            if row.get("Tipo", "").strip() != BUYBACK_TIPO:
                continue
            cnpj = row["CNPJ_Companhia"].strip()
            if cnpj not in known_cnpjs:
                continue
            names[cnpj] = row["Nome_Companhia"].strip()
            month = row["Data_Referencia"].strip()[:7]
            link = row.get("Link_Download", "").strip()
            if link:
                filings.setdefault(cnpj, []).append((month, link))
    return filings, names


def fetch_and_parse_buyback(args) -> tuple[str, str, list[dict]]:
    cnpj, month, url = args
    try:
        pdf_bytes = fetch_url(url)
        records = parse_buyback_pdf(pdf_bytes, month)
        return cnpj, month, records
    except Exception as e:
        print(f"  buyback fetch failed for {cnpj} {month}: {e}")
        return cnpj, month, []


def load_buybacks(years: list[int], known_cnpjs: set[str]) -> dict[str, dict]:
    """cnpj -> {"name": ..., "records": [...], "monthly": {...}}, parsed from PDFs."""
    filings, names = load_buyback_filings(years, known_cnpjs)
    tasks = [(cnpj, month, url) for cnpj, entries in filings.items() for month, url in entries]
    print(f"Fetching {len(tasks)} buyback filings...")

    result: dict[str, dict] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for cnpj, month, records in pool.map(fetch_and_parse_buyback, tasks):
            done += 1
            if done % 250 == 0:
                print(f"  ...{done}/{len(tasks)}")
            company = result.setdefault(cnpj, {"name": names.get(cnpj, ""), "records": [], "monthly": {}})
            if not records:
                continue
            company["records"].extend(records)
            for r in records:
                if r["asset"] in SHARE_ASSETS:
                    accumulate_monthly(company["monthly"], month, r["qty"], r["volume"], r["movement"].startswith("Compra"))
    print(f"Parsed buyback activity for {len(result)} companies")
    return result


def load_total_shares() -> dict[str, float]:
    """cnpj -> best-known total share count, from FRE capital tables.

    Used for % do Capital on buybacks (insiders don't get this figure --
    see the app.js note on why it's not meaningful there). Coverage is
    incomplete (not every company refiles every year, and a few, like
    Petrobras, are absent from every year checked) -- callers must treat a
    missing cnpj as unknown, not zero, and show "--" rather than 0%.
    """
    issued: dict[str, tuple[str, float]] = {}      # cnpj -> (data_referencia, shares), from capital_social
    circulating: dict[str, tuple[str, float]] = {}  # cnpj -> (data_referencia, shares), from distribuicao_capital

    def consider(store: dict, cnpj: str, ref: str, shares_str: str):
        shares = _num(shares_str)
        if not shares or shares <= 0:
            return
        prev = store.get(cnpj)
        if prev is None or ref > prev[0]:
            store[cnpj] = (ref, shares)

    for year in FRE_YEARS:
        url = FRE_URL.format(year=year)
        print(f"Downloading {url}")
        try:
            zf = fetch_zip(url)
        except Exception as e:
            print(f"  skip {year}: {e}")
            continue
        member = f"fre_cia_aberta_capital_social_{year}.csv"
        if member in zf.namelist():
            for row in read_csv_member(zf, member):
                if row.get("Tipo_Capital", "").strip() != "Capital Emitido":
                    continue
                consider(issued, row["CNPJ_Companhia"].strip(), row["Data_Referencia"].strip(), row.get("Quantidade_Total_Acoes", ""))
        member = f"fre_cia_aberta_distribuicao_capital_{year}.csv"
        if member in zf.namelist():
            for row in read_csv_member(zf, member):
                consider(circulating, row["CNPJ_Companhia"].strip(), row["Data_Referencia"].strip(), row.get("Quantidade_Total_Acoes_Circulacao", ""))

    # Prefer total shares issued (capital_social); fall back to shares in
    # circulation for companies that only reported the latter.
    result = {cnpj: shares for cnpj, (ref, shares) in circulating.items()}
    result.update({cnpj: shares for cnpj, (ref, shares) in issued.items()})
    return result


def load_tickers() -> dict[str, list[str]]:
    """cnpj -> sorted list of currently-listed B3 tickers, from FCA valor_mobiliario table."""
    tickers: dict[str, set[str]] = {}
    for year in YEARS:
        url = FCA_URL.format(year=year)
        print(f"Downloading {url}")
        try:
            zf = fetch_zip(url)
        except Exception as e:
            print(f"  skip {year}: {e}")
            continue
        member = f"fca_cia_aberta_valor_mobiliario_{year}.csv"
        if member not in zf.namelist():
            continue
        for row in read_csv_member(zf, member):
            code = row.get("Codigo_Negociacao", "").strip().upper()
            if not code or not TICKER_RE.match(code) or row.get("Mercado", "").strip() != "Bolsa":
                continue
            if row.get("Data_Fim_Negociacao", "").strip():
                continue  # no longer listed under this code
            cnpj = row["CNPJ_Companhia"].strip()
            tickers.setdefault(cnpj, set()).add(code)
    for cnpj, codes in TICKER_OVERRIDES.items():
        tickers.setdefault(cnpj, set()).update(codes)
    return {cnpj: sorted(codes) for cnpj, codes in tickers.items()}


def load_transactions() -> dict[str, dict]:
    """cnpj -> {name, insiders: [...], monthly: {...}}

    Note: this dataset's "Tipo_Cargo blank" rows (nominally the company's own
    trades) are almost never populated with real trades -- CVM's structured
    extraction of that sub-section is unreliable (verified: 5 real trade rows
    across all ~500 companies for a full year). Real buyback data comes from
    load_buybacks() instead, which parses the actual filed PDFs.
    """
    companies: dict[str, dict] = {}
    for year in YEARS:
        url = VLMO_URL.format(year=year)
        print(f"Downloading {url}")
        try:
            zf = fetch_zip(url)
        except Exception as e:
            print(f"  skip {year}: {e}")
            continue
        member = f"vlmo_cia_aberta_con_{year}.csv"
        if member not in zf.namelist():
            continue
        for row in read_csv_member(zf, member):
            cargo = row["Tipo_Cargo"].strip()
            if not cargo:
                continue  # not an insider row -- see load_buybacks() instead
            cnpj = row["CNPJ_Companhia"].strip()
            company = companies.setdefault(
                cnpj, {"name": row["Nome_Companhia"].strip(), "insiders": [], "monthly": {}}
            )
            movimentacao = row["Tipo_Movimentacao"].strip()
            is_trade = movimentacao in TRADE_MOVEMENTS
            asset = row["Tipo_Ativo"].strip()
            record = {
                "ref": row["Data_Referencia"].strip(),
                "entity_type": row["Tipo_Empresa"].strip(),
                "entity_name": row["Empresa"].strip(),
                "asset": asset,
                "movement": movimentacao,
                "is_trade": is_trade,
                "op": row["Tipo_Operacao"].strip(),
                "date": row["Data_Movimentacao"].strip(),
                "qty": _num(row["Quantidade"]),
                "price": _num(row["Preco_Unitario"]),
                "volume": _num(row["Volume"]),
                "role": cargo,
            }
            company["insiders"].append(record)

            if is_trade and asset in SHARE_ASSETS:
                month = record["ref"][:7]
                accumulate_monthly(company["monthly"], month, record["qty"] or 0.0, record["volume"] or 0.0, movimentacao.startswith("Compra"))
    return companies


def _num(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def monthly_dict_to_rows(monthly: dict, cnpj_digits: str, name: str, company_tickers: list[str], months_seen: set, total_shares=None):
    rows = []
    for month, agg in monthly.items():
        months_seen.add(month)
        qty, val = agg["qty"], agg["val"]
        gross_qty, gross_val = agg["gross_qty"], agg["gross_val"]
        if not qty:
            continue
        row = {
            "cnpj_digits": cnpj_digits,
            "name": name,
            "tickers": company_tickers,
            "month": month,
            "qty": qty,
            "val": val,
            "gross_qty": gross_qty,
            "gross_val": gross_val,
            "price": gross_val / gross_qty if gross_qty else 0,
        }
        if total_shares is not None:
            shares = total_shares.get(cnpj_digits)
            row["pct"] = abs(qty) / shares * 100 if shares else None
        rows.append(row)
    return rows


def main():
    tickers = load_tickers()
    companies = load_transactions()
    buybacks = load_buybacks(YEARS, known_cnpjs=set(tickers.keys()))
    total_shares_by_cnpj = load_total_shares()
    total_shares = {
        "".join(ch for ch in cnpj if ch.isdigit()): shares
        for cnpj, shares in total_shares_by_cnpj.items()
    }

    BY_COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    monthly_rows = []
    bb_monthly_rows = []
    months_seen = set()

    all_cnpjs = set(companies.keys()) | set(buybacks.keys())
    for cnpj in all_cnpjs:
        bb = buybacks.get(cnpj, {"records": [], "monthly": {}})
        data = companies.get(cnpj) or {"name": bb.get("name", ""), "insiders": [], "monthly": {}}
        company_tickers = tickers.get(cnpj, [])
        cnpj_digits = "".join(ch for ch in cnpj if ch.isdigit())
        name = data["name"]
        index.append({
            "cnpj": cnpj,
            "cnpj_digits": cnpj_digits,
            "name": name,
            "tickers": company_tickers,
            "buyback_count": len(bb["records"]),
            "insider_count": len(data["insiders"]),
        })
        with open(BY_COMPANY_DIR / f"{cnpj_digits}.json", "w", encoding="utf-8") as f:
            json.dump({
                "cnpj": cnpj,
                "name": name,
                "tickers": company_tickers,
                "buybacks": bb["records"],
                "insiders": data["insiders"],
            }, f, ensure_ascii=False, separators=(",", ":"))

        if not company_tickers:
            continue  # not usable in ranking tables without a ticker to display
        monthly_rows.extend(monthly_dict_to_rows(data["monthly"], cnpj_digits, name, company_tickers, months_seen))
        bb_monthly_rows.extend(monthly_dict_to_rows(bb["monthly"], cnpj_digits, name, company_tickers, months_seen, total_shares=total_shares))

    index.sort(key=lambda c: c["name"])
    with open(OUT_DIR / "companies.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    monthly_rows.sort(key=lambda r: r["month"])
    with open(OUT_DIR / "monthly.json", "w", encoding="utf-8") as f:
        json.dump(monthly_rows, f, ensure_ascii=False, separators=(",", ":"))

    bb_monthly_rows.sort(key=lambda r: r["month"])
    with open(OUT_DIR / "bb_monthly.json", "w", encoding="utf-8") as f:
        json.dump(bb_monthly_rows, f, ensure_ascii=False, separators=(",", ":"))

    today = datetime.date.today()
    last_complete_month = (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")

    with open(OUT_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "years": YEARS,
            "company_count": len(index),
            "last_complete_month": last_complete_month,
            "available_months": sorted(months_seen),
        }, f)

    print(f"Wrote {len(index)} companies, {len(monthly_rows)} insider monthly rows, "
          f"{len(bb_monthly_rows)} buyback monthly rows to {OUT_DIR}")


if __name__ == "__main__":
    main()
