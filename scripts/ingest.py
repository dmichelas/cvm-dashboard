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
import collections
import concurrent.futures
import csv
import datetime
import io
import json
import os
import pathlib
import re
import socket
import statistics
import time
import urllib.request
import zipfile

from parse_buyback_pdf import parse_buyback_pdf, parse_position_pdf

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
INSIDER_TIPO = "Posição Consolidada"

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
    return zipfile.ZipFile(io.BytesIO(fetch_url(url)))


def read_csv_member(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="latin-1", newline="")
        yield from csv.DictReader(text, delimiter=";")


# dados.cvm.gov.br publishes an AAAA record (2804:3e68:170::66) alongside
# its A record, but GitHub's runners have no working IPv6 route -- so any
# attempt over v6 dies instantly with "Network is unreachable". That is the
# error that killed every fetch in the 2026-08-10 run, and it hit only
# dados.cvm.gov.br; rad.cvm.gov.br, which has no AAAA record, was never
# implicated. Reproduced locally: curl -6 to that host fails in 4ms while
# curl -4 returns 200.
#
# Ordinarily socket.create_connection walks every address getaddrinfo
# returns and falls back from v6 to v4, which would have saved the run --
# so the resolver there most likely returned the v6 address alone. Sorting
# v4 first wouldn't help in that case; only refusing to ask for v6 does.
# Every CVM host this script touches resolves over IPv4, so pin lookups to
# AF_INET rather than leave the run at the mercy of the runner's resolver.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


socket.getaddrinfo = _ipv4_only_getaddrinfo


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


def fetch_json_post(url: str, payload: dict, retries: int = 3) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Referer": "https://www.rad.cvm.gov.br/ENETWeb/frmConsultaExternaCVM.aspx",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


# The 5 insider role categories CVM's VLMO form reports (Tipo_Cargo), mapped
# to short keys used in the JSON output. "orgaos_tecnicos" is what CVM's own
# PDF form calls "Órgãos Técnicos ou Consultivos" -- the CSV's Tipo_Cargo
# spells the same category "Órgão Estatutário ou Vinculado".
ROLE_KEYS = {
    "Controlador ou Vinculado": "controlador",
    "Conselho de Administração ou Vinculado": "conselho_administracao",
    "Diretor ou Vinculado": "diretoria",
    "Conselho Fiscal ou Vinculado": "conselho_fiscal",
    "Órgão Estatutário ou Vinculado": "orgaos_tecnicos",
}

# Insider records parsed from the live PDF carry the short role key that
# parse_position_pdf reads off the form's checkbox; VLMO's structured rows
# carry the full Tipo_Cargo label. compute_monthly's by_role step matches
# on the full label, so live records are mapped back to it before merging.
ROLE_KEY_TO_LABEL = {v: k for k, v in ROLE_KEYS.items()}


def _aggregate_records(records: list[dict]) -> dict:
    """Sums signed qty/value plus gross (unsigned) qty/value for Preço Médio
    (see monthly_dict_to_rows for why net/net can blow up), rejecting price
    outliers first: CVM's source filings occasionally have a fat-finger
    data-entry error (verified case: one entity's rows for a single month
    priced some trades at R$1,242/share against a R$12-14 range the same
    week -- the Volume field was internally consistent with that bad price,
    confirming it's a source error, not a parsing bug). A row priced more
    than 5x off the group's median is dropped; a stock's price realistically
    never moves that much within one month, so this only catches genuine
    data-entry errors.
    """
    priced = [r["price"] for r in records if r.get("price")]
    med = statistics.median(priced) if priced else None
    good = [r for r in records if not r.get("price") or med is None or 0.2 <= r["price"] / med <= 5]

    qty = val = gross_qty = gross_val = 0.0
    for r in good:
        q, v = r.get("qty") or 0.0, r.get("volume") or 0.0
        sign = 1.0 if r["movement"].startswith("Compra") else -1.0
        qty += sign * q
        val += sign * v
        gross_qty += q
        gross_val += v
    return {"qty": qty, "val": val, "gross_qty": gross_qty, "gross_val": gross_val}


def compute_monthly(records: list[dict], by_role: bool = False) -> dict:
    """Groups a company's trade records by month and aggregates qty/value.

    If by_role, each month's entry also gets a "by_role" dict (see ROLE_KEYS)
    with the same aggregate computed separately per insider role category --
    a company can be net-flat overall in a month while a specific role was
    clearly buying or selling, and that's exactly the case this is for.
    """
    by_month: dict[str, list[dict]] = {}
    for r in records:
        if not r.get("is_trade") or r.get("asset") not in SHARE_ASSETS:
            continue
        by_month.setdefault(r["ref"][:7], []).append(r)

    result = {}
    for month, recs in by_month.items():
        agg = _aggregate_records(recs)
        roles = {}
        if by_role:
            for role_name, role_key in ROLE_KEYS.items():
                role_recs = [r for r in recs if r.get("role") == role_name]
                if not role_recs:
                    continue
                role_agg = _aggregate_records(role_recs)
                if role_agg["qty"]:
                    roles[role_key] = role_agg
        if agg["qty"] or roles:
            if roles:
                agg["by_role"] = roles
            result[month] = agg
    return result


# CVM's bulk IPE zip for a given year only appears once CVM has published
# it -- there can be a lag of a few months into a new year where the bulk
# export for that year doesn't exist at all yet (confirmed: as of Jul 2026,
# ipe_cia_aberta_2026.zip 404s, while sibling datasets VLMO/FCA already have
# their 2026 files). LIVE_QUERY_URL is the JSON webmethod behind CVM's own
# "Consulta de Documentos de Companhias Abertas" search UI
# (rad.cvm.gov.br/ENETWeb/frmConsultaExternaCVM.aspx) -- undocumented, but
# it serves the same IPE filings individually by protocol number ahead of
# the bulk export, so it's used as a fallback to fill that gap. Verified
# manually against Petrobras: returns the same filings (Nov/2025-Jun/2026)
# that Fundamentus.com.br's own buyback tab links to.
LIVE_QUERY_URL = "https://www.rad.cvm.gov.br/ENETWeb/frmConsultaExternaCVM.aspx/ListarDocumentos"
LIVE_DOWNLOAD_URL = "https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&numProtocolo={protocolo}&descTipo=IPE&CodigoInstituicao=1"
LIVE_PROTOCOLO_RE = re.compile(r"OpenDownloadDocumentos\('\d+','\d+','(\d+)','IPE'\)")
LIVE_REF_DATE_RE = re.compile(r"<spanOrder>(\d{8})</spanOrder>")


def iter_live_market_index(data_de: str, data_ate: str):
    """Yield (code, name, tipo, ref_month, proto) for every Art. 11 "Valores
    Mobiliários Negociados e Detidos" filing *delivered* in [data_de,
    data_ate] (dd/mm/yyyy), across the whole market.

    This is CVM's live ENET document search -- the same source Fundamentus
    reads -- which serves each filing the moment it's received, unlike the
    bulk VLMO/IPE exports that trail delivery by ~2 days and periodically
    freeze for a week or more. A market-wide query (empresa empty) returns
    every company's filings of both position types in a single request, and
    unlike the per-company form has no same-calendar-year restriction.
    `code` is CVM's numeric company code (int); `tipo` is BUYBACK_TIPO or
    INSIDER_TIPO.
    """
    payload = {
        "dataDe": data_de, "dataAte": data_ate, "empresa": "",
        "setorAtividade": "-1", "categoriaEmissor": "-1", "situacaoEmissor": "-1",
        "tipoParticipante": "-1", "dataReferencia": "", "categoria": "IPE_-1_-1_-1",
        "periodo": "2", "horaIni": "", "horaFim": "", "palavraChave": "",
        "ultimaDtRef": "false", "tipoEmpresa": "0", "token": "", "versaoCaptcha": "",
    }
    body = fetch_json_post(LIVE_QUERY_URL, payload)
    d = body.get("d") or {}
    if d.get("temErro") or not d.get("dados"):
        return
    for row in d["dados"].split("&*"):
        f = row.split("$&")
        if len(f) < 11:
            continue
        tipo = f[3].strip()
        if tipo not in (BUYBACK_TIPO, INSIDER_TIPO):
            continue
        ref = LIVE_REF_DATE_RE.search(f[5])
        proto = LIVE_PROTOCOLO_RE.search(f[10])
        code = re.sub(r"\D", "", f[0])
        if not ref or not proto or not code:
            continue
        ref_month = f"{ref.group(1)[:4]}-{ref.group(1)[4:6]}"
        yield int(code), f[1].strip(), tipo, ref_month, proto.group(1)


def _recent_months(today: datetime.date, count: int) -> list[str]:
    """The `count` reference months ending with the one before `today`."""
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        months.append(f"{year}-{month:02d}")
    return months


def months_to_refresh(counts: dict[str, int], missing_years: list[int], today: datetime.date,
                      window_floor: str, lookback: int = 12, fraction: float = 0.8) -> set[str]:
    """Reference months whose bulk data should be replaced from live search.

    Two failure modes, both seen in practice:
      * a year's bulk zip 404s entirely (July 2026: ipe_cia_aberta_2026.zip
        did not exist yet) -- every month of that year needs live data;
      * the zip exists but has stopped refreshing (Aug 2026: every export
        frozen at Last-Modified 2026-08-09, so July sat at ~30% of a normal
        month's filings) -- recent months fall below their historical norm.
    Bounded to months at or after window_floor (the displayed range) so a
    stale older year doesn't trigger a full re-parse.
    """
    wanted = set()
    for year in missing_years:
        wanted |= {m for m in _recent_months(today, 24)
                   if m.startswith(str(year)) and m >= window_floor}
    months = sorted(counts)
    for month in _recent_months(today, 4):
        if month < window_floor:
            continue
        baseline = [counts[m] for m in months if m < month][-lookback:]
        if len(baseline) < 3:
            wanted.add(month)  # no settled history yet -- treat as needing fill
            continue
        median = statistics.median(baseline)
        if median == 0 or counts.get(month, 0) < median * fraction:
            wanted.add(month)
    return wanted


def collect_live_index(ref_months: set[str], code_to_cnpj: dict[int, str],
                       today: datetime.date, span_days: int = 15) -> dict:
    """(cnpj, tipo, ref_month) -> {proto} for every wanted filing, gathered
    by sweeping delivery-date windows market-wide.

    Filings for a reference month are delivered from that month onward
    (mostly by the 10th of the next month, corrections later), so the sweep
    runs from the first of the earliest wanted month to today in span_days
    chunks -- small enough that each market query stays fast and reliable.
    Filings whose company code isn't in our FCA-derived universe are
    dropped.
    """
    if not ref_months:
        return {}
    earliest = min(ref_months)
    start = datetime.date(int(earliest[:4]), int(earliest[5:7]), 1)
    index: dict = {}
    seen_proto: set = set()
    cur = start
    while cur <= today:
        end = min(cur + datetime.timedelta(days=span_days - 1), today)
        de, ate = cur.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")
        try:
            rows = list(iter_live_market_index(de, ate))
        except Exception as e:
            print(f"  live index {de}..{ate} failed: {e}")
            cur = end + datetime.timedelta(days=1)
            continue
        for code, name, tipo, ref_month, proto in rows:
            if ref_month not in ref_months or proto in seen_proto:
                continue
            cnpj = code_to_cnpj.get(code)
            if not cnpj:
                continue
            seen_proto.add(proto)
            index.setdefault((cnpj, tipo, ref_month), set()).add(proto)
        cur = end + datetime.timedelta(days=1)
    return index


def _fetch_parse_live(args):
    """(key, proto) -> (key, proto, records, error). key is (cnpj, tipo,
    month). Buyback PDFs parse without a role; consolidated insider PDFs
    parse with the governance-group role read off the form."""
    key, proto = args
    cnpj, tipo, month = key
    with_role = tipo == INSIDER_TIPO
    url = LIVE_DOWNLOAD_URL.format(protocolo=proto)
    last_err = None
    for attempt in range(3):
        try:
            b = fetch_url(url)
            if not b.startswith(PDF_MAGIC):
                raise ValueError(f"response is not a PDF (starts {b[:24]!r})")
            return key, proto, parse_position_pdf(b, month, with_role=with_role), ""
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    return key, proto, [], f"{cnpj} {tipo[:20]} {month} proto {proto}: {last_err}"


def refresh_from_live_search(companies: dict, buybacks: dict, live_names: dict,
                             code_to_cnpj: dict, keep_cnpjs: set, ins_months: set,
                             bb_months: set, today: datetime.date):
    """Replace the given reference months, in place, with live ENET data for
    both insiders (companies[cnpj]['insiders']) and buybacks
    (buybacks[cnpj]['records']). Every other month is left exactly as the
    bulk export produced it. Aborts if too many PDFs fail, since each
    failure would silently drop a company-month.
    """
    want = {(BUYBACK_TIPO, m) for m in bb_months} | {(INSIDER_TIPO, m) for m in ins_months}
    if not want:
        return {}
    all_months = ins_months | bb_months
    print(f"Live refresh: insiders {sorted(ins_months)} buybacks {sorted(bb_months)}")
    index = collect_live_index(all_months, code_to_cnpj, today)
    # Only companies with a ticker reach the ranking tables, same as the bulk
    # path -- no point fetching PDFs for the rest.
    index = {k: v for k, v in index.items() if k[0] in keep_cnpjs and (k[1], k[2]) in want}
    tasks = [(k, proto) for k, protos in index.items() for proto in protos]
    print(f"  {len(tasks)} live filings to fetch across {len(index)} company-months")

    # Filer coverage per refreshed month (distinct companies that filed,
    # trades or not) -- what the partial-month flag needs. Taken from the
    # index rather than parsed records, since a no-trade filing yields no
    # records but still counts as coverage.
    kind = {BUYBACK_TIPO: "buybacks", INSIDER_TIPO: "insiders"}
    live_filer_counts: dict = {}
    for (cnpj, tipo, month) in index:
        live_filer_counts.setdefault((kind[tipo], month), set()).add(cnpj)
    live_filer_counts = {k: len(v) for k, v in live_filer_counts.items()}

    # (cnpj, tipo, month) -> {proto: [records]}
    parsed: dict = {}
    failures: list[str] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for key, proto, records, error in pool.map(_fetch_parse_live, tasks):
            done += 1
            if done % 300 == 0:
                print(f"  ...{done}/{len(tasks)}")
            if error:
                failures.append(error)
                continue
            parsed.setdefault(key, {})[proto] = records
    if failures:
        rate = len(failures) / max(1, len(tasks))
        print(f"  {len(failures)}/{len(tasks)} live filings failed ({rate:.1%})")
        for line in failures[:5]:
            print(f"    {line}")
        if rate > MAX_BUYBACK_FAILURE_RATE:
            raise SystemExit(
                f"ABORT: {len(failures)}/{len(tasks)} live filings ({rate:.1%}) failed, over the "
                f"{MAX_BUYBACK_FAILURE_RATE:.0%} limit. Each failure drops a company-month; "
                f"publishing would understate recent activity. Usually rad.cvm.gov.br "
                f"throttling -- re-run later."
            )

    touched_ins, touched_bb = set(), set()
    for (cnpj, tipo, month), by_proto in parsed.items():
        if tipo == BUYBACK_TIPO:
            best = max(by_proto)  # latest version supersedes corrections
            recs = by_proto[best]
            store = buybacks.setdefault(cnpj, {"name": live_names.get(cnpj, ""), "records": []})
            store["records"] = [r for r in store["records"] if r["ref"][:7] != month] + recs
            touched_bb.add(cnpj)
        else:
            # One consolidated doc per governance group; keep the highest-proto
            # doc per role (dedups version corrections, keeps distinct groups).
            docs = list(by_proto.items())
            roles = {r.get("role") for _, recs in docs for r in recs}
            merged = []
            for role in roles:
                best = max(p for p, recs in docs if any(r.get("role") == role for r in recs))
                for r in by_proto[best]:
                    if r.get("role") != role:
                        continue
                    r = dict(r, role=ROLE_KEY_TO_LABEL.get(role, role))
                    merged.append(r)
            data = companies.setdefault(cnpj, {"name": live_names.get(cnpj, ""),
                                               "insiders": [], "monthly": {}})
            data["insiders"] = [r for r in data["insiders"] if r["ref"][:7] != month] + merged
            touched_ins.add(cnpj)

    for cnpj in touched_bb:
        buybacks[cnpj]["monthly"] = compute_monthly(buybacks[cnpj]["records"])
    for cnpj in touched_ins:
        companies[cnpj]["monthly"] = compute_monthly(companies[cnpj]["insiders"], by_role=True)
    print(f"  refreshed {len(touched_ins)} companies' insiders, {len(touched_bb)} buybacks from live")
    return live_filer_counts


def load_buyback_filings(years: list[int], known_cnpjs: set[str]) -> tuple[dict, dict, list[int]]:
    """cnpj -> [(month, pdf_url), ...] for the buyback-specific filing, plus
    cnpj -> company name and the list of years whose bulk zip was missing.

    Restricted to known_cnpjs (companies we already track via VLMO/FCA) to
    avoid spending requests on the long tail of unlisted/inactive filers.
    Recent or missing months are filled from CVM's live search later, in
    refresh_from_live_search, which is authoritative for those months.
    """
    filings: dict[str, list[tuple[str, str]]] = {}
    names: dict[str, str] = {}
    missing_years: list[int] = []
    for year in years:
        url = IPE_URL.format(year=year)
        print(f"Downloading {url}")
        try:
            zf = fetch_zip(url)
        except Exception as e:
            print(f"  skip {year}: {e} -- live search will backfill this year")
            missing_years.append(year)
            continue
        member = f"ipe_cia_aberta_{year}.csv"
        if member not in zf.namelist():
            continue
        for row in read_csv_member(zf, member):
            cnpj = row["CNPJ_Companhia"].strip()
            if cnpj not in known_cnpjs:
                continue
            if row.get("Tipo", "").strip() != BUYBACK_TIPO:
                continue
            names[cnpj] = row["Nome_Companhia"].strip()
            month = row["Data_Referencia"].strip()[:7]
            link = row.get("Link_Download", "").strip()
            if link:
                filings.setdefault(cnpj, []).append((month, link))

    return filings, names, missing_years


# rad.cvm.gov.br answers some requests with an HTML error/throttle page
# under HTTP 200. fetch_url sees a valid response and returns it, then
# pdfplumber rejects it ("No /Root object!") -- which used to be swallowed
# as "this company had no buybacks". On 2026-08-14 that silently deleted
# 609 records across 548 filings, shrinking every month in the dataset,
# and the run still reported success. Check the magic bytes so a bad
# response is retried rather than believed.
PDF_MAGIC = b"%PDF"

# Above this share of filings failing after retries, the dataset is too
# holed to publish: each failure erases one company-month, so a partial
# fetch looks exactly like companies having stopped buying back.
MAX_BUYBACK_FAILURE_RATE = 0.02


def fetch_and_parse_buyback(args) -> tuple[str, str, list[dict], str]:
    """Returns (cnpj, month, records, error). error is "" on success.

    An empty record list is a real answer -- most filings report no trades
    -- so failures are reported out of band rather than as emptiness.
    """
    cnpj, month, url = args
    last_err = None
    for attempt in range(3):
        try:
            pdf_bytes = fetch_url(url)
            if not pdf_bytes.startswith(PDF_MAGIC):
                raise ValueError(f"response is not a PDF (starts {pdf_bytes[:24]!r})")
            return cnpj, month, parse_buyback_pdf(pdf_bytes, month), ""
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    return cnpj, month, [], f"{cnpj} {month}: {last_err}"


def load_buybacks(years: list[int], known_cnpjs: set[str]) -> tuple[dict[str, dict], dict[str, int], dict[str, str], list[int]]:
    """cnpj -> {"name": ..., "records": [...], "monthly": {...}}, parsed from
    PDFs, plus month -> filer count, cnpj -> name, and missing bulk years.

    The filing count comes from the IPE index rather than the parsed
    records because most buyback filings report no trades at all: counting
    parsed records would measure activity, where the caller needs coverage.
    """
    filings, names, missing_years = load_buyback_filings(years, known_cnpjs)
    filing_counts = collections.Counter(
        month for entries in filings.values() for month, _ in entries
    )
    tasks = [(cnpj, month, url) for cnpj, entries in filings.items() for month, url in entries]
    print(f"Fetching {len(tasks)} buyback filings...")

    result: dict[str, dict] = {}
    failures: list[str] = []
    done = 0
    # 8 rather than 12: the throttle responses this retries around get more
    # common the harder rad.cvm.gov.br is pushed.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for cnpj, month, records, error in pool.map(fetch_and_parse_buyback, tasks):
            done += 1
            if done % 250 == 0:
                print(f"  ...{done}/{len(tasks)}")
            if error:
                failures.append(error)
                continue
            company = result.setdefault(cnpj, {"name": names.get(cnpj, ""), "records": []})
            company["records"].extend(records)

    if failures:
        rate = len(failures) / len(tasks)
        print(f"  {len(failures)}/{len(tasks)} filings failed after retries ({rate:.1%})")
        for line in failures[:5]:
            print(f"    {line}")
        if rate > MAX_BUYBACK_FAILURE_RATE:
            raise SystemExit(
                f"ABORT: {len(failures)} of {len(tasks)} buyback filings ({rate:.1%}) could not be "
                f"fetched, over the {MAX_BUYBACK_FAILURE_RATE:.0%} limit.\nEach failure silently "
                f"erases a company-month, so publishing this would understate buybacks across the "
                f"whole dataset.\nUsually rad.cvm.gov.br throttling -- re-run later."
            )

    for company in result.values():
        company["monthly"] = compute_monthly(company["records"])
    print(f"Parsed buyback activity for {len(result)} companies")
    return result, filing_counts, names, missing_years


# Preference order for FRE's capital_social Tipo_Capital when a company
# reports more than one in the same filing (lower = preferred). Capital
# Integralizado (paid-in) is the standard "shares actually outstanding now"
# figure; Emitido and Subscrito are reasonable stand-ins when that's not
# filed. "Capital Autorizado" is excluded -- it's an authorization ceiling,
# often far above real shares outstanding, not a current share count.
CAPITAL_TYPE_RANK = {"Capital Integralizado": 0, "Capital Emitido": 1, "Capital Subscrito": 2}


def load_total_shares() -> dict[str, float]:
    """cnpj -> best-known total share count, from FRE capital tables.

    Used for % do Capital. Coverage is incomplete (not every company
    refiles every year, and a few are absent every year checked) --
    callers must treat a missing cnpj as unknown, not zero, and show "--"
    rather than 0%.

    Falls back to distribuicao_capital's shares-in-circulation figure only
    when a company has no usable capital_social row at all -- that table is
    free-float only (excludes controller/insider-held shares by
    definition), which is the wrong denominator for "% of capital" and,
    verified in one case (Neogrid), can also just be wrong: its 2026 filing
    reported 386,399 shares in circulation, a ~10x drop from 2025's
    3,869,250 with no matching corporate event, while capital_social's
    Capital Integralizado held steady at 9,140,944 across all three years.
    """
    by_type: dict[str, dict[str, tuple[str, float]]] = {}  # tipo -> cnpj -> (ref, shares)
    circulating: dict[str, tuple[str, float]] = {}  # cnpj -> (ref, shares), from distribuicao_capital

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
                tipo = row.get("Tipo_Capital", "").strip()
                if tipo not in CAPITAL_TYPE_RANK:
                    continue
                store = by_type.setdefault(tipo, {})
                consider(store, row["CNPJ_Companhia"].strip(), row["Data_Referencia"].strip(), row.get("Quantidade_Total_Acoes", ""))
        member = f"fre_cia_aberta_distribuicao_capital_{year}.csv"
        if member in zf.namelist():
            for row in read_csv_member(zf, member):
                consider(circulating, row["CNPJ_Companhia"].strip(), row["Data_Referencia"].strip(), row.get("Quantidade_Total_Acoes_Circulacao", ""))

    # Apply worst-to-best so the most-preferred available type wins per
    # company, regardless of which years/types happened to have data.
    result = {cnpj: shares for cnpj, (ref, shares) in circulating.items()}
    for tipo in sorted(CAPITAL_TYPE_RANK, key=lambda t: -CAPITAL_TYPE_RANK[t]):
        for cnpj, (ref, shares) in by_type.get(tipo, {}).items():
            result[cnpj] = shares
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


def load_code_cnpj_map() -> dict[int, str]:
    """CVM numeric company code -> CNPJ, from FCA's registration table.

    The live ENET search identifies companies by numeric code; the rest of
    the pipeline keys on CNPJ, so this bridges the two. FCA is a slow-moving
    registry (code<->CNPJ is effectively static for a listed company), so a
    week-stale copy during a bulk-export freeze is still fine here.
    """
    mapping: dict[int, str] = {}
    for year in YEARS:
        try:
            zf = fetch_zip(FCA_URL.format(year=year))
        except Exception as e:
            print(f"  code map skip {year}: {e}")
            continue
        member = f"fca_cia_aberta_{year}.csv"
        if member not in zf.namelist():
            continue
        for row in read_csv_member(zf, member):
            code = re.sub(r"\D", "", row.get("CD_CVM", ""))
            cnpj = row.get("CNPJ_CIA", "").strip()
            if code and cnpj:
                mapping[int(code)] = cnpj
    return mapping


def load_transactions() -> tuple[dict[str, dict], list[int]]:
    """(cnpj -> {name, insiders: [...], monthly: {...}}, missing bulk years)

    Note: this dataset's "Tipo_Cargo blank" rows (nominally the company's own
    trades) are almost never populated with real trades -- CVM's structured
    extraction of that sub-section is unreliable (verified: 5 real trade rows
    across all ~500 companies for a full year). Real buyback data comes from
    load_buybacks() instead, which parses the actual filed PDFs.
    """
    companies: dict[str, dict] = {}
    missing_years: list[int] = []
    for year in YEARS:
        url = VLMO_URL.format(year=year)
        print(f"Downloading {url}")
        try:
            zf = fetch_zip(url)
        except Exception as e:
            print(f"  skip {year}: {e} -- live search will backfill this year")
            missing_years.append(year)
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

    for company in companies.values():
        company["monthly"] = compute_monthly(company["insiders"], by_role=True)
    return companies, missing_years


def _num(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _monthly_row(agg: dict, cnpj_digits: str, name: str, company_tickers: list[str], month: str, total_shares, role: str = None) -> dict:
    qty, val = agg["qty"], agg["val"]
    gross_qty, gross_val = agg["gross_qty"], agg["gross_val"]
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
    if role:
        row["role"] = role
    if total_shares is not None:
        shares = total_shares.get(cnpj_digits)
        # Signed: a net sale (qty < 0) shows as a negative % of capital, so
        # the column reads the same direction as Valor.
        row["pct"] = qty / shares * 100 if shares else None
    return row


def monthly_dict_to_rows(monthly: dict, cnpj_digits: str, name: str, company_tickers: list[str], months_seen: set, total_shares=None):
    """One row per (month) for the aggregate (unchanged shape), plus one more
    row per (month, role) when compute_monthly was run with by_role=True --
    the ranking table's role toggle filters on that "role" field, defaulting
    to the roleless aggregate rows when no role is selected."""
    rows = []
    for month, agg in monthly.items():
        months_seen.add(month)
        if agg["qty"]:
            rows.append(_monthly_row(agg, cnpj_digits, name, company_tickers, month, total_shares))
        for role_key, role_agg in agg.get("by_role", {}).items():
            rows.append(_monthly_row(role_agg, cnpj_digits, name, company_tickers, month, total_shares, role=role_key))
    return rows


# Every loader here catches its own fetch failure, prints a skip, and
# returns empty -- correct per-source (one missing year shouldn't sink a
# run) but it makes a total outage indistinguishable from "CVM has no
# data": the run completes cleanly, writes nothing, and exits 0. Verified
# on 2026-08-10, when every CVM request from the GitHub runner failed with
# "Network is unreachable" and the pipeline overwrote a healthy
# 506-company dataset with 0 companies, committed it, and published a
# blank dashboard. So compare against the dataset already on disk and
# refuse to write when it shrinks materially. Set ALLOW_DATA_SHRINK=1 to
# override, for the rare case where CVM genuinely revises data downward.
MIN_RETAINED_FRACTION = 0.9


def assert_not_degraded(all_cnpjs: set, companies: dict, buybacks: dict):
    """Aborts before any file is written if this run's data is drastically
    smaller than the previous one -- the signature of a failed fetch rather
    than a real change. All network I/O happens before this point, so
    tripping here leaves the committed dataset untouched.
    """
    prev_path = OUT_DIR / "companies.json"
    if not prev_path.exists():
        if not all_cnpjs:
            raise SystemExit("ABORT: produced 0 companies and there is no previous dataset to fall back on")
        return
    with open(prev_path, encoding="utf-8") as f:
        prev = json.load(f)

    checks = [
        ("companies", len(all_cnpjs), len(prev)),
        ("insider records",
         sum(len(c["insiders"]) for c in companies.values()),
         sum(c.get("insider_count", 0) for c in prev)),
        ("buyback records",
         sum(len(b["records"]) for b in buybacks.values()),
         sum(c.get("buyback_count", 0) for c in prev)),
    ]
    problems = [
        f"  {label}: {new} now vs {old} before ({new / old:.0%})"
        for label, new, old in checks
        if old > 0 and new < old * MIN_RETAINED_FRACTION
    ]
    if not problems:
        return
    if os.environ.get("ALLOW_DATA_SHRINK") == "1":
        print("WARNING: dataset shrank, writing anyway (ALLOW_DATA_SHRINK=1):")
        print("\n".join(problems))
        return
    raise SystemExit(
        "ABORT: refusing to overwrite the published dataset -- this run lost data:\n"
        + "\n".join(problems)
        + "\n\nUsually means the CVM fetches failed (check for skip/404/unreachable above)."
          "\nRe-run once CVM is reachable, or set ALLOW_DATA_SHRINK=1 if the drop is real."
    )


# CVM gives companies until the 10th of the following month to file their
# Art. 11 disclosures, and its bulk export trails delivery by ~2 days, so
# the newest month is always partially filed. Reporting it like a settled
# one reads as "buybacks collapsed" (on 2026-08-10 July showed 6 buyback
# rows against 19-36 for settled months), so flag it for the frontend.
#
# Coverage is measured as *how many companies filed*, not how many rows
# they generated. Rows count only companies that actually traded, which
# swings wildly on its own -- settled buyback months ranged 19-36 rows
# (+/-33%) while the filing count behind them held at 251-262 (+/-2%).
# Any threshold high enough to catch a half-filed month would therefore
# false-flag a genuinely quiet one if it went by rows: April 2026 had 19
# rows, 73% of the median, while being completely filed. Filing counts
# separate cleanly, so the threshold can sit high enough to catch the
# ~55%-covered month the 11th-of-month run sees.
PARTIAL_MONTH_FRACTION = 0.8


def partial_tail_months(coverage: dict[str, int], published: set[str], lookback: int = 12) -> list[str]:
    """The trailing published months whose filing count is far below recent norms.

    Walks backwards from the newest month and stops at the first one that
    looks settled -- filings only ever arrive late, so a thin month in the
    middle of the series is a real signal about that month, not an
    artefact of when the pipeline happened to run.
    """
    months = sorted(published)
    partial = []
    for month in reversed(months):
        baseline = [coverage.get(m, 0) for m in months if m < month][-lookback:]
        if len(baseline) < 3:
            break  # too little history to call anything anomalous
        median = statistics.median(baseline)
        if median > 0 and coverage.get(month, 0) < median * PARTIAL_MONTH_FRACTION:
            partial.append(month)
        else:
            break
    return sorted(partial)


def _filer_counts(by_cnpj: dict, records_key: str) -> collections.Counter:
    """month -> number of companies that filed for it (one per company, not
    per trade), the coverage measure used to spot months bulk hasn't
    finished publishing."""
    return collections.Counter(
        month
        for data in by_cnpj.values()
        for month in {r["ref"][:7] for r in data.get(records_key, []) if r.get("ref")}
    )


def main():
    today = datetime.date.today()
    # Trailing display window: refresh live only within the months the site
    # shows, so a stale older year never triggers a full re-parse.
    window_floor = f"{min(YEARS) - 1}-01"

    tickers = load_tickers()
    code_to_cnpj = load_code_cnpj_map()
    companies, ins_missing_years = load_transactions()
    buybacks, bb_filing_counts, bb_names, bb_missing_years = load_buybacks(
        YEARS, known_cnpjs=set(tickers.keys()))

    # CVM's bulk exports lag delivery by ~2 days and periodically freeze, so
    # the newest reference months arrive late or not at all. Replace those
    # months -- for both tabs -- with CVM's live document search, which
    # serves filings in real time (the source Fundamentus uses). Bulk stays
    # authoritative for settled months.
    insider_bulk_counts = _filer_counts(companies, "insiders")
    ins_months = months_to_refresh(insider_bulk_counts, ins_missing_years, today, window_floor)
    bb_months = months_to_refresh(bb_filing_counts, bb_missing_years, today, window_floor)
    live_filer_counts = refresh_from_live_search(
        companies, buybacks, bb_names, code_to_cnpj, set(tickers.keys()),
        ins_months, bb_months, today)

    total_shares_by_cnpj = load_total_shares()
    total_shares = {
        "".join(ch for ch in cnpj if ch.isdigit()): shares
        for cnpj, shares in total_shares_by_cnpj.items()
    }

    all_cnpjs = set(companies.keys()) | set(buybacks.keys())
    # After the live refresh, so a frozen bulk export (which the refresh has
    # just healed) doesn't read as a data loss and abort the run.
    assert_not_degraded(all_cnpjs, companies, buybacks)

    # Coverage for the partial-month flag: bulk filer counts, overridden by
    # the live filer count for any month the refresh replaced.
    insider_filing_counts = dict(insider_bulk_counts)
    for (kind, month), count in live_filer_counts.items():
        target = insider_filing_counts if kind == "insiders" else bb_filing_counts
        target[month] = max(target.get(month, 0), count)

    BY_COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    monthly_rows = []
    bb_monthly_rows = []
    months_seen = set()

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
        monthly_rows.extend(monthly_dict_to_rows(data["monthly"], cnpj_digits, name, company_tickers, months_seen, total_shares=total_shares))
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
            "partial_months": {
                "insiders": partial_tail_months(insider_filing_counts, months_seen),
                "buybacks": partial_tail_months(bb_filing_counts, months_seen),
            },
        }, f)

    print(f"Wrote {len(index)} companies, {len(monthly_rows)} insider monthly rows, "
          f"{len(bb_monthly_rows)} buyback monthly rows to {OUT_DIR}")


if __name__ == "__main__":
    main()
