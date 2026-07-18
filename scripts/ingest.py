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
            code = row.get("Codigo_Negociacao", "").strip()
            if not code or row.get("Mercado", "").strip() != "Bolsa":
                continue
            if row.get("Data_Fim_Negociacao", "").strip():
                continue  # no longer listed under this code
            cnpj = row["CNPJ_Companhia"].strip()
            tickers.setdefault(cnpj, set()).add(code)
    return {cnpj: sorted(codes) for cnpj, codes in tickers.items()}


def load_transactions() -> dict[str, dict]:
    """cnpj -> {name, buybacks: [...], insiders: [...]}"""
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
                cnpj, {"name": row["Nome_Companhia"].strip(), "buybacks": [], "insiders": []}
            )
            movimentacao = row["Tipo_Movimentacao"].strip()
            record = {
                "ref": row["Data_Referencia"].strip(),
                "entity_type": row["Tipo_Empresa"].strip(),
                "entity_name": row["Empresa"].strip(),
                "asset": row["Tipo_Ativo"].strip(),
                "movement": movimentacao,
                "is_trade": movimentacao in TRADE_MOVEMENTS,
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

    index.sort(key=lambda c: c["name"])
    with open(OUT_DIR / "companies.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    with open(OUT_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "years": YEARS,
            "company_count": len(index),
        }, f)

    print(f"Wrote {len(index)} companies to {OUT_DIR}")


if __name__ == "__main__":
    main()
