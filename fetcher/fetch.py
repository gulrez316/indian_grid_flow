"""Fetch live and daily Indian grid data and write data/latest.json for the map.

Two sources:

1. Vidyut Pravah (Ministry of Power): live state-wise "demand met" for the
   current 15-minute time block. Summed into the five regional grids.
2. Grid-India (NLDC) Daily PSP report: yesterday's measured energy by fuel,
   energy met and peak demand per region. Used to estimate the generation mix
   and each region's net import/export position.

Run:  python fetcher/fetch.py            (writes data/latest.json)
      python fetcher/fetch.py --only live|psp
      python fetcher/fetch.py --stdout   (print, don't write)

Each source is independent. If one fails, the previous values for it are kept
in latest.json and the failure is recorded under "errors".
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("fetch")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "latest.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

REGIONS = ["NR", "WR", "SR", "ER", "NER"]

# Vidyut Pravah state slugs -> Grid-India region
VP_STATES = {
    "NR": ["chandigarh", "delhi", "haryana", "himachal-pradesh", "jammu-kashmir", "punjab", "rajasthan", "uttar-pradesh", "uttarakhand"],
    "WR": ["chhattisgarh", "goa", "gujarat", "madhya-pradesh", "maharashtra"],
    "SR": ["andhra-pradesh", "karnataka", "kerala", "puducherry", "tamil-nadu", "telangana"],
    "ER": ["bihar", "jharkhand", "sikkim", "west-bengal"],
    "NER": ["arunachal-pradesh", "assam", "manipur", "meghalaya", "mizoram", "nagaland", "tripura"],
}
# States Vidyut Pravah does not list. Their share of each region is taken from
# yesterday's PSP report and used to scale the live regional sums up
# (see apply_coverage). Values are regexes matched against PSP state names.
VP_MISSING = {"ER": r"odisha|orissa", "WR": r"dnh|dadra|daman", "NR": r"^ladakh"}

VP_BASE = "https://vidyutpravah.in"
GI_API = "https://webapi.grid-india.in/api/v1/file"
GI_CDN = "https://webcdn.grid-india.in"


# --------------------------------------------------------------------------- http

def get(url: str, *, tries: int = 3, **kw) -> requests.Response:
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=25, **kw)
            r.raise_for_status()
            return r
        except requests.exceptions.SSLError:
            raise
        except Exception as e:  # connection resets are common on these sites
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def get_vp(url: str) -> str:
    """Vidyut Pravah's certificate has been invalid for long stretches.

    Try a verified request first. Only if that fails on TLS do we fall back to
    an unverified one, for this single public, read-only host.
    """
    try:
        return get(url).text
    except requests.exceptions.SSLError:
        log.warning("vidyutpravah.in TLS verification failed; retrying unverified (public read-only data)")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return get(url, verify=False).text


# --------------------------------------------------------------------------- live

NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_vp_state(html: str) -> dict[str, Any]:
    demand = re.search(r'class="[^"]*value_DemandMET_en[^"]*"[^>]*>\s*' + NUM, html)
    block = re.search(r"TIME\s*BLOCK\s*([0-9]{1,2}:[0-9]{2})\s*-\s*([0-9]{1,2}:[0-9]{2})\s*DATED\s*([0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4})", html, re.I)
    return {
        "demand_mw": _num(demand.group(1)) if demand else None,
        "block": f"{block.group(1)}-{block.group(2)}" if block else None,
        "date": block.group(3) if block else None,
    }


def fetch_live() -> dict[str, Any]:
    home = get_vp(VP_BASE + "/")
    cur = re.search(r'id="CurrentDemandMET"[^>]*>\s*' + NUM, home)
    all_india = _num(cur.group(1)) if cur else None

    slugs = [(r, s) for r in REGIONS for s in VP_STATES[r]]
    states: dict[str, Any] = {}

    def one(item):
        r, s = item
        try:
            return r, s, parse_vp_state(get_vp(f"{VP_BASE}/state-data/{s}"))
        except Exception as e:
            return r, s, {"error": str(e)[:200]}

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for r, s, res in ex.map(one, slugs):
            states[s] = {"region": r, **res}

    regions: dict[str, Any] = {}
    for r in REGIONS:
        vals = [states[s].get("demand_mw") for s in VP_STATES[r]]
        got = [v for v in vals if v is not None]
        regions[r] = {
            "demand_mw": round(sum(got)) if got else None,
            "states_reported": len(got),
            "states_expected": len(vals),
        }

    blocks = [v.get("block") for v in states.values() if v.get("block")]
    dates = [v.get("date") for v in states.values() if v.get("date")]
    reported = sum(1 for v in states.values() if v.get("demand_mw") is not None)
    if reported < len(slugs) * 0.6:
        raise RuntimeError(f"only {reported}/{len(slugs)} states parsed; page layout may have changed")
    return {
        "source": "vidyutpravah.in",
        "fetched_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "time_block": max(set(blocks), key=blocks.count) if blocks else None,
        "date": max(set(dates), key=dates.count) if dates else None,
        "all_india_demand_mw": all_india,
        "regions": regions,
        "states": {s: v.get("demand_mw") for s, v in states.items()},
        "not_covered": VP_MISSING,
    }


# --------------------------------------------------------------------------- PSP

def psp_catalog() -> list[dict[str, Any]]:
    r = requests.post(GI_API, json={"_source": "GRDW", "_type": "DAILY_PSP_REPORT"}, headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json().get("retData") or []
    if not data:
        raise RuntimeError("Grid-India catalog returned no reports")
    return data


def _title_date(title: str) -> dt.date | None:
    m = re.search(r"(\d{1,2})[.\-_](\d{1,2})[.\-_](\d{2,4})", title or "")
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    y = y + 2000 if y < 100 else y
    try:
        return dt.date(y, mth, d)
    except ValueError:
        return None


def pick_latest_xls(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    xls = [c for c in catalog if str(c.get("FilePath", "")).lower().endswith((".xls", ".xlsx"))
           or "excel" in str(c.get("MimeType", "")).lower()]
    if not xls:
        raise RuntimeError("no Excel PSP reports in catalog")
    xls.sort(key=lambda c: _title_date(c.get("Title_", "")) or dt.date.min, reverse=True)
    return xls[0]


def _f(v: Any) -> float | None:
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "None", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_psp_xls(content: bytes) -> dict[str, Any]:
    """Parse the MOP_E sheet of the NLDC daily PSP workbook.

    The sheet is a printed report: label in column 0, then one column per
    region (NR, WR, SR, ER, NER) and an All-India total. Column positions are
    found from the header row instead of being hard-coded.
    """
    import xlrd

    wb = xlrd.open_workbook(file_contents=content)
    names = [s.lower() for s in wb.sheet_names()]
    sh = wb.sheet_by_index(names.index("mop_e") if "mop_e" in names else 0)
    cell = lambda r, c: str(sh.cell_value(r, c)).strip() if c < sh.ncols else ""

    report_date = None
    for r in range(min(8, sh.nrows)):
        for c in range(sh.ncols):
            if "date of reporting" in cell(r, c).lower():
                for cc in range(c + 1, min(c + 4, sh.ncols)):
                    v = cell(r, cc)
                    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
                        try:
                            report_date = (dt.datetime.strptime(v, fmt).date() - dt.timedelta(days=1)).isoformat()
                            break
                        except ValueError:
                            pass
                    if report_date:
                        break

    cols: dict[str, int] = {}
    out: dict[str, Any] = {r: {"fuel_mu": {}} for r in REGIONS}
    out["ALL"] = {"fuel_mu": {}}
    section = ""
    transnational: list[list[str]] = []
    state_energy: dict[str, dict[str, float]] = {r: {} for r in REGIONS}
    cur_region = ""

    A_ROWS = {
        "Demand Met during Evening Peak": "evening_peak_mw",
        "Energy Met (MU)": "energy_met_mu",
        "Hydro Gen (MU)": "hydro_mu",
        "Wind Gen (MU)": "wind_mu",
        "Solar Gen (MU)": "solar_mu",
        "Energy Shortage (MU)": "energy_shortage_mu",
        "Maximum Demand Met During the Day": "max_demand_mw",
    }
    G_ROWS = {"Coal": "coal", "Lignite": "lignite", "Hydro": "hydro", "Nuclear": "nuclear",
              "Gas, Naptha & Diesel": "gas", "RES (Wind, Solar, Biomass & Others)": "res", "Total": "total"}

    for r in range(sh.nrows):
        row = [cell(r, c) for c in range(sh.ncols)]
        label = row[0] or (row[1] if len(row) > 1 else "")
        # header row with region columns (re-detected per section)
        if {"NR", "WR", "SR", "ER", "NER"} <= set(row):
            cols = {name: row.index(name) for name in REGIONS}
            tot = next((i for i, v in enumerate(row) if v.upper() in ("TOTAL", "ALL INDIA")), None)
            if tot is not None:
                cols["ALL"] = tot
            continue
        m = re.match(r"^([A-H])\.\s", label)
        if m:
            section = m.group(1)
            continue
        if section == "C":
            if row[0] in REGIONS:
                cur_region = row[0]
            name = row[1] if len(row) > 1 else ""
            e = _f(row[4]) if len(row) > 4 else None
            if cur_region and name and e is not None and name.upper() not in ("TOTAL", "STATES"):
                state_energy[cur_region][name] = e
        if section == "D" and any(k in " ".join(row) for k in ("Bhutan", "Nepal", "Bangladesh", "Myanmar")):
            transnational.append([v for v in row if v])
        if not cols:
            continue
        if section in ("A", ""):
            for key, name in A_ROWS.items():
                if label.replace("*", "").strip().startswith(key):
                    for reg, c in cols.items():
                        out[reg][name] = _f(row[c]) if c < len(row) else None
        if section == "G":
            for key, name in G_ROWS.items():
                if label.strip() == key:
                    for reg, c in cols.items():
                        out[reg]["fuel_mu"][name] = _f(row[c]) if c < len(row) else None

    for reg in [*REGIONS, "ALL"]:
        x = out[reg]
        fu = x["fuel_mu"]
        # RES in section G bundles wind, solar, biomass and small hydro
        if fu.get("res") is not None:
            fu["wind"] = x.get("wind_mu") or 0.0
            fu["solar"] = x.get("solar_mu") or 0.0
            fu["bio"] = max(0.0, fu["res"] - fu["wind"] - fu["solar"])
        if fu.get("total") is not None and x.get("energy_met_mu") is not None:
            # positive = region generated more than it consumed (net exporter)
            x["net_export_mu"] = round(fu["total"] - x["energy_met_mu"], 2)

    if not any(out[r]["fuel_mu"] for r in REGIONS):
        raise RuntimeError("PSP sheet parsed but no generation rows found; layout may have changed")
    return {"report_date": report_date, "regions": {r: out[r] for r in REGIONS}, "all_india": out["ALL"],
            "state_energy_mu": state_energy, "transnational_rows": transnational}


def fetch_psp(prev: dict[str, Any] | None) -> dict[str, Any]:
    item = pick_latest_xls(psp_catalog())
    url = f"{GI_CDN}/{item['FilePath'].lstrip('/')}"
    if prev and prev.get("file") == url:
        log.info("PSP %s already parsed", item.get("Title_"))
        return prev
    parsed = parse_psp_xls(get(url).content)
    return {"source": "Grid-India NLDC Daily PSP report", "title": item.get("Title_"), "file": url,
            "fetched_at": dt.datetime.now(IST).isoformat(timespec="seconds"), **parsed}


def apply_coverage(out: dict[str, Any]) -> None:
    """Scale live regional demand up for states Vidyut Pravah does not report."""
    live, psp = out.get("live"), out.get("psp")
    if not live or not psp:
        return
    for r in REGIONS:
        reg = live["regions"][r]
        reg["coverage"] = 1.0
        states = (psp.get("state_energy_mu") or {}).get(r) or {}
        total = sum(states.values())
        pat = VP_MISSING.get(r)
        if pat and total > 0:
            miss = sum(v for k, v in states.items() if re.search(pat, k, re.I))
            reg["coverage"] = round(max(0.5, 1 - miss / total), 4)
        if reg.get("demand_mw") is not None:
            reg["demand_mw_estimated_total"] = round(reg["demand_mw"] / reg["coverage"])


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["live", "psp"])
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    prev: dict[str, Any] = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
        except json.JSONDecodeError:
            prev = {}
    out = {"schema": 1, "live": prev.get("live"), "psp": prev.get("psp"), "errors": {}}

    if args.only in (None, "live"):
        try:
            out["live"] = fetch_live()
            log.info("live: block %s, all-India %s MW", out["live"]["time_block"], out["live"]["all_india_demand_mw"])
        except Exception as e:
            log.error("live failed: %s", e)
            out["errors"]["live"] = str(e)[:300]
    if args.only in (None, "psp"):
        try:
            out["psp"] = fetch_psp(prev.get("psp"))
            log.info("psp: report for %s", out["psp"]["report_date"])
        except Exception as e:
            log.error("psp failed: %s", e)
            out["errors"]["psp"] = str(e)[:300]

    apply_coverage(out)
    out["generated_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
    text = json.dumps(out, indent=1, ensure_ascii=False)
    if args.stdout:
        print(text)
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text + "\n")
    # fail the run only if nothing at all could be fetched
    return 1 if out["live"] is None and out["psp"] is None else 0


if __name__ == "__main__":
    sys.exit(main())
