#!/usr/bin/env python
"""Hand-checked matches from graph concepts to World Bank indicators.

The automatic name match (pipeline.py `review`) proposes indicators; a person reads the proposals
and records the decision here, by concept label. Running this file writes data/match_review.json,
which `pipeline.py build` applies. A concept with no entry below that appears on a page is recorded
as checked with no indicator ("Not found in the World Bank datasets we checked").

Tie-break: where indicators in two datasets fit a concept equally well, the one outside
World Development Indicators is kept, because the tool exists to surface indicators an analyst
would not find by staying in one dataset. Those cases are marked with a note.

    .venv/bin/python scripts/hand_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as P  # noqa: E402

# concept label (lower case) -> indicator code, or (code, note)
MATCHES: dict[str, str | tuple[str, str]] = {
    # jobs and the economy
    "unemployment": "SL.UEM.TOTL.ZS",
    "employment": "SL.EMP.TOTL.SP.ZS",
    "economic growth": "NY.GDP.MKTP.KD.ZG",
    "gross domestic product": "NY.GDP.MKTP.CD",
    "income": ("NY.GNP.PCAP.CD", "Income per person; the graph concept is personal or household income."),
    "income inequality": "SI.POV.GINI",
    "poverty": "SI.POV.NAHC",
    "inflation": "FP.CPI.TOTL.ZG",
    "investment": "NE.GDI.TOTL.ZS",
    "fiscal policy": "IQ.CPA.FISP.XQ",
    "industrial sector": "NV.IND.TOTL.ZS",
    "export": ("IC.FRM.TRD.TR17", "Tie-break: WDI also has 'Exports of goods and services (% of GDP)' (NE.EXP.GNFS.ZS)."),
    "theft": ("IC.FRM.CRM.CRIME10", "Firm-level measure; no theft indicator in WDI."),
    "human migration": "SM.POP.NETM",
    "refugee": "SM.POP.RHCR.EA",
    # education and connectivity
    "education": ("SE.SEC.CUAT.LO.ZS", "Studies use level of education reached; matched to attainment."),
    "educational stage": ("SE.SEC.CUAT.LO.ZS", "Level of education reached; matched to attainment."),
    "literacy": "SE.ADT.LITR.ZS",
    "internet": ("IT.NET.BBND.P2", "Internet availability; the topic's own indicator already measures use."),
    "internet access": "IT.NET.BBND.P2",
    "urban population": "SP.URB.TOTL.IN.ZS",
    "rural population": "SP.RUR.TOTL.ZS",
    # health and population
    "mortality rate": "SP.DYN.CDRT.IN",
    "life expectancy": "SP.DYN.LE00.IN",
    "infant mortality": "SP.DYN.IMRT.IN",
    "neonatal mortality": "SH.DYN.NMRT",
    "maternal mortality ratio": "SH.STA.MMRT",
    "maternal death": "SH.STA.MMRT",
    "suicide": "SH.STA.SUIC.P5",
    "homicide": "VC.IHR.PSRC.P5",
    "food insecurity": "SN.ITK.MSFI.ZS",
    "undernutrition": "SN.ITK.DEFC.ZS",
    "smoking": "SH.PRV.SMOK",
    "tobacco use": "SH.PRV.SMOK",
    "alcohol consumption": "SH.ALC.PCAP.LI",
    "hiv": "SH.DYN.AIDS.ZS",
    "human immunodeficiency virus infectious disease": "SH.DYN.AIDS.ZS",
    "antiretroviral therapy": "SH.HIV.ARTC.ZS",
    "tuberculosis": "SH.TBS.INCD",
    "malaria": "SH.MLR.INCD.P3",
    "diabetes": "SH.STA.DIAB.ZS",
    "breastfeeding": "SH.STA.BFED.ZS",
    "birth control": "SP.DYN.CONU.ZS",
    "fertility": "SP.DYN.TFRT.IN",
    "physician": "SH.MED.PHYS.ZS",
    "health expenditure": "SH.XPD.CHEX.GD.ZS",
    "intimate partner violence": "SG.VAW.1549.ZS",
    "domestic violence": (
        "GD_WBL_SAF_LAW_DOMVIOL",
        "Measures the law, not how common violence is. Tie-break: WDI has a prevalence measure (SG.VAW.1549.ZS).",
    ),
}


def main() -> int:
    by_code = {c["code"]: c for c in P.load_catalog()}
    bad = [str(v if isinstance(v, str) else v[0]) for v in MATCHES.values() if (v if isinstance(v, str) else v[0]) not in by_code]
    if bad:
        print("Unknown indicator codes, nothing written:", ", ".join(sorted(set(bad))))
        return 1

    decisions: dict[str, dict] = {}
    used: set[str] = set()
    # 1. every linked concept with a match above, wherever it ranks
    rels = []
    for t in P.load_topics():
        cid = t.get("concept_id")
        if cid and P.rel_file(cid).is_file():
            rels.append((cid, json.loads(P.rel_file(cid).read_text())))
    for cid, rel in rels:
        for row in P.usable_rows(rel):
            other = P.neighbor(row, cid)[0]
            label = (other.get("wikidata_label") or "").lower()
            if label in MATCHES:
                m = MATCHES[label]
                code, note = (m, None) if isinstance(m, str) else m
                decisions[other["system_id"]] = {
                    "label": other.get("wikidata_label"), "indicator": code,
                    "indicator_name": by_code[code]["name"], "dataset": by_code[code]["dataset"], "note": note,
                }
                used.add(label)
    P.REVIEW_FILE.write_text(json.dumps({"decisions": decisions}, indent=1) + "\n")
    # 2. every other concept on a page: checked, nothing found
    none = 0
    for cid, rel in rels:
        for row in P.shown_rows(rel):
            other = P.neighbor(row, cid)[0]
            if other["system_id"] not in decisions:
                decisions[other["system_id"]] = {"label": other.get("wikidata_label"), "indicator": None, "note": None}
                none += 1
    P.REVIEW_FILE.write_text(
        json.dumps(
            {
                "_note": "Written by scripts/hand_check.py. Hand-checked matches from graph concepts to World Bank indicators.",
                "decisions": decisions,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"{len(decisions) - none} concepts matched to an indicator, {none} checked with nothing found")
    unused = sorted(set(MATCHES) - used)
    if unused:
        print("Labels above that match no linked concept:", ", ".join(unused))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
