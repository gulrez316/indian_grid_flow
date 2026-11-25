# India Grid Flow

An animated map of power moving between India's five regional grids (NR, WR, SR, ER, NER) and to Nepal, Bhutan and Bangladesh, in 15-minute time blocks.

- **Live mode**: measured state demand for the current block, summed into regions. Generation mix and each region's net position come from yesterday's Grid-India report. Flows between regions are estimated from those numbers.
- **Model a day**: a modelled day for summer, monsoon or winter that you can play or scrub through.

## How it works

```
GitHub Actions (every 15 min)
  └─ fetcher/fetch.py
       ├─ vidyutpravah.in            live state demand met (MW), current time block
       └─ webapi.grid-india.in       catalog of NLDC Daily PSP reports
          webcdn.grid-india.in       PSP workbook (.xls): energy by fuel per region
  └─ data/latest.json  ──►  GitHub Pages  ──►  index.html reads it every 5 min
```

The workflow downloads the currently published `data/latest.json` first. A source that fails keeps its last good values, and the day's PSP report is parsed only once.

## Set up

1. Create a GitHub repository and push this folder to it.
2. In the repository's **Settings → Pages**, set **Source** to **GitHub Actions**.
3. Open **Actions → Update grid data → Run workflow** once. After that it runs every 15 minutes. GitHub can delay scheduled runs by several minutes when it is busy.

The site is published at `https://<user>.github.io/<repo>/`.

## Run locally

```bash
pip install -r fetcher/requirements.txt
python fetcher/fetch.py          # writes data/latest.json
python -m http.server            # then open http://localhost:8000
```

`python fetcher/fetch.py --only live` or `--only psp` runs one source; `--stdout` prints instead of writing.

## Caveats

- **Inter-regional flows are estimates.** Metered flows are published by the regional load despatch centres (RLDCs). Their live endpoints return no data outside India, so the page solves for flows from each region's demand and net position.
- **Vidyut Pravah lists 31 states.** Odisha, Ladakh and DNH & DD are missing. Their share of their region is taken from the PSP report and the live sum is scaled up.
- **Vidyut Pravah's TLS certificate has often been invalid.** The fetcher tries a verified request first. Only if that fails on TLS does it retry that one public, read-only host without verification, and it logs a warning when it does.
- **The government sites reset connections from some cloud networks.** Other open-source projects fetch Grid-India's API from GitHub Actions successfully. If a source keeps failing in your runs, its error is recorded under `errors` in `latest.json` and the page keeps the last good values.
- **The parsers follow the current page and report layouts.** If a layout changes, the run records an error instead of writing wrong numbers.

## Credits

- State boundaries: [DataMeet India community](https://github.com/datameet/maps) (CC BY 4.0), simplified with mapshaper.
- Data: Grid Controller of India (Grid-India) and Vidyut Pravah (Ministry of Power).
- Endpoint notes drawn from the open-source projects [ADIMANV/India-Energy-Data](https://github.com/ADIMANV/India-Energy-Data) and [randomaban-beep/india-power-demand-pipeline](https://github.com/randomaban-beep/india-power-demand-pipeline).
