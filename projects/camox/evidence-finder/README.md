# CAMOX: Jobs evidence finder (offline)

System Public Challenge, Build Challenge 2 (World Bank Group). Team CAMOX.

This folder is a self-contained companion to the CAMOX Evidence Agent in the parent folder. It answers one question from saved files, with no install, no keys and no network, so it also works as the demo fallback. All paths below are relative to this folder.

A World Bank analyst picks a jobs topic and sees what published research links it to, which World Bank indicator measures each linked concept, and which dataset that indicator is in. They open a link to see the studies and findings behind it, add indicators to a list, and download the list as a CSV.

## Run it

```
open app/index.html
```

That is the whole app: one HTML file and one saved data file. It needs no server, no install and no network. On Windows or Linux, open `app/index.html` in a browser, or run `python3 -m http.server 8000 --directory app` and go to `http://localhost:8000`.

## What is on screen

- **Home.** One search box ("Search a jobs topic") over topics, linked concepts and indicator names, and a button per topic.
- **Topic page.** The topic, the World Bank indicator that measures it, its dataset, an evidence label, and a one-line summary. Below that, two lists: "What affects {topic}" and "What {topic} affects". Each row shows the linked concept, its tier and study count, the matching World Bank indicator and dataset, and a tag when the indicator is in a different dataset or was not found.
- **Evidence panel and list.** Selecting a row shows the study count, up to five findings with the statistic exactly as recorded, and a link to every paper. "Add to my list" keeps the indicator; "Download CSV" exports the list.

Three topics are built: **Unemployment** (job outcome), **Internet use** (Foundations) and **Access to finance** (Private capital, Enterprise Surveys).

## Data sources

| Source | What we take | Where it is saved |
|---|---|---|
| System Public Graph, release v2.1, Harvard Dataverse, doi:10.7910/DVN/7V6JDO | Concepts, relationships with study and finding counts, findings, studies | `data/raw/graph/` (the rows we use) |
| System Public Graph API v1, `https://public-api.system.com` | A cross-check of link counts, in the minutes the API answered | `data/raw/api_check/` |
| World Bank Indicators API v2, `https://api.worldbank.org/v2/` | Indicator code, name, definition and source for three datasets | `data/raw/worldbank/` |
| PubMed E-utilities | Paper title, journal, year and first author, by PubMed ID | `data/raw/pubmed/` |

**Why the release files and not the API.** On build day the System API returned HTTP 500 for most requests. The relationships, findings and studies therefore come from the published release of the same graph. `scripts/pipeline.py graph` and `evidence` use the API when run without `--local`. When the API answered, it gave higher counts than the release for some links (for example 29 studies against 22 for internet use and depressive disorder), and no links at all for the unemployment concept. The app shows the release numbers throughout.

**World Bank datasets checked:** World Development Indicators (1,498 indicators), Enterprise Surveys (115), and Women, Business and the Law (153, taken from the Gender Statistics database, where they are credited to Women, Business and the Law). **B-READY is not checked:** it is not in the World Bank Indicators API.

## How the data is built

```
python scripts/pipeline.py worldbank          # indicator lists
python scripts/pipeline.py candidates         # step 1: concepts matching each topic
python scripts/pipeline.py graph --local      # step 2: each concept's links
python scripts/hand_check.py                  # step 4: record the hand-checked matches
python scripts/pipeline.py evidence --local   # step 6: findings and papers
python scripts/pipeline.py titles             # paper titles
python scripts/pipeline.py build              # tier, match, flag -> app/data.js
```

The pipeline needs Python 3.11 or later and `httpx` (`pip install -r requirements.txt`). `--local` reads the release files from `data/dataverse/` (about 1.1 GB, not in the repo; download them from the DOI above). The API stages need a `.env` file in this folder containing `SYSTEM_API_KEY=your-key`.

1. **Topic to concept.** Each topic's concept was picked by hand from the search results and saved in `data/topics.json`. The graph has no concept named "access to finance"; the closest is "financial inclusion", and the page says so.
2. **Relationships.** Every relationship where the concept is the source or the target, with its study and finding counts.
3. **Tiers.** See below.
4. **Indicator match.** A script proposes indicators whose name contains the concept's label or an alias. Each proposal for a concept shown in the app was then read and accepted, replaced or rejected. The decisions are in `scripts/hand_check.py` and `data/match_review.json`.
5. **Flags.** "Different dataset" when the indicator's dataset is not the topic's own. "Not in World Bank data" when no indicator was accepted.
6. **Findings.** For every link shown, all its findings and studies. The panel shows up to five findings that report a statistic, at most two per study, those with a confidence interval or p-value first.

## The tiering rule

Set after looking at the real study counts (most links rest on a single study):

| Topic | Links | 10+ studies | 3 to 9 | 1 or 2 |
|---|---|---|---|---|
| Unemployment | 719 | 16 | 98 | 605 |
| Internet use | 328 | 4 | 31 | 293 |
| Access to finance | 13 | 0 | 0 | 13 |

- **Link tier:** strong = 10 or more studies, moderate = 3 to 9, thin = 1 or 2.
- **Topic label:** thin if fewer than 3 of its links are moderate or strong; strong if at least 5 links are strong; otherwise moderate.

## Which links are shown, and in what order

- Shown: the 25 most-studied links in each direction, plus every other link whose concept has a hand-checked World Bank indicator. The page states how many of the total are shown.
- Default order: links whose indicator is in a different dataset first, then by study count. The "Most studied" toggle orders by study count only.
- The summary line counts only the links shown. Its study count is the number of distinct studies behind them.

## Known limits

- **Three topics, three datasets.** B-READY and the Women, Business and the Law topic are not built. The coverage table on the home page is not built.
- **Direction.** The two lists follow the source and target recorded in the graph. System's documentation lists wrong direction as a known extraction error, so read the lists as "studied in this direction", not as proof that one leads to the other.
- **Matching is by name and judgement.** A concept is matched to one indicator. Many concepts are broader or narrower than the indicator (for example "education" is matched to lower-secondary attainment). Where indicators in two datasets fitted equally well, the one outside World Development Indicators was kept; those cases carry a note in `scripts/hand_check.py`. The matches were not validated by a subject expert.
- **"Not in World Bank data"** means not found in the three datasets checked. The data may exist elsewhere.
- **The graph leans towards health research.** Most well-studied links for these topics are health outcomes. The direct links from internet use to jobs measures (employment, unemployment, income, exports) each rest on one study.
- **Findings are machine-extracted.** They are shown as recorded and not re-checked against the papers.
- **Release against live API.** Counts are from the July 2026 release and can be lower than the live API.

## Licence

Data derived from the System Public Graph is licensed CC BY-NC 4.0. Cite: System, Inc. (2026). System Public Graph (Version 2.1) [Data set]. https://doi.org/10.7910/DVN/7V6JDO. World Bank indicator metadata is from the World Bank Indicators API (CC BY 4.0).

## AI-tool disclosure

The pipeline, the app and this README were written with Claude Code (Anthropic) under the team's direction. The concept-to-indicator matches were proposed by a script and reviewed by the AI assistant; the team spot-checks them before the demo. The app itself makes no AI or network calls: it reads one saved file.
