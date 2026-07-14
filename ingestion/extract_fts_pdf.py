"""
M2 — Extract Bangladesh FTS *PDF* years into canonical trade facts, then reconcile.

Starts with 2023-24 (its Table 04 is a dedicated file, so it's the cleanest PDF year).
Reuses the M1 spine unchanged: the canonical fact schema, code-length classification,
forward-fill of the hierarchy, unpivot into H1/H2, and the four reconciliation checks
(imported from extract_fts_excel — single source of truth for validation). Only the
READER differs: PDF text lines instead of Excel cells.

Why PDFs are harder than the Excel:
  - text is a TOKEN STREAM, not columns -> parse from both ends (code first, 6 numbers
    last, description in the middle). This also makes the old inch-mark quote bug harmless.
  - the unit sometimes glues to the description ("ANIMALSNUM") -> split using the known
    8-code unit set derived from the M1 output.
  - page headers/footers repeat mid-table -> junk lines simply fail split_row and skip.
  - forward-fill must cross PAGE boundaries -> we stream the whole file as one flow.
  - some years bury Table 04 inside a bigger file -> optional page range per source.

Run:    ./.venv/bin/python ingestion/extract_fts_pdf.py
Output: data/interim/fts_<FY>_facts.csv (git-ignored) + a printed verdict per year.
"""

from pathlib import Path
import re
import sys
import pandas as pd
from pypdf import PdfReader

# Reuse M1's validators — do NOT re-implement them (single source of truth).
from extract_fts_excel import num, reconcile

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "Bd_Exportdata"
OUT_DIR = ROOT / "data" / "interim"
TOL = 0.001

# Valid unit codes, derived empirically from the clean M1 2024-25 output.
UNITS = {"NUM", "LTR", "CUM", "MTR", "SQM", "KG", "MT", "PR"}

ORIENTATION = {"X": "commodity_major", "M": "country_major"}

# Per-year source map. `t04` = the Table 04 file; `t01` = the chapter answer-key file.
# `pages` (optional) = (start, end) slice when Table 04 is embedded in a bigger file.
# Extensions are tolerated (resolve() tries with/without .pdf), so mixed naming is fine.
#
# `t05` (exports only): the older years' Table 04 omits the smallest ~1,100 commodities;
# Table 05 (the country-major mirror) itemizes ALL of them (verified: T03's full HS8 set
# minus T04's is exactly covered by T05). We fill only the commodities T04 lacks, so the
# publication's primary table stays the source of record as far as it goes.
#
# `repairs` (2022-23 imports only): that file has a physical printing defect — India's
# section was interrupted at chapter 85 by a spliced-in REVISED India (ch 29-98), and
# India's original continuation was misprinted after OTHER CONTRI. BBS's own Table 01
# matches original-India + revised-Asia-tail (97/97 chapters, +0.0002% grand), so we
# drop the splice and reattach the stray continuation. Hand-verified, evidence in
# LEARNING.md.
SOURCES = {
    # 2023-24 & 2022-23: Table 04 is a dedicated file; Table 01 sits at the front of Vol1/Vol2.1.
    "2023-24": {
        "X": {"t04": "FTS_23-24/Volume2.pdf",  "t01": "FTS_23-24/Volume1.pdf"},
        "M": {"t04": "FTS_23-24/Volume2.2",    "t01": "FTS_23-24/Volume2.1"},
    },
    "2022-23": {
        "X": {"t04": "FTS_22-23/Volume2.pdf",  "t01": "FTS_22-23/Volume1.pdf",
              "t05": "FTS_22-23/Volume3.pdf"},
        "M": {"t04": "FTS_22-23/Volume2.2",    "t01": "FTS_22-23/Volume2.1",
              "repairs": [
                  # spliced revised-India (runs from the code restart onward): drop
                  {"country": "746", "print": 1, "op": "drop_after_restart"},
                  # India's stray original continuation printed inside OTHER CONTRI: reattach
                  {"country": "999", "print": 1, "op": "reassign_after_restart",
                   "to": ("746", "INDIA")},
              ]},
    },
    # 2021-22: export Tables 01-05 all live in Volume1 (T04 pp 65-353, T05 pp 354-680).
    "2021-22": {
        "X": {"t04": "FTS_21-22/Volume1.pdf", "t04_pages": (65, 354),
              "t05": "FTS_21-22/Volume1.pdf", "t05_pages": (354, 681),
              "t01": "FTS_21-22/Volume1.pdf"},
        "M": {"t04": "FTS_21-22/Volume2.2",   "t01": "FTS_21-22/Volume2.1"},
    },
    # 2020-21: one summary doc per flow holds everything. Table 01 starts p39; Table 04 is
    # pp 112-446 + Table 05 pp 447-719 (export) / pp 183-end (import, no Table 05).
    "2020-21": {
        "X": {"t04": "FTS_20-21/FTS_20-21_SummaryDoc_Volume1.pdf", "t04_pages": (112, 447),
              "t05": "FTS_20-21/FTS_20-21_SummaryDoc_Volume1.pdf", "t05_pages": (447, 720),
              "t01": "FTS_20-21/FTS_20-21_SummaryDoc_Volume1.pdf", "t01_pages": (39, 60)},
        "M": {"t04": "FTS_20-21/FTS_20-21_SummaryDoc_Volume2.pdf", "t04_pages": (183, 1483),
              "t01": "FTS_20-21/FTS_20-21_SummaryDoc_Volume2.pdf", "t01_pages": (39, 60)},
    },
}


def resolve(rel):
    """Find the file whether or not it carries a .pdf extension.

    NB: append/strip ".pdf" on the full name — do NOT use Path.with_suffix, which
    treats "Volume2.2" as having extension ".2" and would mangle it to "Volume2.pdf".
    """
    p = DATA / rel
    for cand in (p, Path(f"{p}.pdf"), Path(str(p).removesuffix(".pdf"))):
        if cand.exists():
            return cand
    sys.exit(f"Source not found: {rel}")


def pdf_lines(path, pages=None):
    """Yield text lines across the given page range as ONE continuous stream."""
    r = PdfReader(str(path))
    start, end = (pages or (0, len(r.pages)))
    for i in range(start, end):
        for ln in (r.pages[i].extract_text() or "").splitlines():
            yield ln


# A value token is a plain integer OR scientific notation ("3.06704E+11"): the
# revised re-print sections in 2022-23 imports were exported from Excel with big
# numbers in E-notation, and rejecting those tokens silently dropped country headers.
NUM_RE = re.compile(r"-?(?:\d+|\d+\.\d+E\+?\d+)$", re.I)


def split_row(line):
    """Parse a data line from both ends: (code, middle_tokens, [6 numbers]) or None.

    A data row is: CODE  description-words  [unit]  q_h1 v_h1 q_h2 v_h2 q_fy v_fy.
    Junk lines (titles, column headers, footers) lack a digit code + 6 trailing
    integers and return None, so they're skipped for free.
    """
    toks = line.split()
    if len(toks) < 6:
        return None
    nums = [t.replace(",", "") for t in toks[-6:]]
    if not all(NUM_RE.match(t) for t in nums):
        return None
    # Return (label_tokens, six_numbers). The label is everything before the numbers:
    #   - a numeric code + description (normal row),
    #   - a name only, no code (e.g. WEST.SAHARA — some countries lack a code), or
    #   - EMPTY, when PDF extraction dropped a row's code+name and left 6 bare numbers.
    # The 6-trailing-numbers requirement already screens out title/header/footer junk.
    return toks[:-6], [num(t) for t in nums]


def parse_unit(middle):
    """For a commodity row -> (unit, description). Handles spaced and glued units."""
    if not middle:
        return None, ""
    last = middle[-1]
    if last in UNITS:                                   # spaced: "PRIMATES NUM"
        return last, " ".join(middle[:-1])
    for u in sorted(UNITS, key=len, reverse=True):      # glued: "ANIMALSNUM"
        if last.endswith(u) and len(last) > len(u):
            return u, " ".join(middle[:-1] + [last[:-len(u)]])
    return None, " ".join(middle)                       # no unit present


def apply_repairs(records, repairs, occ):
    """Surgical fixes for hand-verified printing defects (see SOURCES docstring).

    Each repair targets one section print (country + print number), finds the first
    point where its HS-code sequence RESTARTS (codes ascend within a section, so a
    descent marks spliced-in foreign pages), and either drops the tail or reassigns
    it to the country it really belongs to.
    """
    for rep in repairs:
        key, prt = rep["country"], rep.get("print", 1)
        idx = [i for i, r in enumerate(records)
               if r and r["_key"] == key and r["_occ"] == prt]
        last, cut = "", None
        for i in idx:
            c = records[i]["hs8"]
            if c == "UNKNOWN":
                continue
            if last and c < last:
                cut = i
                break
            last = c
        if cut is None:
            print(f"  [warn] repair on {key} print {prt}: no code restart found, skipped")
            continue
        tail = [i for i in idx if i >= cut]
        if rep["op"] == "drop_after_restart":
            for i in tail:
                records[i] = None
        else:                                           # reassign_after_restart
            tk, tname = rep["to"]
            for i in tail:
                records[i].update(country_code=tk, country_name=tname,
                                  _key=tk, _occ=occ.get(tk, 1))
        print(f"  [note] repair: {rep['op']} on {key} print {prt} "
              f"({len(tail)} fact rows)")
    return [r for r in records if r]


def parse_table04(path, flow, orientation, pages=None, repairs=None):
    """Flatten a Table 04 PDF into canonical per-half fact rows (mirrors M1)."""
    commodity_major = orientation == "commodity_major"
    parent_key = "hs8" if commodity_major else "country_code"
    total_word = "EXPORT TOTAL" if flow == "X" else "IMPORT TOTAL"

    records, parents = [], []
    grand_v = grand_q = None
    cur_hs8 = cur_unit = cur_cc = cur_cname = None
    unknown = lost = 0
    # Some files (2022-23 imports) append a REVISED re-print of whole sections after
    # the original run; BBS's own control totals match the revision. Track how many
    # times each header key has appeared so the LAST print of a section wins.
    occ, cur_key, cur_occ = {}, None, 0

    def emit(f_hs8, f_unit, f_cc, f_cname, nums):
        """Append one fact per non-empty half (the unpivot)."""
        q1, v1, q2, v2, _, _ = nums
        for half, qq, vv in (("H1", q1, v1), ("H2", q2, v2)):
            if qq or vv:
                records.append({
                    "flow": flow, "fy": None, "half": half,
                    "hs8": f_hs8, "country_code": f_cc, "country_name": f_cname,
                    "unit": f_unit, "quantity": qq, "value_bdt": vv,
                    "_key": cur_key, "_occ": cur_occ,
                })

    for line in pdf_lines(path, pages):
        s = line.strip()
        if not s:
            continue

        if total_word in s:                             # the grand TOTAL row
            g = [num(t) for t in (x.replace(",", "") for x in s.split())
                 if NUM_RE.match(t)]
            if len(g) >= 6:
                grand_q, grand_v = g[-2], g[-1]         # q_fy, v_fy
            continue

        row = split_row(s)
        if row is None:                                 # header/footer/junk
            continue
        label, nums = row
        v_fy, q_fy = nums[5], nums[4]

        if not label:
            # Bare 6-number row: PDF extraction dropped this detail's code+name. Attach it
            # to the current header so the value still reconciles; mark the lost side UNKNOWN.
            lost += 1
            if commodity_major:                         # export: destination lost
                if cur_hs8 is not None:
                    emit(cur_hs8, cur_unit, "UNKNOWN", "UNKNOWN", nums)
            else:                                       # import: commodity lost
                if cur_cc is not None:
                    emit("UNKNOWN", None, cur_cc, cur_cname, nums)
            continue

        code = label[0]
        if code.isdigit():
            is_commodity, is_country = len(code) >= 6, len(code) <= 3
            if not (is_commodity or is_country):        # 4-5 digit code shouldn't happen
                unknown += 1
                continue
            name = label[1:]
        else:
            # Name-only country (no code, e.g. WEST.SAHARA): a detail in exports, a header
            # in imports. Keyed by its name.
            is_commodity, is_country = False, True
            name, code = label, None

        if is_commodity:
            unit, desc = parse_unit(name)
            code = code.zfill(8)
        else:
            unit, desc = None, " ".join(name)
            code = code.zfill(3) if code is not None else desc

        if is_commodity == commodity_major:             # HEADER row (subtotal)
            if is_commodity:
                cur_hs8, cur_unit, key = code, unit, code
            else:
                cur_cc, cur_cname, key = code, desc, code
            occ[key] = occ.get(key, 0) + 1
            cur_key, cur_occ = key, occ[key]
            parents.append((key, nums[1], nums[3], v_fy, nums[0], nums[2], q_fy,
                            occ[key]))
            continue

        # DETAIL row -> stamp with the running header context.
        if is_commodity:                                # import: commodity under country
            f_hs8, f_unit, f_cc, f_cname = code, unit, cur_cc, cur_cname
        else:                                           # export: country under commodity
            f_hs8, f_unit, f_cc, f_cname = cur_hs8, cur_unit, code, desc
        if f_hs8 is None or f_cc is None:
            unknown += 1
            continue
        emit(f_hs8, f_unit, f_cc, f_cname, nums)

    if unknown:
        print(f"  [warn] {unknown} rows matched no rule (bad code, or detail before header)")
    if lost:
        print(f"  [note] {lost} rows had code+name dropped by PDF extraction "
              f"-> value kept, counterpart marked UNKNOWN")

    if repairs:
        records = apply_repairs(records, repairs, occ)
    df = pd.DataFrame(records)
    pdf_ = pd.DataFrame(parents, columns=["key", "v_h1", "v_h2", "v_fy",
                                          "q_h1", "q_h2", "q_fy", "occ"])
    reprinted = {k for k, n in occ.items() if n > 1}
    if reprinted:
        # Keep only each key's LAST print (the revision BBS's totals agree with).
        df = df[df["_key"].map(occ) == df["_occ"]]
        pdf_ = pdf_[pdf_["key"].map(occ) == pdf_["occ"]]
        print(f"  [note] {len(reprinted)} {parent_key} sections printed twice "
              f"(original + revision) -> kept the last print of each")
    df = df.drop(columns=["_key", "_occ"]).reset_index(drop=True)
    return (df, pdf_.drop(columns=["occ"]).reset_index(drop=True),
            (grand_v, grand_q), parent_key)


def parse_table01(path, flow, pages=None):
    """Chapter (HS2) -> full-year value, plus the reported grand total.

    A chapter row is: `NN DESCRIPTION v_h1 v_h2 v_fy` (2-digit code, 3 trailing values).
    `pages` brackets the Table 01 section for the summary-doc years where it's buried after
    prose. We stop at the Table 02 header, but only AFTER collecting a chapter, so a
    contents-page mention of "Table 02" before the real data doesn't end us early.
    """
    total_word = "EXPORT TOTAL" if flow == "X" else "IMPORT TOTAL"
    chapters, grand = {}, None
    for line in pdf_lines(path, pages):
        s = line.strip()
        if chapters and re.search(r"Table\s*-?\s*0?2\b", s):   # reached Table 02 -> done
            break
        if total_word in s:
            g = [num(t) for t in re.findall(r"-?\d+", s.replace(",", ""))]
            if g:
                grand = g[-1]                           # v_fy is the last value
            continue
        toks = s.split()
        if len(toks) >= 4 and toks[0].isdigit() and len(toks[0]) == 2:
            # A chapter row ends "... v_h1 v_h2 v_fy", but a page FOOTER can glue onto
            # the last line ("...31476881650Fts-Exp- 1"), corrupting naive last-token
            # parsing. The halves must sum to the year, so find the a+b==c triple.
            nums = [int(x) for x in re.findall(r"\d+", s.replace(",", ""))][1:]
            v = next((nums[i + 2] for i in range(len(nums) - 2)
                      if nums[i] + nums[i + 1] == nums[i + 2]), None)
            if v is None and len(nums) >= 3:
                v = nums[-1]                            # clean-line fallback
            if v is not None:
                chapters[toks[0].zfill(2)] = float(v)
    return chapters, grand


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_ok = True

    for fy, flows in SOURCES.items():
        year_facts = []
        for flow in ("X", "M"):
            src = flows[flow]
            df, parents, grand, pkey = parse_table04(
                resolve(src["t04"]), flow, ORIENTATION[flow], src.get("t04_pages"),
                src.get("repairs"))

            if "t05" in src:
                # Older export Table 04s omit the smallest commodities entirely; the
                # country-major mirror (Table 05) itemizes all of them. Fill ONLY the
                # HS8 codes whose DETAIL rows Table 04 lacks (keying on details, not
                # headers, also covers a 2020-21 defect where one commodity's header
                # printed but its country rows didn't), so T04 stays the record.
                df5, _, _, _ = parse_table04(
                    resolve(src["t05"]), flow, "country_major", src.get("t05_pages"))
                add = df5[~df5["hs8"].isin(set(df["hs8"])) & (df5["hs8"] != "UNKNOWN")]
                print(f"  [note] Table 05 supplement: {add['hs8'].nunique():,} commodities "
                      f"absent from Table 04 -> +{len(add):,} rows, "
                      f"+{add['value_bdt'].sum():,.0f} BDT")
                df = pd.concat([df, add], ignore_index=True)

            df["fy"] = fy
            t01, t01_grand = parse_table01(resolve(src["t01"]), flow, src.get("t01_pages"))
            all_ok &= reconcile(df, parents, grand, t01, t01_grand, flow, pkey, fy)
            year_facts.append(df)

        facts = pd.concat(year_facts, ignore_index=True)
        out = OUT_DIR / f"fts_{fy}_facts.csv"
        facts.to_csv(out, index=False)
        print(f"\nWrote {len(facts):,} fact rows -> {out.relative_to(ROOT)}")

        exp = facts[facts["flow"] == "X"]
        usable = exp[(exp["quantity"] > 0) & exp["unit"].notna()]
        pct = len(usable) / len(exp) * 100 if len(exp) else 0
        print(f"Export rows usable for unit price (qty>0 & unit set): "
              f"{len(usable):,}/{len(exp):,} ({pct:.1f}%)")

    print(f"\nOVERALL: {'PASS' if all_ok else 'FAIL — fix parser before proceeding'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
