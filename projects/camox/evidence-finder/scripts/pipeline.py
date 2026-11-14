#!/usr/bin/env python
"""CAMOX data pipeline. Run once; the app reads only app/data.js and needs no network.

    .venv/bin/python scripts/pipeline.py worldbank   # World Bank indicator lists (no key)
    .venv/bin/python scripts/pipeline.py graph       # relationships per topic (needs SYSTEM_API_KEY)
    .venv/bin/python scripts/pipeline.py evidence    # findings and papers for the links shown
    .venv/bin/python scripts/pipeline.py titles      # paper titles from PubMed (optional, no key)
    .venv/bin/python scripts/pipeline.py review      # print indicator matches for hand-checking
    .venv/bin/python scripts/pipeline.py find WORDS  # search the saved indicator lists
    .venv/bin/python scripts/pipeline.py build       # offline: tier, match, flag -> app/data.js

Every stage except `build`, `review` and `find` saves the raw API responses under data/raw/.
`build` reads only those saved files.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke import Api, ApiError, fetch_relationships, neighbor  # noqa: E402
from spg import config  # noqa: E402

ROOT = config.PROJECT_ROOT
RAW = ROOT / "data" / "raw"
TOPICS_FILE = ROOT / "data" / "topics.json"
REVIEW_FILE = ROOT / "data" / "match_review.json"
OUT = ROOT / "app" / "data.js"
DV = ROOT / "data" / "dataverse"  # the published release of the same graph (doi:10.7910/DVN/7V6JDO)
DV_VERSION = "System Public Graph v2.1 (Harvard Dataverse, doi:10.7910/DVN/7V6JDO)"
csv.field_size_limit(10**9)

# World Bank datasets we check. `credit` keeps only indicators whose source organisation names it.
DATASETS = {
    "WDI": {"name": "World Development Indicators", "short": "WDI", "source": 2, "credit": None},
    "ES": {"name": "Enterprise Surveys", "short": "", "source": 13, "credit": None},
    "WBL": {"name": "Women, Business and the Law", "short": "WBL", "source": 14, "credit": "Women, Business and the Law"},
}

# Tiering rule (see README). Set after looking at the real study counts.
STRONG_MIN = 10  # studies
MODERATE_MIN = 3
# A topic is thin when fewer than this many of its links reach the moderate tier.
TOPIC_THIN_BELOW = 3
TOPIC_STRONG_AT = 5  # links in the strong tier
PER_DIRECTION = 25  # most-studied links shown per topic in each direction
AUTO_ACCEPT = 0.45  # name-match score needed when a match has not been hand-checked
FINDINGS_SHOWN = 5
PAGE = 100


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_topics() -> list[dict]:
    return json.loads(TOPICS_FILE.read_text())["topics"]


def system_api() -> Api:
    client = httpx.Client(
        base_url=config.BASE_URL + config.API_PREFIX,
        headers={config.API_KEY_HEADER: config.api_key()},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    return Api(client)


def tier_of(studies: int) -> str:
    return "strong" if studies >= STRONG_MIN else "moderate" if studies >= MODERATE_MIN else "thin"


def rel_file(concept_id: str) -> Path:
    return RAW / "graph" / f"{concept_id}.relationships.json"


def evidence_file(relationship_id: str) -> Path:
    return RAW / "graph" / "evidence" / (relationship_id.replace("|", "__") + ".json")


def direction_of(row: dict, concept_id: str) -> str:
    """'out' when the graph records the topic as the source of the link, 'in' when it is the target."""
    return "out" if (row.get("source_concept") or {}).get("system_id") == concept_id else "in"


def usable_rows(rel: dict) -> list[dict]:
    """A topic's links without links from the concept to itself."""
    cid = rel["concept"]["system_id"]
    return [r for r in rel["rows"] if neighbor(r, cid)[0].get("system_id") not in (None, cid)]


def shown_rows(rel: dict) -> list[dict]:
    """The links a topic page shows: the PER_DIRECTION most-studied in each direction, plus every
    other link whose concept has a hand-checked World Bank indicator (data/match_review.json)."""
    cid = rel["concept"]["system_id"]
    matched = {k for k, v in load_review().items() if v.get("indicator")}
    rows = sorted(usable_rows(rel), key=lambda r: (-(r.get("num_studies") or 0), -(r.get("num_findings") or 0), r["relationship_id"]))
    out = []
    for d in ("out", "in"):
        side = [r for r in rows if direction_of(r, cid) == d]
        out += side[:PER_DIRECTION] + [r for r in side[PER_DIRECTION:] if neighbor(r, cid)[0]["system_id"] in matched]
    return out


# --- stage: worldbank ---------------------------------------------------------------


def wb_get(http: httpx.Client, url: str, params: dict) -> list:
    last: Exception | None = None
    for attempt in range(5):
        try:
            r = http.get(url, params=params)
            r.raise_for_status()
            body = r.json()
            if isinstance(body, list) and len(body) == 2:
                return body
            last = RuntimeError(f"unexpected body: {str(body)[:200]}")
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
        print(f"    retry {attempt + 1}: {type(last).__name__}", flush=True)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"World Bank API failed for {url}: {last}")


def stage_worldbank(args: argparse.Namespace) -> None:
    out_dir = RAW / "worldbank"
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0)) as http:
        for key, ds in DATASETS.items():
            path = out_dir / f"source_{ds['source']}.json"
            if path.is_file() and not args.refresh:
                print(f"  {ds['name']}: already saved")
                continue
            url = f"https://api.worldbank.org/v2/source/{ds['source']}/indicator"
            rows, page, pages = [], 1, 1
            while page <= pages:
                meta, data = wb_get(http, url, {"format": "json", "per_page": 250, "page": page})
                pages = int(meta.get("pages") or 1)
                rows += data or []
                print(f"  {ds['name']}: page {page}/{pages}, {len(rows)} indicators", flush=True)
                page += 1
            path.write_text(json.dumps({"fetched_at": now(), "url": url, "indicators": rows}, indent=1) + "\n")


# --- indicator catalogue and matching -----------------------------------------------


def load_catalog() -> list[dict]:
    seen: set[str] = set()
    out = []
    for key, ds in DATASETS.items():
        path = RAW / "worldbank" / f"source_{ds['source']}.json"
        if not path.is_file():
            print(f"  WARNING: {ds['name']} not downloaded; run the worldbank stage")
            continue
        for ind in json.loads(path.read_text())["indicators"]:
            if ds["credit"] and ds["credit"].lower() not in (ind.get("sourceOrganization") or "").lower():
                continue
            if ind["id"] in seen or not (ind.get("name") or "").strip():
                continue
            seen.add(ind["id"])
            out.append(
                {"code": ind["id"], "name": ind["name"].strip(), "definition": (ind.get("sourceNote") or "").strip(), "dataset": key}
            )
    return out


WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an the of in on at to for and or by with from per as is are be that this it its who which wbl".split())
GENERIC = set(
    "rate ratio index level number share percent percentage population people person male female men women age year "
    "total value score time use access growth change status risk factor measure country world group other".split()
)
QUALIFIERS = set("female male women men girl boy youth young rural urban adult child children elderly".split())


def stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "shes", "ches", "xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokens(text: str) -> list[str]:
    return [stem(w) for w in WORD.findall(text.lower()) if w not in STOP]


def build_index(catalog: list[dict]) -> list[dict]:
    index = []
    for ind in catalog:
        name = tokens(ind["name"])
        core = tokens(re.split(r"\(", ind["name"], maxsplit=1)[0])
        index.append(
            {
                **ind,
                "name_set": set(name),
                "name_str": " " + " ".join(name) + " ",
                "core_set": set(core) or set(name),
                "def_str": " " + " ".join(tokens(ind["definition"])) + " ",
            }
        )
    return index


def propose(concept: dict, index: list[dict], k: int = 3) -> list[dict]:
    """Indicators whose name (or, more weakly, definition) contains the concept's label or an alias."""
    phrases = [(concept.get("wikidata_label") or "", 1.0)]
    phrases += [(a, 0.8) for a in (concept.get("wikidata_aliases") or [])[:40]]
    best: dict[str, dict] = {}
    for text, weight in phrases:
        p = tokens(text)
        pset = set(p)
        if not p or len(p) > 5 or pset <= GENERIC or all(len(t) < 3 for t in p):
            continue
        pstr = " " + " ".join(p) + " "
        for ind in index:
            if pset <= ind["name_set"]:
                score = weight * len(pset) / len(ind["core_set"] | pset)
                score *= 1.0 if pstr in ind["name_str"] else 0.7
                score *= 1.1 if "total" in ind["name_set"] else 1.0
                score *= 0.8 if (ind["name_set"] & QUALIFIERS) - pset else 1.0
                how = "name"
            elif len(p) >= 2 and pstr in ind["def_str"]:
                score, how = weight * 0.25, "definition"
            else:
                continue
            if score > best.get(ind["code"], {"score": 0})["score"]:
                best[ind["code"]] = {"code": ind["code"], "score": round(score, 3), "how": how, "phrase": text}
    return sorted(best.values(), key=lambda m: (-m["score"], m["code"]))[:k]


def load_review() -> dict:
    if not REVIEW_FILE.is_file():
        return {}
    return json.loads(REVIEW_FILE.read_text()).get("decisions", {})


def decide(concept: dict, review: dict, index: list[dict]) -> tuple[str | None, bool]:
    """(indicator code or None, hand-checked?). A hand-check always wins over the automatic match."""
    cid = concept.get("system_id")
    if cid in review:
        return review[cid].get("indicator"), True
    top = propose(concept, index, 1)
    if top and top[0]["how"] == "name" and top[0]["score"] >= AUTO_ACCEPT:
        return top[0]["code"], False
    return None, False



# --- the published graph files (used when the API is unavailable) -------------------


def parse_aliases(text: str) -> list[str]:
    try:
        value = json.loads(text) if text else []
    except ValueError:
        value = []
    return [a for a in value if isinstance(a, str)] if isinstance(value, list) else []


def read_concepts():
    with open(DV / "concepts.csv", newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def rel_row(row: dict) -> dict:
    """A relationships.csv row in the same shape the API returns."""

    def side(p: str) -> dict:
        return {
            "system_id": row[f"{p}.system_id"],
            "wikidata_label": row[f"{p}.wikidata_label"],
            "wikidata_id": row[f"{p}.wikidata_id"],
            "wikidata_description": row[f"{p}.wikidata_description"] or None,
            "wikidata_aliases": parse_aliases(row[f"{p}.wikidata_aliases"]),
        }

    return {
        "relationship_id": row["relationship_id"],
        "num_findings": int(row["num_findings"] or 0),
        "num_studies": int(row["num_studies"] or 0),
        "last_published_date": row["last_published_date"] or None,
        "last_updated": row["last_updated"] or None,
        "source_concept": side("source_concept"),
        "target_concept": side("target_concept"),
    }


def scan_relationships(ids: set[str]) -> dict[str, list[dict]]:
    """One pass over relationships.csv: every row where a wanted concept is the source or the target."""
    out: dict[str, list[dict]] = {i: [] for i in ids}
    with open(DV / "relationships.csv", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        si, ti = header.index("source_concept.system_id"), header.index("target_concept.system_id")
        for rec in reader:
            s, t = rec[si], rec[ti]
            if s in ids or t in ids:
                row = rel_row(dict(zip(header, rec)))
                if s in ids:
                    out[s].append(row)
                if t in ids and t != s:
                    out[t].append(row)
    return out


def stage_candidates(args: argparse.Namespace) -> None:
    """Step 1: list graph concepts matching each topic's search words, with how much evidence each has."""
    topics = load_topics()
    wanted = args.words and [{"id": "adhoc", "name": " ".join(args.words), "search": " ".join(args.words)}] or topics
    per_topic: dict[str, list[dict]] = defaultdict(list)
    cands: dict[str, dict] = {}
    for c in read_concepts():
        label = (c["wikidata_label"] or "").lower()
        aliases = [a.lower() for a in parse_aliases(c["wikidata_aliases"])]
        for t in wanted:
            terms = [t["search"]] + t.get("also_search", [])
            if any(term.lower() in label or any(term.lower() in a for a in aliases) for term in terms):
                per_topic[t["id"]].append(c)
                cands[c["system_id"]] = c
    rels = scan_relationships(set(cands))
    for t in wanted:
        print(f"\n=== {t['name']} ===   chosen: {t.get('concept_id')}")
        rows = sorted(per_topic[t["id"]], key=lambda c: -len(rels[c["system_id"]]))
        print(f"  {'id':11} {'links':>6} {'3+ st.':>6} {'max st.':>7}  label - description")
        for c in rows[:25]:
            counts = [r["num_studies"] for r in rels[c["system_id"]]]
            print(
                f"  {c['system_id']:11} {len(counts):>6} {sum(n >= 3 for n in counts):>6} {max(counts, default=0):>7}  "
                f"{c['wikidata_label']} - {(c['wikidata_description'] or '')[:60]}"
            )
        if len(rows) > 25:
            print(f"  ... {len(rows) - 25} more with fewer links")


# --- stage: graph -------------------------------------------------------------------


def stage_graph(args: argparse.Namespace) -> None:
    """Step 2: each topic concept's relationships. From the API, or with --local from the published files."""
    (RAW / "graph").mkdir(parents=True, exist_ok=True)
    topics = load_topics()
    todo = [t["concept_id"] for t in topics if t.get("concept_id") and (args.refresh or not rel_file(t["concept_id"]).is_file())]
    if todo and args.local:
        concepts = {c["system_id"]: c for c in read_concepts() if c["system_id"] in todo}
        rels = scan_relationships(set(todo))
        for cid in todo:
            c = concepts[cid]
            concept = {
                "system_id": cid, "wikidata_label": c["wikidata_label"], "wikidata_id": c["wikidata_id"],
                "wikidata_description": c["wikidata_description"] or None, "wikidata_aliases": parse_aliases(c["wikidata_aliases"]),
            }
            rel = {"fetched_at": now(), "source": DV_VERSION, "concept": concept, "total": len(rels[cid]), "rows": rels[cid]}
            rel_file(cid).write_text(json.dumps(rel, indent=1) + "\n")
    elif todo:
        api = system_api()
        for cid in todo:
            concept = api.get(f"/concepts/{quote(cid, safe='')}")
            rows, total = fetch_relationships(api, cid)
            rel = {"fetched_at": now(), "source": "System Public Graph API v1", "concept": concept, "total": total, "rows": rows}
            rel_file(cid).write_text(json.dumps(rel, indent=1) + "\n")
    for t in topics:
        cid = t.get("concept_id")
        if not cid:
            print(f"  {t['name']}: no concept chosen; it will show the thin state")
            continue
        rel = json.loads(rel_file(cid).read_text())
        counts = sorted((r.get("num_studies") or 0 for r in rel["rows"]), reverse=True)
        print(f"  {t['name']}: concept '{rel['concept'].get('wikidata_label')}' ({cid}), {rel['total']} links, source: {rel.get('source')}")
        if counts:
            dist = {k: sum(c >= k for c in counts) for k in (1, 2, 3, 5, 10, 20, 50)}
            print(f"    studies per link: max {counts[0]}, median {statistics.median(counts)}; links with at least N studies: {dist}")


# --- stage: evidence ----------------------------------------------------------------


def get_list(api: Api, path: str, limit: int) -> dict:
    """One page of a list route; steps the page size down if the API rejects it."""
    for size in (limit, 50, 10):
        try:
            return api.get(path, {"limit": size, "include_total": "true"})
        except ApiError as exc:
            if exc.status != 422 or size == 10:
                raise
    raise AssertionError("unreachable")


def num_or_none(text: str):
    return text if text not in ("", None) else None


def stage_evidence_local() -> None:
    """Findings and papers for the links shown, read from associations.csv and studies.csv."""
    (RAW / "graph" / "evidence").mkdir(parents=True, exist_ok=True)
    wanted: dict[tuple[str, str], dict] = {}
    for t in load_topics():
        cid = t.get("concept_id")
        if cid and rel_file(cid).is_file():
            for row in shown_rows(json.loads(rel_file(cid).read_text())):
                a, b = row["relationship_id"].split("|")
                wanted[(a, b)] = row
    found: dict[str, list[dict]] = defaultdict(list)
    with open(DV / "associations.csv", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        i1, i2 = header.index("variable_1_concept_id"), header.index("variable_2_concept_id")
        for rec in reader:
            row = wanted.get((rec[i1], rec[i2]))
            if row is not None:
                a = {k: num_or_none(v) for k, v in zip(header, rec)}
                a["statistic_type"] = a["statistic_type"].strip() if a["statistic_type"] else None
                found[row["relationship_id"]].append(a)
    study_ids = {a["study_id"] for rows in found.values() for a in rows if a["study_id"]}
    studies: dict[str, dict] = {}
    with open(DV / "studies.csv", newline="", encoding="utf-8") as f:
        for srow in csv.DictReader(f):
            if srow["study_id"] in study_ids:
                studies[srow["study_id"]] = {
                    "study_id": srow["study_id"], "doi": srow["doi"] or None, "pubmed_id": srow["pubmed_id"] or None,
                    "population": srow["population_string"] or None, "study_type": srow["study_type"] or None,
                }
    mismatched = 0
    for row in wanted.values():
        rid = row["relationship_id"]
        assoc = found.get(rid, [])
        ids = list(dict.fromkeys(a["study_id"] for a in assoc if a["study_id"]))
        if len(assoc) != row["num_findings"] or len(ids) != row["num_studies"]:
            mismatched += 1
            print(f"    count differs for {rid}: relationship says {row['num_studies']} studies / {row['num_findings']} findings, files hold {len(ids)} / {len(assoc)}")
        body = {
            "relationship_id": rid, "fetched_at": now(), "source": DV_VERSION,
            "associations": {"data": assoc, "total": len(assoc)},
            "studies": {"data": [studies[i] for i in ids if i in studies], "total": len(ids)},
        }
        evidence_file(rid).write_text(json.dumps(body, indent=1) + "\n")
    print(f"  saved evidence for {len(wanted)} links; {mismatched} with counts that differ from the relationship row")


def stage_evidence(args: argparse.Namespace) -> None:
    if args.local:
        return stage_evidence_local()
    api = system_api()
    (RAW / "graph" / "evidence").mkdir(parents=True, exist_ok=True)
    todo = []
    for t in load_topics():
        cid = t.get("concept_id")
        if cid and rel_file(cid).is_file():
            for row in shown_rows(json.loads(rel_file(cid).read_text())):
                if args.refresh or not evidence_file(row["relationship_id"]).is_file():
                    todo.append(row["relationship_id"])
    todo = list(dict.fromkeys(todo))
    print(f"  {len(todo)} links to fetch ({2 * len(todo)} requests)")

    def fetch(rid: str) -> str:
        base = f"/relationships/{quote(rid, safe='')}"
        try:
            body = {
                "relationship_id": rid,
                "fetched_at": now(),
                "associations": get_list(api, base + "/associations", PAGE),
                "studies": get_list(api, base + "/studies", PAGE),
            }
        except (ApiError, httpx.HTTPError) as exc:
            return f"FAILED {rid}: {exc}"
        evidence_file(rid).write_text(json.dumps(body, indent=1) + "\n")
        return "ok"

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(fetch, todo))
    failed = [r for r in results if r != "ok"]
    print(f"  saved {len(results) - len(failed)}, failed {len(failed)}")
    for line in failed[:10]:
        print("   ", line)


# --- stage: titles ------------------------------------------------------------------


def stage_titles(args: argparse.Namespace) -> None:
    """Paper titles from PubMed for the studies we show. The graph API returns only DOI and PubMed ID."""
    path = RAW / "pubmed" / "summaries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = json.loads(path.read_text())["papers"] if path.is_file() else {}
    ids: set[str] = set()
    for f in (RAW / "graph" / "evidence").glob("*.json"):
        for s in json.loads(f.read_text())["studies"].get("data", []):
            if s.get("pubmed_id"):
                ids.add(str(s["pubmed_id"]))
    todo = sorted(ids - set(saved))
    print(f"  {len(ids)} PubMed IDs, {len(todo)} to fetch")
    with httpx.Client(timeout=60.0) as http:
        for i in range(0, len(todo), 150):
            batch = todo[i : i + 150]
            try:
                r = http.post(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    data={"db": "pubmed", "retmode": "json", "id": ",".join(batch)},
                )
                r.raise_for_status()
                result = r.json().get("result", {})
            except (httpx.HTTPError, ValueError) as exc:
                print(f"    batch {i // 150 + 1} failed: {type(exc).__name__}; titles for it are left out")
                continue
            for pmid in batch:
                item = result.get(pmid) or {}
                if item.get("title"):
                    authors = item.get("authors") or []
                    saved[pmid] = {
                        "title": item["title"].strip(),
                        "journal": item.get("source") or "",
                        "year": (item.get("pubdate") or "")[:4],
                        "first_author": authors[0]["name"] if authors else "",
                    }
            path.write_text(json.dumps({"fetched_at": now(), "papers": saved}, indent=1) + "\n")
            time.sleep(0.4)
    print(f"  {len(saved)} titles saved")



# --- stage: verify ------------------------------------------------------------------


def stage_verify(args: argparse.Namespace) -> None:
    """Cross-check the saved links against the live System API and keep the API's raw answers."""
    api = system_api()
    out_dir = RAW / "api_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    for t in load_topics():
        cid = t.get("concept_id")
        if not (cid and rel_file(cid).is_file()):
            continue
        saved = {r["relationship_id"]: r for r in json.loads(rel_file(cid).read_text())["rows"]}
        try:
            rows, total = fetch_relationships(api, cid)
        except (ApiError, httpx.HTTPError) as exc:
            print(f"  {t['name']}: API check failed: {exc}")
            continue
        (out_dir / f"{cid}.relationships.json").write_text(
            json.dumps({"fetched_at": now(), "source": "System Public Graph API v1", "total": total, "rows": rows}, indent=1) + "\n"
        )
        live = {r["relationship_id"]: r for r in rows}
        same = sum(
            1 for k, r in live.items()
            if k in saved and r.get("num_studies") == saved[k]["num_studies"] and r.get("num_findings") == saved[k]["num_findings"]
        )
        differ = [k for k, r in live.items() if k in saved and (r.get("num_studies") != saved[k]["num_studies"] or r.get("num_findings") != saved[k]["num_findings"])]
        print(
            f"  {t['name']}: API reports {total} links, {len(live)} fetched; saved files hold {len(saved)}. "
            f"Same study and finding counts: {same}; different counts: {len(differ)}; "
            f"only in API: {len(set(live) - set(saved))}; only in saved files: {len(set(saved) - set(live))}"
        )
        for k in differ[:5]:
            print(f"      {k}: API {live[k].get('num_studies')} studies / {live[k].get('num_findings')} findings, saved {saved[k]['num_studies']} / {saved[k]['num_findings']}")


# --- stage: review / find -----------------------------------------------------------


def stage_review(args: argparse.Namespace) -> None:
    catalog = load_catalog()
    by_code = {c["code"]: c for c in catalog}
    index, review = build_index(catalog), load_review()
    seen: set[str] = set()
    for t in load_topics():
        cid = t.get("concept_id")
        if not (cid and rel_file(cid).is_file()):
            continue
        print(f"\n=== {t['name']} ===")
        for row in shown_rows(json.loads(rel_file(cid).read_text())):
            other, _ = neighbor(row, cid)
            if other["system_id"] in seen:
                continue
            seen.add(other["system_id"])
            code, checked = decide(other, review, index)
            if args.unchecked and checked:
                continue
            state = "checked" if checked else "auto"
            chosen = f"{code} {by_code[code]['name'][:70]}" if code in by_code else "none"
            desc = (other.get("wikidata_description") or "")[:50]
            print(f"{other['system_id']} | {other.get('wikidata_label')} ({desc}) | {direction_of(row, cid)} {row['num_studies']} st | {state}: {chosen}")
            for m in propose(other, index):
                c = by_code[m["code"]]
                print(f"      {m['score']:.2f} {m['how'][:4]} [{c['dataset']}] {m['code']} {c['name'][:80]}  <- '{m['phrase']}'")


def stage_find(args: argparse.Namespace) -> None:
    words = [w.lower() for w in args.words]
    for c in load_catalog():
        if all(w in c["name"].lower() for w in words):
            print(f"[{c['dataset']}] {c['code']}\t{c['name']}")


# --- stage: build -------------------------------------------------------------------


def indicator_view(ind: dict | None) -> dict | None:
    return {"code": ind["code"], "name": ind["name"], "dataset": ind["dataset"]} if ind else None


def finding_view(a: dict) -> dict:
    keys = (
        "variable_1_name", "variable_2_name", "statistic_type", "statistic_value",
        "ci_lower", "ci_upper", "ci_level", "p_value", "p_value_equality", "study_id", "pubmed_id",
    )
    return {k: a.get(k) for k in keys}


def pick_findings(associations: list[dict]) -> list[dict]:
    """Up to FINDINGS_SHOWN findings that report an effect size, at most two per study, fullest first."""
    usable = [a for a in associations if a.get("statistic_value") is not None]

    def fullness(a: dict) -> int:
        return (a.get("ci_lower") is not None and a.get("ci_upper") is not None) + (a.get("p_value") is not None)

    usable.sort(key=fullness, reverse=True)  # stable: API order within the same fullness
    per_study: dict[str, int] = {}
    out = []
    for a in usable:
        sid = a.get("study_id") or a.get("system_id")
        if per_study.get(sid, 0) >= 2:
            continue
        per_study[sid] = per_study.get(sid, 0) + 1
        out.append(finding_view(a))
        if len(out) == FINDINGS_SHOWN:
            break
    return out


def paper_view(s: dict, titles: dict) -> dict:
    meta = titles.get(str(s.get("pubmed_id")), {}) if s.get("pubmed_id") else {}
    doi = (s.get("doi") or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    url = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{s['pubmed_id']}/" if s.get("pubmed_id") else None
    return {
        "study_id": s.get("study_id"),
        "url": url,
        "doi": doi or None,
        "pubmed_id": s.get("pubmed_id"),
        "study_type": s.get("study_type"),
        "title": meta.get("title"),
        "journal": meta.get("journal"),
        "year": meta.get("year"),
        "first_author": meta.get("first_author"),
    }


def stage_build(args: argparse.Namespace) -> None:
    catalog = load_catalog()
    by_code = {c["code"]: c for c in catalog}
    index, review = build_index(catalog), load_review()
    titles_path = RAW / "pubmed" / "summaries.json"
    titles = json.loads(titles_path.read_text())["papers"] if titles_path.is_file() else {}
    sources = set()

    topics_out = []
    for t in load_topics():
        own = by_code.get(t.get("indicator") or "")
        if t.get("indicator") and not own:
            print(f"  WARNING: {t['name']}: indicator {t['indicator']} is not in the saved lists")
        topic = {
            "id": t["id"], "name": t["name"], "group": t["group"],
            "indicator": indicator_view(own), "concept": None, "evidence": "thin",
            "total_links": {"out": 0, "in": 0}, "tier_counts": {"strong": 0, "moderate": 0, "thin": 0},
            "summary": None, "links": [],
        }
        cid = t.get("concept_id")
        if cid and rel_file(cid).is_file():
            rel = json.loads(rel_file(cid).read_text())
            sources.add(rel.get("source"))
            c = rel["concept"]
            topic["concept"] = {
                "id": cid, "label": c.get("wikidata_label"), "description": c.get("wikidata_description"),
                "aliases": (c.get("wikidata_aliases") or [])[:20],
            }
            for r in usable_rows(rel):
                topic["total_links"][direction_of(r, cid)] += 1
                topic["tier_counts"][tier_of(r.get("num_studies") or 0)] += 1
            well = topic["tier_counts"]["strong"] + topic["tier_counts"]["moderate"]
            topic["evidence"] = (
                "thin" if well < TOPIC_THIN_BELOW else "strong" if topic["tier_counts"]["strong"] >= TOPIC_STRONG_AT else "moderate"
            )
            study_ids: set[str] = set()
            for row in shown_rows(rel):
                other, _ = neighbor(row, cid)
                code, checked = decide(other, review, index)
                ind = by_code.get(code) if code else None
                link = {
                    "id": row["relationship_id"],
                    "direction": direction_of(row, cid),
                    "concept_id": other["system_id"],
                    "name": other.get("wikidata_label") or other["system_id"],
                    "description": other.get("wikidata_description"),
                    "aliases": (other.get("wikidata_aliases") or [])[:8],
                    "studies": row.get("num_studies") or 0,
                    "findings_count": row.get("num_findings") or 0,
                    "last_published": (row.get("last_published_date") or "")[:4] or None,
                    "tier": tier_of(row.get("num_studies") or 0),
                    "indicator": indicator_view(ind),
                    "match_checked": checked,
                    "different_dataset": bool(ind and own and ind["dataset"] != own["dataset"]),
                    "findings": [], "papers": [], "evidence_saved": False,
                }
                ev_path = evidence_file(row["relationship_id"])
                if ev_path.is_file():
                    ev = json.loads(ev_path.read_text())
                    link["evidence_saved"] = True
                    link["findings"] = pick_findings(ev["associations"].get("data", []))
                    link["papers"] = [paper_view(s, titles) for s in ev["studies"].get("data", [])]
                    study_ids.update(a["study_id"] for a in ev["associations"].get("data", []) if a.get("study_id"))
                topic["links"].append(link)
            inds = {l["indicator"]["code"]: l["indicator"]["dataset"] for l in topic["links"] if l["indicator"]}
            complete = all(l["evidence_saved"] for l in topic["links"])
            topic["summary"] = {
                "linked_concepts": len({l["concept_id"] for l in topic["links"]}),
                "studies": len(study_ids) if complete else None,  # distinct studies behind the links shown
                "indicators": len(inds),
                "datasets": len(set(inds.values())),
                "outside": sum(1 for d in inds.values() if own and d != own["dataset"]),
                "not_found": len({l["concept_id"] for l in topic["links"] if not l["indicator"]}),
            }
        topics_out.append(topic)
        print(
            f"  {topic['name']}: {topic['evidence']} {topic['tier_counts']}; {len(topic['links'])} links shown of "
            f"{topic['total_links']}; summary {topic['summary']}; "
            f"{sum(l['match_checked'] for l in topic['links'])} matches hand-checked"
        )

    data = {
        "generated_at": now(),
        "graph_source": sorted(x for x in sources if x),
        "datasets": {k: {"name": d["name"], "short": d["short"]} for k, d in DATASETS.items()},
        "tier_rule": {
            "strong_min": STRONG_MIN, "moderate_min": MODERATE_MIN,
            "topic_thin_below": TOPIC_THIN_BELOW, "topic_strong_at": TOPIC_STRONG_AT, "per_direction": PER_DIRECTION,
        },
        "groups": ["Job outcomes", "Foundations", "Business environment", "Private capital"],
        "topics": topics_out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("// Generated by scripts/pipeline.py build. Do not edit by hand.\nwindow.CAMOX_DATA = " + json.dumps(data, indent=1) + ";\n")
    print(f"  wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


STAGES = {
    "worldbank": stage_worldbank, "candidates": stage_candidates, "graph": stage_graph, "evidence": stage_evidence, "titles": stage_titles,
    "verify": stage_verify, "review": stage_review, "find": stage_find, "build": stage_build,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=list(STAGES))
    parser.add_argument("words", nargs="*", help="for `find`: words that must all be in the indicator name")
    parser.add_argument("--refresh", action="store_true", help="fetch again even if a saved response exists")
    parser.add_argument("--local", action="store_true", help="read the published graph files in data/dataverse instead of the API")
    parser.add_argument("--unchecked", action="store_true", help="for `review`: only links not yet hand-checked")
    args = parser.parse_args()
    print(f"== {args.stage} ==")
    try:
        STAGES[args.stage](args)
    except config.MissingApiKey as exc:
        print(f"Cannot run: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
