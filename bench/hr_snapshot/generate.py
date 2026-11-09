#!/usr/bin/env python3
"""hr_snapshot generator — deterministic by seed; profiles: clean, nasty.

Emits one JSONL file per base table: <schema>.<table>.jsonl in the out dir.
JSON null for NULL, ISO date strings (YYYY-MM-DD), JSON true/false booleans.
The bench loader reads these and does the SQLite <schema>_<table> prefixing.

Shape:
- 52 biweekly periods ending 2025-06-14 .. 2026-06-20 (freq B, period_seq -51..0)
- 104 weekly periods ending 2024-06-22 .. 2026-06-20 (freq W, aligned to same anchor)
- as_of = 2026-06-30 (post-current, within a biweekly period)
- current_ppe = 2026-06-20 (period_seq = 0)
- Snapshot restatement per owner's model: each snap_dt restates its own period and
  the two prior biweekly periods ({snap_dt, snap_dt-14, snap_dt-28} as period_end_dt).
  So each biweekly period has up to 3 (snap_dt, period_end_dt) rows per associate
  (the period's own PPE snapshot, plus the 2 later-snap-dt restatements).
- ~250 employees with skew: 1 large org (~40%), 1 mid (~20%), small orgs (~30% split),
  and a long tail of single-person orgs (~10%).
- LVL 2..7 for both reporting and functional chains. Nasty strips ~10% LVL_6 functional
  nodes (gaps in _FN chain).
- Nasty hazards: NULL statuses (~2%), term_before_hire row (assoc 999001), orphan
  lvl_5_id=999999, zero-headcount period (single PPE wiped of A-status), one duplicate.
"""
import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 6, 30)
CURRENT_PPE = date(2026, 6, 20)  # period_seq = 0, is_current_flag = True
BIWEEKLY_PERIODS = 52
WEEKLY_PERIODS = 104
RESTATEMENTS_PER_PERIOD = 3  # snap_dt ∈ {ppe, ppe+14, ppe+28}

COUNTRIES = [
    ("US", "NorthAm"), ("CA", "NorthAm"),
    ("GB", "EMEA"), ("DE", "EMEA"), ("FR", "EMEA"), ("IE", "EMEA"),
    ("IN", "APAC"), ("JP", "APAC"), ("SG", "APAC"), ("AU", "APAC"),
    ("BR", "LATAM"), ("MX", "LATAM"),
]
JOB_GROUPS = [
    ("ENG", "Technical"), ("DAT", "Technical"), ("DES", "Creative"),
    ("MKT", "GTM"), ("SAL", "GTM"), ("CS",  "GTM"),
    ("OPS", "Admin"), ("FIN", "Admin"), ("HR",  "Admin"), ("LEG", "Admin"),
]
STATUS_MEANINGS = [
    ("A", "Active"), ("L", "Leave"), ("T", "Terminated"),
    ("R", "Retired"), ("U", "Unknown"),
]

SEGMENTS = ["Enterprise", "Mid-Market", "SMB"]
ASSOC_TYPES = ["FTE", "Contractor", "Intern"]
GIVEN_NAMES = ["Aarav", "Diya", "Ravi", "Priya", "Sara", "Alex", "Jin", "Maya", "Noor", "Liam",
               "Ethan", "Sofia", "Oliver", "Amara", "Yuki", "Kenji", "Lucia", "Hugo",
               "Nadia", "Omar", "Elena", "Ivan", "Chloe", "Marco", "Aya", "Kai", "Rin",
               "Taro", "Emma", "Mika"]
FAMILY_NAMES = ["Sharma", "Patel", "Kumar", "Lee", "Kim", "Zhang", "Nguyen", "Rossi", "Garcia",
                "Silva", "Petrov", "Nakamura", "Mori", "Smith", "Brown", "Dubois", "Chen",
                "Mueller", "Fischer", "Morales", "Tanaka", "Yamamoto", "Iyer", "Reddy"]


def biweekly_series(current_ppe: date, n: int):
    return [current_ppe - timedelta(days=14 * i) for i in range(n - 1, -1, -1)]


def weekly_series(current_ppe: date, n: int):
    return [current_ppe - timedelta(days=7 * i) for i in range(n - 1, -1, -1)]


def build_hierarchy(rng, kind: str, has_fn_gaps: bool = False):
    """Return list of (lvl, node_id, node_name, parent_node_id, kind) for LVL 2..7."""
    nodes = []
    base = 10000 if kind == "reporting" else 50000
    lvl2 = [(2, base + 2001 + i, f"{kind[:3].upper()}-L2-{i:02}", None) for i in range(4)]
    nodes.extend(lvl2)
    prev = lvl2
    counter = [base + 3001, base + 4001, base + 5001, base + 6001, base + 7001]
    for lvl, start in zip(range(3, 8), counter):
        branches = 3 if lvl < 5 else 2
        cur = []
        for parent in prev:
            for i in range(branches):
                nid = start
                start += 1
                cur.append((lvl, nid, f"{kind[:3].upper()}-L{lvl}-{nid - (base + lvl * 1000)}", parent[1]))
        prev = cur
        nodes.extend(cur)
    if has_fn_gaps and kind == "functional":
        kept = [n for n in nodes if not (n[0] == 6 and rng.random() < 0.1)]
        nodes = kept
    return [(lvl, nid, name, pid, kind) for lvl, nid, name, pid in nodes]


def chain_for_leaf(leaf_id: int, by_lvl_parent: dict):
    """Given a leaf LVL_7 id, climb to LVL_2 by looking up parents at each level."""
    chain = {7: leaf_id}
    cur = leaf_id
    for lvl in (7, 6, 5, 4, 3, 2):
        # look up parent of cur in the next-higher lvl
        pid = by_lvl_parent.get(cur)
        if lvl - 1 >= 2:
            chain[lvl - 1] = pid
        cur = pid
        if cur is None:
            break
    return {f"lvl_{lvl}_id": chain.get(lvl) for lvl in range(2, 8)}


def skewed_leaf_weights(rng, leaves):
    """Return weight list with a strong skew matching Watson's agreed shape:
    - one large org gets ~40% of weight
    - one mid gets ~20%
    - a handful of small orgs together ~30% (split across ~6 nodes)
    - the rest are the long tail (~10%), many with tiny weights.
    """
    n = len(leaves)
    weights = [0.0] * n
    # Deterministic pick of big & mid orgs using rng
    order = list(range(n))
    rng.shuffle(order)
    big = order[0]
    mid = order[1]
    smalls = order[2:8]
    tail = order[8:]
    weights[big] = 0.40
    weights[mid] = 0.20
    for s in smalls:
        weights[s] = 0.30 / max(len(smalls), 1)
    rest = 0.10
    if tail:
        # a few get almost nothing, most get a tiny equal share
        for t in tail:
            weights[t] = rest / len(tail)
    # normalize
    total = sum(weights)
    return [w / total for w in weights]


def weighted_choice(rng, items, weights):
    r = rng.random()
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if r <= acc:
            return item
    return items[-1]


def generate(seed: int, profile: str, out_dir: Path):
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    ref_country = COUNTRIES[:]
    ref_job = JOB_GROUPS[:]
    ref_status = STATUS_MEANINGS[:]

    bppes = biweekly_series(CURRENT_PPE, BIWEEKLY_PERIODS)
    wppes = weekly_series(CURRENT_PPE, WEEKLY_PERIODS)
    pay_period = []
    for i, ppe in enumerate(bppes):
        period_seq = i - (BIWEEKLY_PERIODS - 1)
        pay_period.append({
            "ppe_dt": ppe.isoformat(),
            "freq_cd": "B",
            "period_start_dt": (ppe - timedelta(days=13)).isoformat(),
            "period_seq": period_seq,
            "is_current_flag": (ppe == CURRENT_PPE),
        })
    for i, ppe in enumerate(wppes):
        period_seq = i - (WEEKLY_PERIODS - 1)
        pay_period.append({
            "ppe_dt": ppe.isoformat(),
            "freq_cd": "W",
            "period_start_dt": (ppe - timedelta(days=6)).isoformat(),
            "period_seq": period_seq,
            "is_current_flag": (ppe == CURRENT_PPE),
        })

    has_fn_gaps = (profile != "clean")
    rep_hier = build_hierarchy(rng, "reporting")
    fn_hier = build_hierarchy(rng, "functional", has_fn_gaps=has_fn_gaps)
    hierarchy = rep_hier + fn_hier
    # parent lookup: node_id -> parent_node_id (within same kind)
    rep_parent = {nid: pid for (lvl, nid, nm, pid, kind) in rep_hier}
    fn_parent = {nid: pid for (lvl, nid, nm, pid, kind) in fn_hier}
    rep_leaves = [nid for (lvl, nid, nm, pid, kind) in rep_hier if lvl == 7]
    fn_leaves = [nid for (lvl, nid, nm, pid, kind) in fn_hier if lvl == 7]

    # Skewed weights (same rng stream reuse → deterministic)
    rep_weights = skewed_leaf_weights(rng, rep_leaves)
    fn_weights = skewed_leaf_weights(rng, fn_leaves)

    n_emp = 250 if profile == "clean" else 255
    employees = []
    assoc_assignments = {}
    # Seed 44 injects early-leaver-rich hires so termed_90_n is not always 0.
    # This runs ONLY on seed 44; seeds 1-3/42/43 stream the RNG identically to before.
    inject_early_leavers_seed44 = (seed == 44 and profile == "nasty")
    for i in range(n_emp):
        aid = 900001 + i
        given = GIVEN_NAMES[rng.randint(0, len(GIVEN_NAMES) - 1)]
        family = FAMILY_NAMES[rng.randint(0, len(FAMILY_NAMES) - 1)]
        # Hire anywhere from 30 days to 8 years before as_of
        hire_days_before_as_of = rng.randint(30, 365 * 8)
        hire_dt = AS_OF - timedelta(days=hire_days_before_as_of)
        term_dt = None
        if rng.random() < 0.12:
            term_days_before_as_of = rng.randint(0, hire_days_before_as_of - 1)
            term_dt = AS_OF - timedelta(days=term_days_before_as_of)
        employees.append({
            "assoc_id": aid,
            "given_name": given,
            "family_name": family,
            "hire_dt": hire_dt.isoformat(),
            "term_dt": term_dt.isoformat() if term_dt else None,
            "mgr_assoc_id": None,
        })
        rep_leaf = weighted_choice(rng, rep_leaves, rep_weights)
        fn_leaf = weighted_choice(rng, fn_leaves, fn_weights)
        assoc_assignments[aid] = {
            "country": rng.choice(ref_country)[0],
            "job_grp": rng.choice(ref_job)[0],
            "segment": rng.choice(SEGMENTS),
            "assoc_type": rng.choice(ASSOC_TYPES) if rng.random() < 0.9 else None,
            "hire_dt_d": hire_dt,
            "term_dt_d": term_dt,
            "rep_chain": chain_for_leaf(rep_leaf, rep_parent),
            "fn_chain": chain_for_leaf(fn_leaf, fn_parent),
        }

    if inject_early_leavers_seed44:
        # Pick a leaf whose chain climbs to LVL_2 = 12003 (the big reporting org under {manager_id}=12003).
        el_leaf = None
        for cand in rep_leaves:
            c = chain_for_leaf(cand, rep_parent)
            if c.get("lvl_2_id") == 12003:
                el_leaf = cand
                break
        if el_leaf is None:
            el_leaf = rep_leaves[0]
        el_chain = chain_for_leaf(el_leaf, rep_parent)
        # Insert 8 early-leaver associates with hire_dt inside the last 13 biweekly periods.
        # The authoritative snapshot uses snap_dt = period_end_dt and only knows a term that
        # has happened by that period's close, so term_dt must fall inside the same biweekly
        # period as hire_dt for it to show up in the termed_90 numerator. Place 6 inside
        # the same biweek (short tenures), and 2 in a later biweek to exercise the restatement
        # path (they will count as new hires but not as termed_90 in the authoritative view).
        for k in range(8):
            el_aid = 998001 + k
            hire_days_before_current = rng.randint(14, 180)
            el_hire = CURRENT_PPE - timedelta(days=hire_days_before_current)
            if k < 6:
                term_days_after_hire = rng.randint(1, 10)   # same biweek as hire
            else:
                term_days_after_hire = rng.randint(20, 85)  # later, snapshot won't see it on the hire-period snap
            el_term = el_hire + timedelta(days=term_days_after_hire)
            employees.append({
                "assoc_id": el_aid, "given_name": "Early", "family_name": f"Leaver{k:02d}",
                "hire_dt": el_hire.isoformat(), "term_dt": el_term.isoformat(),
                "mgr_assoc_id": None,
            })
            assoc_assignments[el_aid] = {
                "country": "US", "job_grp": "ENG", "segment": "SMB", "assoc_type": "FTE",
                "hire_dt_d": el_hire, "term_dt_d": el_term,
                "rep_chain": el_chain,
                "fn_chain": chain_for_leaf(fn_leaves[0], fn_parent),
            }

    if profile == "nasty":
        bad_aid = 999001
        bad_hire = date(2025, 3, 1)
        bad_term = date(2024, 12, 15)  # term BEFORE hire
        employees.append({
            "assoc_id": bad_aid, "given_name": "Bad", "family_name": "Row",
            "hire_dt": bad_hire.isoformat(), "term_dt": bad_term.isoformat(),
            "mgr_assoc_id": None,
        })
        assoc_assignments[bad_aid] = {
            "country": "US", "job_grp": "ENG", "segment": "SMB", "assoc_type": "FTE",
            "hire_dt_d": bad_hire, "term_dt_d": bad_term,
            "rep_chain": chain_for_leaf(rep_leaves[0], rep_parent),
            "fn_chain": chain_for_leaf(fn_leaves[0], fn_parent),
        }
        # Orphan lvl_5: patch assignment of the first employee
        orphan_aid = employees[0]["assoc_id"]
        assoc_assignments[orphan_aid]["rep_chain"]["lvl_5_id"] = 999999

    # Pre-pick a "job_grp move" record: one associate, one restatement snap_dt, carries a
    # corrected job_grp relative to the original snap_dt=PPE snapshot. The CORRECTED value
    # appears on the LATER snap_dt row; the original (earlier) snap_dt row keeps the original.
    jg_move_aid = sorted(assoc_assignments.keys())[7]
    jg_move_period = bppes[-2]  # period_seq = -1
    jg_move_snap = bppes[-1]    # snap_dt = current, restates period_seq -1 with corrected job_grp
    jg_original = assoc_assignments[jg_move_aid]["job_grp"]
    # pick a deterministically-different job_grp
    other_jgs = [jg for jg, _ in ref_job if jg != jg_original]
    jg_corrected = other_jgs[0] if other_jgs else jg_original

    # Build snapshot: for each snap_dt (biweekly PPEs), restate current + 2 prior PPEs.
    country_region_lookup = {c: r for c, r in ref_country}
    snapshots = []
    ppe_set = set(bppes)
    for snap_dt in bppes:
        for k in range(RESTATEMENTS_PER_PERIOD):
            ppe = snap_dt - timedelta(days=14 * k)
            if ppe not in ppe_set:
                continue  # earlier than history
            is_restatement = (snap_dt > ppe)
            for aid, asgn in assoc_assignments.items():
                hire_dt_d = asgn["hire_dt_d"]
                term_dt_d = asgn["term_dt_d"]
                if hire_dt_d > ppe:
                    continue
                active_at_ppe = (term_dt_d is None or term_dt_d > ppe)
                term_known_at_snap = (term_dt_d is not None and term_dt_d <= snap_dt)
                if active_at_ppe:
                    status = "A"
                elif term_known_at_snap:
                    status = "T"
                else:
                    status = "A"  # term not yet known at snap_dt

                # STATUS_CD restatement: on ~2% of restatement rows, correct the status
                # (so the restated row carries a different status than the original snap=PPE row).
                # Flip A→L or L→A deterministically via rng.
                if is_restatement and rng.random() < 0.02:
                    if status == "A":
                        status = "L"
                    elif status == "L":
                        status = "A"

                # NULL statuses (nasty only)
                if profile == "nasty" and rng.random() < 0.02:
                    status = None

                # job_grp move: on the pre-picked (aid, period, snap_dt) restatement row, use the corrected jg.
                this_job_grp = asgn["job_grp"]
                if is_restatement and aid == jg_move_aid and ppe == jg_move_period and snap_dt == jg_move_snap:
                    this_job_grp = jg_corrected

                is_new_hire = 1 if (hire_dt_d > ppe - timedelta(days=90) and hire_dt_d <= ppe) else 0
                is_exit = 1 if (term_dt_d is not None and term_dt_d > ppe - timedelta(days=14) and term_dt_d <= ppe) else 0
                invol = None
                if is_exit:
                    invol = 1 if rng.random() < 0.3 else 0
                row = {
                    "snap_dt": snap_dt.isoformat(),
                    "period_end_dt": ppe.isoformat(),
                    "assoc_id": aid,
                    "hire_dt": hire_dt_d.isoformat(),
                    "term_dt": term_dt_d.isoformat() if term_known_at_snap else None,
                    "status_cd": status,
                    "invol_exit_ind": invol,
                    "new_hire_ind": is_new_hire,
                    "exit_filt_ind": is_exit,
                    "turn_filt_ind": is_exit,
                    "region": country_region_lookup.get(asgn["country"], "ROW"),
                    "segment": asgn["segment"],
                    "country": asgn["country"],
                    "job_grp": this_job_grp,
                    "assoc_type": asgn["assoc_type"],
                }
                for lvl in range(2, 8):
                    row[f"lvl_{lvl}_id"] = asgn["rep_chain"].get(f"lvl_{lvl}_id")
                    row[f"lvl_{lvl}_id_fn"] = asgn["fn_chain"].get(f"lvl_{lvl}_id")
                snapshots.append(row)

    if profile == "nasty":
        # Zero-headcount period: one PPE wiped of A-status rows for its own snapshot
        zero_ppe = bppes[10]
        snapshots = [r for r in snapshots
                     if not (r["period_end_dt"] == zero_ppe.isoformat()
                             and r["snap_dt"] == zero_ppe.isoformat()
                             and r["status_cd"] == "A")]
        # Duplicate-row hazard removed: it violated the declared PK (assoc_id, period_end_dt, snap_dt),
        # and the real production fact is keyed. Grain reasoning now trusts the declared key.

    # --- write JSONL per table (schema.table.jsonl)
    def jsonl(name: str, rows: list, keys: list):
        with (out_dir / f"{name}.jsonl").open("w") as f:
            for r in rows:
                if isinstance(r, dict):
                    f.write(json.dumps({k: r.get(k) for k in keys}, separators=(",", ":")) + "\n")
                else:
                    f.write(json.dumps({k: v for k, v in zip(keys, r)}, separators=(",", ":")) + "\n")

    jsonl("hr_ref.country_region",
          [dict(zip(["country_cd", "region"], row)) for row in ref_country],
          ["country_cd", "region"])
    jsonl("hr_ref.job_group_category",
          [dict(zip(["job_grp", "category"], row)) for row in ref_job],
          ["job_grp", "category"])
    jsonl("hr_ref.status_code_meaning",
          [dict(zip(["status_cd", "meaning"], row)) for row in ref_status],
          ["status_cd", "meaning"])
    jsonl("hr.pay_period_dim", pay_period,
          ["ppe_dt", "freq_cd", "period_start_dt", "period_seq", "is_current_flag"])
    hierarchy_rows = [dict(zip(["lvl", "node_id", "node_name", "parent_node_id", "hierarchy_kind"], h))
                      for h in hierarchy]
    jsonl("hr.hierarchy_dim", hierarchy_rows,
          ["lvl", "node_id", "node_name", "parent_node_id", "hierarchy_kind"])
    jsonl("hr.employee_dim", employees,
          ["assoc_id", "given_name", "family_name", "hire_dt", "term_dt", "mgr_assoc_id"])
    snap_cols = ["snap_dt", "period_end_dt", "assoc_id", "hire_dt", "term_dt", "status_cd",
                 "invol_exit_ind", "new_hire_ind", "exit_filt_ind", "turn_filt_ind",
                 "region", "segment", "country", "job_grp", "assoc_type",
                 "lvl_2_id", "lvl_3_id", "lvl_4_id", "lvl_5_id", "lvl_6_id", "lvl_7_id",
                 "lvl_2_id_fn", "lvl_3_id_fn", "lvl_4_id_fn", "lvl_5_id_fn", "lvl_6_id_fn", "lvl_7_id_fn"]
    jsonl("hr.emp_period_snapshot", snapshots, snap_cols)

    print(f"seed={seed} profile={profile}: pay_period={len(pay_period)} "
          f"hierarchy={len(hierarchy)} employees={len(employees)} snapshots={len(snapshots)}")
    return {
        "pay_period": len(pay_period), "hierarchy": len(hierarchy),
        "employees": len(employees), "snapshots": len(snapshots),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--profile", choices=["clean", "nasty"], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    generate(args.seed, args.profile, Path(args.out))


if __name__ == "__main__":
    main()
