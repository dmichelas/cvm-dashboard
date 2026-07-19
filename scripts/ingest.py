#!/usr/bin/env python3
"""Downloads CVM open data (VLMO + FCA) and builds static JSON for the dashboard.

Sources (CVM Open Data Portal, updated weekly by CVM):
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/vlmo_cia_aberta_con_{year}.zip
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip

No third-party dependencies -- stdlib only, so this runs in CI with no pip install.
"""
import csv
import datetime
import io
import json
import pathlib
import re
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data"
BY_COMPANY_DIR = OUT_DIR / "by_company"

CURRENT_YEAR = datetime.date.today().year
YEARS = [CURRENT_YEAR, CURRENT_YEAR - 1]

VLMO_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/vlmo_cia_aberta_{year}.zip"
FCA_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip"

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
    """cnpj -> {name, buybacks: [...], insiders: [...], monthly: {...}}"""
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
            cnpj = row["CNPJ_Companhia"].strip()
            company = companies.setdefault(
                cnpj,
                {"name": row["Nome_Companhia"].strip(), "buybacks": [], "insiders": [], "monthly": {}},
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
            }
            cargo = row["Tipo_Cargo"].strip()
            if cargo:
                record["role"] = cargo
                company["insiders"].append(record)
            else:
                company["buybacks"].append(record)

            if cargo and is_trade and asset in SHARE_ASSETS:
                month = record["ref"][:7]
                qty = record["qty"] or 0.0
                vol = record["volume"] or 0.0
                sign = 1.0 if movimentacao.startswith("Compra") else -1.0
                bucket = company["monthly"].setdefault(month, {"qty": 0.0, "val": 0.0})
                bucket["qty"] += sign * qty
                bucket["val"] += sign * vol
    return companies


def _num(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def main():
    tickers = load_tickers()
    companies = load_transactions()

    BY_COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    monthly_rows = []
    months_seen = set()
    for cnpj, data in companies.items():
        company_tickers = tickers.get(cnpj, [])
        cnpj_digits = "".join(ch for ch in cnpj if ch.isdigit())
        index.append({
            "cnpj": cnpj,
            "cnpj_digits": cnpj_digits,
            "name": data["name"],
            "tickers": company_tickers,
            "buyback_count": len(data["buybacks"]),
            "insider_count": len(data["insiders"]),
        })
        with open(BY_COMPANY_DIR / f"{cnpj_digits}.json", "w", encoding="utf-8") as f:
            json.dump({
                "cnpj": cnpj,
                "name": data["name"],
                "tickers": company_tickers,
                "buybacks": data["buybacks"],
                "insiders": data["insiders"],
            }, f, ensure_ascii=False, separators=(",", ":"))

        if not company_tickers:
            continue  # not usable in ranking tables without a ticker to display
        for month, agg in data["monthly"].items():
            months_seen.add(month)
            qty, val = agg["qty"], agg["val"]
            if not qty:
                continue
            monthly_rows.append({
                "cnpj_digits": cnpj_digits,
                "name": data["name"],
                "tickers": company_tickers,
                "month": month,
                "qty": qty,
                "val": val,
                "price": abs(val / qty),
            })

    index.sort(key=lambda c: c["name"])
    with open(OUT_DIR / "companies.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    monthly_rows.sort(key=lambda r: r["month"])
    with open(OUT_DIR / "monthly.json", "w", encoding="utf-8") as f:
        json.dump(monthly_rows, f, ensure_ascii=False, separators=(",", ":"))

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

    print(f"Wrote {len(index)} companies and {len(monthly_rows)} monthly rows to {OUT_DIR}")


if __name__ == "__main__":
    main()
