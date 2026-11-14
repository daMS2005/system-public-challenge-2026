#!/usr/bin/env python
"""Smoke test: search the System Public Graph for a topic and print its relationships with study counts.

    .venv/bin/python scripts/smoke.py                         # "internet use"
    .venv/bin/python scripts/smoke.py "unemployment"
    .venv/bin/python scripts/smoke.py "internet use" --concept-id <system_id> --top 100

Every number printed comes straight from the API. Raw responses are saved to
data/raw/smoke/<topic>.json. The API key is read from SYSTEM_API_KEY (.env) and is
never printed or saved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spg import config  # noqa: E402

PAGE = 100  # largest page size is undocumented; falls back to 50, then 10, on a 422
MAX_ROWS = 2000
RETRIES = int(os.environ.get("SPG_RETRIES", "4"))  # raise when the API is unstable
MAX_CANDIDATES = 15
BINS = [(20, None), (10, 19), (5, 9), (2, 4), (1, 1)]  # for reading the spread only, not the tier rule


class ApiError(RuntimeError):
    def __init__(self, status: int, path: str, body: object):
        super().__init__(f"{status} from {path}: {json.dumps(body)[:300]}")
        self.status = status


class Api:
    """GETs that keep every response so the run can be saved and re-read offline."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.calls: list[dict] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(RETRIES):  # the API returns intermittent 500s; retry those and timeouts
            try:
                r = self.client.get(path, params=params)
            except httpx.TransportError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(min(15.0, 1.5 * (attempt + 1)))
                continue
            if r.status_code < 500 or attempt == RETRIES - 1:
                break
            time.sleep(min(15.0, 1.5 * (attempt + 1)))
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        self.calls.append({"path": path, "params": params or {}, "status": r.status_code, "body": body})
        if r.status_code != 200:
            raise ApiError(r.status_code, path, body)
        return body


def search_concepts(api: Api, term: str) -> list[dict]:
    """Concepts whose label or aliases match `term`. Label hits come first."""
    found: dict[str, dict] = {}
    for field in ("wikidata_label", "wikidata_aliases"):
        try:
            body = api.get("/concepts", {"search": f"{field}:{term}", "limit": 25, "include_total": "true"})
        except ApiError as exc:  # the alias search returns 500 for some terms; the label search still stands
            print(f"  ({field} search failed with {exc.status}; skipped)")
            continue
        for row in body.get("data", []):
            found.setdefault(row["system_id"], {**row, "matched_on": field})
    exact = term.strip().lower()
    return sorted(found.values(), key=lambda c: (c.get("wikidata_label") or "").lower() != exact)


def relationship_total(api: Api, concept_id: str) -> int | None:
    body = api.get(f"/concepts/{quote(concept_id, safe='')}/relationships", {"limit": 1, "include_total": "true"})
    return body.get("total")


def fetch_relationships(api: Api, concept_id: str) -> tuple[list[dict], int | None]:
    """All relationships of a concept, most-studied first."""
    path = f"/concepts/{quote(concept_id, safe='')}/relationships"
    rows: dict[str, dict] = {}
    total, page, offset = None, PAGE, 0
    while offset < MAX_ROWS:
        params = {"sort": "num_studies:desc", "limit": page, "offset": offset, "include_total": "true"}
        try:
            body = api.get(path, params)
        except ApiError as exc:
            if exc.status == 422 and offset == 0 and page > 10:
                page = 50 if page > 50 else 10
                continue
            raise
        data = body.get("data", [])
        total = body.get("total", total)
        for row in data:
            rows.setdefault(row["relationship_id"], row)
        offset += len(data)
        if len(data) < page or (total is not None and offset >= total):
            break
    return list(rows.values()), total


def neighbor(row: dict, concept_id: str) -> tuple[dict, str]:
    """The concept on the other side of the link, and which side the topic is on in the API row."""
    source, target = row.get("source_concept") or {}, row.get("target_concept") or {}
    if source.get("system_id") == concept_id:
        return target, "source"
    return source, "target"


def show_candidates(candidates: list[dict]) -> None:
    print(f"  {'id':12} {'links':>6}  {'matched on':16} label - description")
    for c in candidates:
        links = c.get("links")
        desc = (c.get("wikidata_description") or "")[:70]
        print(
            f"  {c['system_id']:12} {'?' if links is None else links:>6}  {c['matched_on']:16} "
            f"{c.get('wikidata_label')} - {desc}"
        )


def show_relationships(rows: list[dict], concept: dict, total: int | None, top: int) -> None:
    cid = concept["system_id"]
    print(f"\nRelationships of '{concept.get('wikidata_label')}' ({cid}): {total} reported by the API, {len(rows)} fetched")
    print("'topic is' = the side the API puts the topic on in that row (source or target).\n")
    print(f"  {'#':>3}  {'studies':>7}  {'findings':>8}  {'topic is':8}  {'last pub':8}  linked concept")
    for i, row in enumerate(rows[:top], 1):
        other, side = neighbor(row, cid)
        year = (row.get("last_published_date") or "")[:4] or "-"
        print(
            f"  {i:>3}  {row.get('num_studies', '-'):>7}  {row.get('num_findings', '-'):>8}  {side:8}  {year:8}  "
            f"{other.get('wikidata_label') or other.get('system_id') or '(no label)'}"
        )
    if len(rows) > top:
        print(f"  ... {len(rows) - top} more (rerun with --top {len(rows)} to see all)")

    counts = [r["num_studies"] for r in rows if isinstance(r.get("num_studies"), int)]
    if not counts:
        return
    print(f"\nStudy counts across {len(counts)} links: max {max(counts)}, median {statistics.median(counts)}, min {min(counts)}")
    for lo, hi in BINS:
        n = sum(c >= lo and (hi is None or c <= hi) for c in counts)
        label = f"{lo}+" if hi is None else str(lo) if lo == hi else f"{lo}-{hi}"
        print(f"  {label:>6} studies: {n:>4} links")
    sides = [neighbor(r, cid)[1] for r in rows]
    print(f"\nTopic appears as source in {sides.count('source')} rows and as target in {sides.count('target')} rows.")


def run(api: Api, term: str, concept_id: str | None, top: int) -> dict:
    print(f"Searching concepts for '{term}' (label, then aliases)")
    candidates = search_concepts(api, term)
    searched = term
    if not candidates and " " in term:
        searched = term.split()[0]
        print(f"  no concept matched '{term}'; falling back to '{searched}'")
        candidates = search_concepts(api, searched)
    if not candidates and not concept_id:
        print("  no concepts found. This topic would show the thin state.")
        return {"term": term, "searched": searched, "candidates": [], "picked": None}

    shown = candidates[:MAX_CANDIDATES]
    for c in shown:
        c["links"] = relationship_total(api, c["system_id"])
    show_candidates(shown)
    if len(candidates) > len(shown):
        print(f"  ... {len(candidates) - len(shown)} more candidates not shown")

    if concept_id:
        picked = next((c for c in candidates if c["system_id"] == concept_id), None)
        if picked is None:
            try:
                picked = api.get(f"/concepts/{quote(concept_id, safe='')}")
            except ApiError:  # the concept lookup is failing; carry on with the id alone
                picked = {"system_id": concept_id, "wikidata_label": term}
        how = "--concept-id"
    else:
        exact = [c for c in shown if (c.get("wikidata_label") or "").lower() == term.strip().lower()]
        picked = exact[0] if exact else max(shown, key=lambda c: c.get("links") or 0)
        how = "exact label match" if exact else "most links among candidates"
    print(f"\nPicked: {picked.get('wikidata_label')} ({picked['system_id']}) by {how}. Override with --concept-id.")

    rows, total = fetch_relationships(api, picked["system_id"])
    show_relationships(rows, picked, total, top)
    return {"term": term, "searched": searched, "candidates": candidates, "picked": picked, "picked_by": how}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("term", nargs="?", default="internet use")
    parser.add_argument("--concept-id", help="skip the automatic pick and use this concept")
    parser.add_argument("--top", type=int, default=40, help="rows to print (default 40); all rows are saved")
    args = parser.parse_args()

    try:
        key = config.api_key()
    except config.MissingApiKey as exc:
        print(f"Cannot run: {exc}")
        return 2

    client = httpx.Client(
        base_url=config.BASE_URL + config.API_PREFIX,
        headers={config.API_KEY_HEADER: key},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    api = Api(client)
    status = 0
    try:
        summary = run(api, args.term, args.concept_id, args.top)
    except (ApiError, httpx.HTTPError) as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        summary, status = {"term": args.term, "error": str(exc)}, 1

    out = config.DATA_DIR / "raw" / "smoke" / (re.sub(r"[^a-z0-9]+", "_", args.term.lower()).strip("_") + ".json")
    blob = json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **summary, "calls": api.calls}, indent=2
    )
    if key in blob:  # the key must never reach disk
        blob = blob.replace(key, "<redacted>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob + "\n")
    print(f"\n{len(api.calls)} API requests. Raw responses saved to {out.relative_to(config.PROJECT_ROOT)}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
