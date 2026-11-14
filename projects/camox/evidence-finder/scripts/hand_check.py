#!/usr/bin/env python
"""Hand-checked matches from graph concepts to World Bank indicators.

The automatic name match (pipeline.py `review`) proposes indicators; a person reads the proposals
and records the decision here, by concept label. Running this file writes data/match_review.json,
which `pipeline.py build` applies.

Every match carries a type, the same four the agent app uses:
  exact     the indicator measures the concept
  narrower  the indicator measures one part or one definition of the concept
  broader   the indicator covers more than the concept
  proxy     the indicator measures something related that stands in for the concept
and one sentence saying why, unless the match is exact.

A concept on a page with no match is recorded as one of:
  gap        nothing in the datasets we checked measures it
  breakdown  a way of splitting data (sex, age, group), not something one indicator measures
  broad      too general to match one indicator

Tie-break: where indicators in two datasets fit a concept equally well, the one outside
World Development Indicators is kept, and the reason names the alternative.

    python scripts/hand_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as P  # noqa: E402

PERSON_VS_COUNTRY = "Studies measure a person's or household's income; the indicator is national income per person."

# concept label (lower case) -> (indicator code, match type, why)
MATCHES: dict[str, tuple[str, str, str]] = {
    # jobs and the economy
    "unemployment": ("SL.UEM.TOTL.ZS", "exact", ""),
    "employment": ("SL.EMP.TOTL.SP.ZS", "narrower", "The employed share of people aged 15 and over; studies use many employment measures."),
    "economic growth": ("NY.GDP.MKTP.KD.ZG", "exact", ""),
    "gross domestic product": ("NY.GDP.MKTP.CD", "exact", ""),
    "income": ("NY.GNP.PCAP.CD", "proxy", PERSON_VS_COUNTRY),
    "income inequality": ("SI.POV.GINI", "exact", ""),
    "poverty": ("SI.POV.NAHC", "narrower", "One definition of poverty: the share below the national poverty line."),
    "inflation": ("FP.CPI.TOTL.ZG", "exact", ""),
    "investment": ("NE.GDI.TOTL.ZS", "narrower", "National investment only (gross capital formation)."),
    "fiscal policy": ("IQ.CPA.FISP.XQ", "proxy", "A 1 to 6 rating of fiscal policy quality, not the policy itself."),
    "industrial sector": ("NV.IND.TOTL.ZS", "proxy", "The size of the sector as a share of GDP."),
    "export": ("IC.FRM.TRD.TR17", "proxy", "The share of firms that export. Tie-break: WDI has 'Exports of goods and services (% of GDP)' (NE.EXP.GNFS.ZS)."),
    "theft": ("IC.FRM.CRM.CRIME10", "proxy", "Theft as firms experience it; no theft indicator in WDI."),
    "human migration": ("SM.POP.NETM", "narrower", "Net migration only: arrivals minus departures."),
    "refugee": ("SM.POP.RHCR.EA", "narrower", "Refugees under the UNHCR mandate, counted by country of asylum."),
    # education and connectivity
    "education": ("SE.SEC.CUAT.LO.ZS", "proxy", "One level of attainment stands in for education in general."),
    "educational stage": ("SE.SEC.CUAT.LO.ZS", "narrower", "One stage: at least lower secondary completed."),
    "literacy": ("SE.ADT.LITR.ZS", "exact", ""),
    "internet": ("IT.NET.BBND.P2", "proxy", "Internet availability; the topic's own indicator already measures use."),
    "internet access": ("IT.NET.BBND.P2", "narrower", "Fixed broadband only; mobile access is not counted."),
    "urban population": ("SP.URB.TOTL.IN.ZS", "exact", ""),
    "rural population": ("SP.RUR.TOTL.ZS", "exact", ""),
    # health and population
    "mortality rate": ("SP.DYN.CDRT.IN", "exact", ""),
    "life expectancy": ("SP.DYN.LE00.IN", "exact", ""),
    "infant mortality": ("SP.DYN.IMRT.IN", "exact", ""),
    "neonatal mortality": ("SH.DYN.NMRT", "exact", ""),
    "maternal mortality ratio": ("SH.STA.MMRT", "exact", ""),
    "maternal death": ("SH.STA.MMRT", "exact", ""),
    "suicide": ("SH.STA.SUIC.P5", "exact", ""),
    "homicide": ("VC.IHR.PSRC.P5", "exact", ""),
    "food insecurity": ("SN.ITK.MSFI.ZS", "exact", ""),
    "undernutrition": ("SN.ITK.DEFC.ZS", "narrower", "Undernourishment (too few calories) only; not stunting or wasting."),
    "smoking": ("SH.PRV.SMOK", "broader", "All tobacco use, smoked or not."),
    "tobacco use": ("SH.PRV.SMOK", "exact", ""),
    "alcohol consumption": ("SH.ALC.PCAP.LI", "exact", ""),
    "hiv": ("SH.DYN.AIDS.ZS", "exact", ""),
    "human immunodeficiency virus infectious disease": ("SH.DYN.AIDS.ZS", "exact", ""),
    "antiretroviral therapy": ("SH.HIV.ARTC.ZS", "exact", ""),
    "tuberculosis": ("SH.TBS.INCD", "exact", ""),
    "malaria": ("SH.MLR.INCD.P3", "exact", ""),
    "diabetes": ("SH.STA.DIAB.ZS", "exact", ""),
    "breastfeeding": ("SH.STA.BFED.ZS", "narrower", "Exclusive breastfeeding of children under 6 months only."),
    "birth control": ("SP.DYN.CONU.ZS", "exact", ""),
    "fertility": ("SP.DYN.TFRT.IN", "exact", ""),
    "physician": ("SH.MED.PHYS.ZS", "exact", ""),
    "health expenditure": ("SH.XPD.CHEX.GD.ZS", "exact", ""),
    "intimate partner violence": ("SG.VAW.1549.ZS", "narrower", "Women aged 15 to 49, violence in the last 12 months."),
    "domestic violence": (
        "GD_WBL_SAF_LAW_DOMVIOL", "proxy",
        "Whether the law addresses domestic violence, not how common it is. Tie-break: WDI has a prevalence measure (SG.VAW.1549.ZS).",
    ),
}
MATCH_TYPES = {"exact", "narrower", "broader", "proxy"}

# Concepts with no indicator that are not data gaps.
BREAKDOWNS = {"age factor", "age group", "ethnic group", "female organism", "gender identity", "male", "marital status", "woman"}
TOO_BROAD = {"family", "health", "health status", "work"}


def main() -> int:
    by_code = {c["code"]: c for c in P.load_catalog()}
    bad = sorted({code for code, _, _ in MATCHES.values() if code not in by_code})
    bad += sorted({f"{label}: type '{kind}'" for label, (_, kind, _) in MATCHES.items() if kind not in MATCH_TYPES})
    bad += sorted({f"{label}: a {kind} match needs a reason" for label, (_, kind, why) in MATCHES.items() if kind != "exact" and not why})
    if bad:
        print("Fix these, nothing written:", "; ".join(bad))
        return 1

    rels = []
    for t in P.load_topics():
        cid = t.get("concept_id")
        if cid and P.rel_file(cid).is_file():
            rels.append((cid, json.loads(P.rel_file(cid).read_text())))

    # 1. every linked concept with a match above, wherever it ranks
    decisions: dict[str, dict] = {}
    used: set[str] = set()
    for cid, rel in rels:
        for row in P.usable_rows(rel):
            other = P.neighbor(row, cid)[0]
            label = (other.get("wikidata_label") or "").lower()
            if label in MATCHES:
                code, kind, why = MATCHES[label]
                decisions[other["system_id"]] = {
                    "label": other.get("wikidata_label"), "indicator": code, "indicator_name": by_code[code]["name"],
                    "dataset": by_code[code]["dataset"], "match_type": kind, "reason": why or None,
                }
                used.add(label)
    P.REVIEW_FILE.write_text(json.dumps({"decisions": decisions}, indent=1) + "\n")  # shown_rows reads this

    # 2. every other concept on a page: checked, and why there is no indicator
    kinds = {"gap": 0, "breakdown": 0, "broad": 0}
    for cid, rel in rels:
        for row in P.shown_rows(rel):
            other = P.neighbor(row, cid)[0]
            if other["system_id"] in decisions:
                continue
            label = (other.get("wikidata_label") or "").lower()
            kind = "breakdown" if label in BREAKDOWNS else "broad" if label in TOO_BROAD else "gap"
            decisions[other["system_id"]] = {"label": other.get("wikidata_label"), "indicator": None, "kind": kind}
            kinds[kind] += 1
    P.REVIEW_FILE.write_text(
        json.dumps({"_note": "Written by scripts/hand_check.py. Edit that file, not this one.", "decisions": decisions}, indent=1) + "\n"
    )
    types = {k: sum(1 for d in decisions.values() if d.get("match_type") == k) for k in sorted(MATCH_TYPES)}
    print(f"{sum(types.values())} concepts matched: {types}")
    print(f"{sum(kinds.values())} concepts with no indicator: {kinds}")
    unused = sorted(set(MATCHES) - used)
    if unused:
        print("Labels above that match no linked concept:", ", ".join(unused))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
