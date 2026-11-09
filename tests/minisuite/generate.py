"""Deterministic data for the minisuite. Standard library only."""
import argparse
import json
import random
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--profile", choices=["clean", "nasty"], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    customers, orders = [], []
    countries = ["US", "DE", "IN"]
    for cid in range(1, 21):
        customers.append({
            "id": cid,
            "tenant_id": 1 + cid % 2,
            "name": f"Customer {cid:02d}",
            "country": countries[cid % 3],
            "is_vip": cid % 5 == 0,
            "deleted_at": None,
        })
    oid = 1000
    for customer in customers:
        for _ in range(rng.randint(1, 4)):
            oid += 1
            orders.append({
                "id": oid,
                "customer_id": customer["id"],
                "tenant_id": customer["tenant_id"],
                "total_cents": rng.randint(1, 500) * 100,
                "status": rng.choice(["paid", "paid", "open", "cancelled"]),
                "created_at": f"2026-0{rng.randint(1, 6)}-{rng.randint(10, 28)}T12:00:00+00:00",
            })
    if args.profile == "nasty":
        customers[1]["deleted_at"] = "2026-05-01T00:00:00+00:00"  # customer 2, in tenant 1
        customers[4]["country"] = None
        customers[6]["name"] = customers[7]["name"]
        for order in orders:
            if order["customer_id"] == 2:
                order["status"] = "paid"
    for name, rows in (("shop.customers", customers), ("shop.orders", orders)):
        with open(out / f"{name}.jsonl", "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
