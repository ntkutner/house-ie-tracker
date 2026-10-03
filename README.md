# House IE Tracker

A self-updating version of the "House Races w/ Party Spending (General Election)" chart, built
from the FEC's own bulk file of 24- and 48-hour independent expenditure reports. Standard-library
Python only. No FEC API key, no pagination, no rate limits: one file download per run, and
nothing at all when the file hasn't changed.

## What you get

`docs/index.html` is a page with one row per House race: CLF, NRCC*, TRUMP, OTH R, HMP, DCCC*,
OTH D, the R/D totals, advantage, money filed in the last 7 days, and the grand total. Sort by any
column, filter by district/candidate/group, toggle between party races and all races, and click
a row to see every group in that race, who they're for or against, and links to their FEC pages.
`docs/races.csv` and `docs/transactions.csv` hold the same data for spreadsheets.

## Set it up to update itself (about 5 minutes, free)

1. Create a new GitHub repository and upload everything in this folder (keep `.github/`).
2. Repo **Settings > Actions > General > Workflow permissions**: choose "Read and write permissions".
3. **Actions** tab > "Update House IE tracker" > **Run workflow**. The first build takes a few minutes.
4. **Settings > Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`.
5. Your tracker is live at `https://<you>.github.io/<repo>/`. It checks the FEC hourly and
   republishes whenever there's new data.

GitHub Pages on a free account requires a public repo; everything in it is public FEC data.

## Run it locally

    python3 tracker.py            # Python 3.11+
    open docs/index.html

`--force` rebuilds even if the FEC file is unchanged. `--input file.csv` builds from a local copy.
Tests: `python3 -m unittest discover tests`

## Customize (config.toml)

- **Columns.** Each `[[columns]]` block is a group with its own column, matched by FEC ID or name.
  Copy a block to break out another group (e.g. AAN, House Majority Forward, Protect Progress).
  The page's "Data notes" lists the biggest groups currently lumped into OTH R / OTH D.
- **Party overrides.** If a candidate is reported as OTHER/blank, or an independent is effectively
  one side's nominee, add `"<FEC candidate ID>" = "REP"` or `"DEM"`. Uncounted candidates are
  listed in Data notes.
- **2024 presidential margins.** Fill in `pres_margins_2024.csv` (`CO08,R+1.8`, `NY17,D+0.6`)
  and a PRES '24 column appears. Use margins for the current district lines.
- **Races.** `races = "party"` shows races where any named column has spent (like the original
  chart). `"all"` shows every race with general-election IEs.

## How the numbers are built

- **Same-day IEs:** the FEC's bulk file is rebuilt about once a day. Every hourly run also lists
  the 24/48-hour reports (and Form 5s) filed since then, reads each one straight from the filing
  (via the OpenFEC API and docquery.fec.gov, using your FEC_API_KEY secret), and folds them in.
  Once a filing shows up in the bulk file, the bulk version is used instead. Amended notices
  replace the notice they amend. Parsed filings are cached in `cache/ie/`.

- **Party columns (NRCC*, DCCC*)** count party coordinated expenditures as well as any IEs. The
  parties' recent monthly reports are read straight from their e-filings (Schedule F), so they
  count the day they're filed; older periods come from the FEC's processed bulk file. Listing
  the parties' filings uses the OpenFEC API: add a free key from api.data.gov as a repository
  secret named FEC_API_KEY (Settings > Secrets and variables > Actions). Parsed filings are
  cached in `cache/coordinated/`.
- **TRUMP** counts MAGA Inc., No Going Back PAC, and Safety and Affordability PAC.

- House rows with election type G for the configured cycle.
- **Amendments:** when a filing is amended, every row from the older versions is dropped and only
  the newest version counts. This handles both A1-then-A2 chains and amendments that all point at
  the original.
- **Re-filings:** an identical transaction (same spender, transaction ID, candidate, amount, date)
  filed in two separate reports is counted once.
- **Side:** supporting a Republican or opposing a Democrat is R-side, and vice versa.
- **REP / DEM names:** the candidate on each side with the most IE money in the race.
- **Blank race info:** filings missing office, state or district are filled in from the candidate's
  FEC ID and the FEC candidate file.
- **QA:** Data notes shows, for each named column, any money in the FEC files that wasn't counted
  and why (coded as primary, removed by an amendment, no usable party), with row detail in
  `docs/not_counted.csv`. It also flags any spender/candidate pair where the counted total exceeds the filer's
  own reported running aggregate by more than 5%.

Known limits: the file only contains 24/48-hour notices, so IEs disclosed only on monthly or
quarterly reports (generally small ones made more than 20 days out) won't appear. The FEC refreshes this
file roughly daily, so "real time" here means within about a day of filing.
