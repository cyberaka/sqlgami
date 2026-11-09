#!/usr/bin/env python3
"""
saas_billing deterministic generator.

Usage:
    python generate.py --seed N --profile clean|nasty --out DIR

Produces one JSONL file per base table at DIR/<schema>.<table>.jsonl.
Standard library only, Python 3.10+. Entropy comes from random.Random(seed).
No wall-clock time: everything derived from AS_OF.
"""

import argparse
import json
import os
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


AS_OF = datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()


# ---------------------------------------------------------------------------
# Word pools (small, embedded; no faker, no external data)
# ---------------------------------------------------------------------------

COMPANY_PREFIXES = [
    "Acme", "Globex", "Initech", "Umbra", "Hooli", "Soylent", "Massive", "Vandelay",
    "Pied", "Vehement", "Dunder", "Wayne", "Stark", "Cyberdyne", "Tyrell", "Weyland",
    "Oscorp", "LexCorp", "Roxxon", "Yoyodyne", "Prometheus", "Chrono", "Quantum",
    "Horizon", "Apex", "Vertex", "Zenith", "Meridian", "Pinnacle", "Harbor"
]
COMPANY_SUFFIXES = [
    "Systems", "Technologies", "Dynamics", "Solutions", "Holdings", "Industries",
    "Labs", "Networks", "Partners", "Group", "Analytics", "Software", "Services",
    "International", "Ventures"
]
FIRST_NAMES = [
    "Aditi", "Rohan", "Mei", "Priya", "Lars", "Yuki", "Ana", "Kai", "Nia", "Omar",
    "Sven", "Tara", "Zara", "Kiran", "Luca", "Ines", "Noa", "Ravi", "Hana", "Finn",
    "Aisha", "Diego", "Lena", "Jin", "Amara", "Luis", "Chiara", "Niall", "Sanaa",
    "Eli", "Maya", "Idris", "Nora", "Juno", "Tomas", "Freya", "Rowan", "Sage"
]
LAST_NAMES = [
    "Patel", "Chen", "Kim", "Lopez", "Nakamura", "Oduya", "Rossi", "Singh", "Tanaka",
    "Weber", "Carvalho", "Diallo", "Eriksen", "Ferrari", "Guerrero", "Haddad",
    "Ibarra", "Johansson", "Khoury", "Larsson", "Mehta", "Novak", "Okafor", "Park",
    "Quintana", "Rahimi", "Silva", "Takahashi", "Umeki", "Volkov"
]
STREETS = [
    "Elm", "Oak", "Maple", "Cedar", "Pine", "Willow", "Birch", "Ash", "Juniper",
    "Chestnut", "Linden", "Magnolia", "Poplar", "Sycamore", "Hawthorn"
]
STREET_TYPE = ["Street", "Avenue", "Road", "Lane", "Court", "Boulevard", "Way"]

# (city, region, country_code, jurisdiction_slug)
CITIES = [
    ("Mumbai",         "MH", "IN", "in_mh"),
    ("Bengaluru",      "KA", "IN", "in_ka"),
    ("Tokyo",          "13", "JP", "jp_tokyo"),
    ("Osaka",          "27", "JP", "jp_osaka"),
    ("San Francisco",  "CA", "US", "us_ca"),
    ("Seattle",        "WA", "US", "us_wa"),
    ("London",         "LND", "GB", "gb_london"),
    ("Manchester",     "MAN", "GB", "gb_manchester"),
    ("Berlin",         "BE", "DE", "de_berlin"),
    ("Munich",         "BY", "DE", "de_munich"),
]

TENANT_TIMEZONES = [
    ("Asia/Kolkata",         False),  # no DST
    ("Asia/Tokyo",           False),  # no DST
    ("America/Los_Angeles",  True),   # DST
    ("Europe/London",        True),   # DST
    ("Asia/Kolkata",         False),
    ("America/Los_Angeles",  True),
]

PLAN_INTERVALS = ["month", "year"]
PLAN_SLUGS = [
    ("starter",    "Starter",    "month",  2900,  "USD"),
    ("pro",        "Pro",        "month",  9900,  "USD"),
    ("business",   "Business",   "month", 29900,  "USD"),
    ("enterprise", "Enterprise", "month", 99900,  "USD"),
    ("pro_yr",     "Pro Yearly", "year", 103000,  "USD"),
    ("biz_yr",     "Business Yearly", "year", 310000, "USD"),
    ("starter_in", "Starter (India)", "month", 199900, "INR"),
    ("pro_in",     "Pro (India)",     "month", 699900, "INR"),
]

FEATURES = [
    ("api_access",       "API Access",        "integration"),
    ("priority_support", "Priority Support",  "support"),
    ("sso",              "Single Sign-On",    "security"),
    ("audit_log",        "Audit Log",         "security"),
    ("bulk_export",      "Bulk Export",       "data"),
    ("webhooks",         "Webhooks",          "integration"),
    ("custom_branding",  "Custom Branding",   "branding"),
    ("team_seats",       "Team Seats",        "collaboration"),
    ("sandbox",          "Sandbox Environment","developer"),
    ("sla",              "Service Level Agreement", "support"),
    ("white_label",      "White Label",       "branding"),
    ("ip_allowlist",     "IP Allowlist",      "security"),
    ("advanced_reports", "Advanced Reports",  "analytics"),
]

METERS = [
    ("api_calls",     "API Calls",       "call",     "sum"),
    ("storage_gb",    "Storage",         "gb_month", "max"),
    ("bandwidth_gb",  "Bandwidth",       "gb",       "sum"),
    ("seats",         "Active Seats",    "seat",     "max"),
    ("sms_sent",      "SMS Sent",        "message",  "sum"),
]

PERMISSIONS_BY_ROLE = {
    "owner":   ["billing.read", "billing.write", "admin.read", "admin.write", "users.read", "users.write"],
    "admin":   ["billing.read", "admin.read", "admin.write", "users.read", "users.write"],
    "billing": ["billing.read", "billing.write"],
    "member":  ["users.read"],
    "viewer":  [],
}

PAYMENT_BRANDS = ["visa", "mastercard", "amex", "discover"]
INVOICE_STATUSES = ["paid", "open", "overdue", "void", "partial"]
SUB_STATUSES = ["active", "past_due", "canceled", "trialing"]
DISPUTE_REASONS = [
    "duplicate_charge", "fraudulent", "product_not_received",
    "service_not_as_described", "credit_not_processed"
]
EMAIL_TEMPLATES = ["invoice_issued", "payment_failed", "trial_ending", "renewal_reminder"]


def iso(dt):
    """Return an ISO 8601 UTC timestamp with +00:00 offset."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def iso_date(d):
    if d is None:
        return None
    return d.strftime("%Y-%m-%d")


class JsonlWriter:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.handles = {}

    def write(self, schema: str, table: str, row: dict):
        key = f"{schema}.{table}"
        h = self.handles.get(key)
        if h is None:
            path = self.out_dir / f"{key}.jsonl"
            h = open(path, "w", encoding="utf-8", newline="\n")
            self.handles[key] = h
        # keys sorted for byte-stable output
        h.write(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        h.write("\n")

    def close(self):
        for h in self.handles.values():
            h.close()


# ---------------------------------------------------------------------------
# Deterministic ID allocator
# ---------------------------------------------------------------------------

class IdPool:
    def __init__(self, base: int):
        self.base = base
        self.n = 0

    def next(self):
        self.n += 1
        return self.base + self.n


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class Gen:
    def __init__(self, seed: int, profile: str, out_dir: Path):
        self.seed = seed
        self.profile = profile  # 'clean' or 'nasty'
        self.out_dir = out_dir
        self.rnd = random.Random(seed)
        self.writer = JsonlWriter(out_dir)
        self.ids = {}
        self.data = {}  # cached lookups for referential integrity

    def _pool(self, name: str, base: int):
        if name not in self.ids:
            self.ids[name] = IdPool(base)
        return self.ids[name]

    def random_name(self):
        return f"{self.rnd.choice(FIRST_NAMES)} {self.rnd.choice(LAST_NAMES)}"

    def random_email(self, display_name=None, tenant_slug=None):
        first = self.rnd.choice(FIRST_NAMES).lower()
        last = self.rnd.choice(LAST_NAMES).lower()
        if tenant_slug:
            return f"{first}.{last}@{tenant_slug}.test"
        return f"{first}.{last}@example.test"

    def random_company_name(self):
        return f"{self.rnd.choice(COMPANY_PREFIXES)} {self.rnd.choice(COMPANY_SUFFIXES)}"

    def random_timestamp_between(self, start_dt: datetime, end_dt: datetime):
        """Uniform sample. Both bounds timezone-aware (UTC)."""
        delta = int((end_dt - start_dt).total_seconds())
        if delta <= 0:
            return start_dt
        s = self.rnd.randint(0, delta)
        return start_dt + timedelta(seconds=s)

    # -----------------------------------------------------------------------

    def build_currencies(self):
        rows = [
            ("USD", "US Dollar", 100),
            ("EUR", "Euro",      100),
            ("GBP", "British Pound", 100),
            ("INR", "Indian Rupee",  100),
            ("JPY", "Japanese Yen",  1),   # minor unit = 1 (no sub-unit)
        ]
        for code, name, scale in rows:
            self.writer.write("billing", "currencies", {
                "currency_code": code, "name": name, "minor_unit_scale": scale
            })
        self.data["currencies"] = [r[0] for r in rows]

    def build_exchange_rates(self):
        # Rates from USD to {EUR,GBP,INR,JPY} daily for H1 2026
        pairs = [
            ("USD", "EUR", 92_000_000),   # 0.92
            ("USD", "GBP", 78_000_000),   # 0.78
            ("USD", "INR", 8_300_000_000),# 83.00
            ("USD", "JPY", 155_000_000_00), # 155.00 — wait that's 15500 at e8. Let me fix: 155 * 1e8 = 15_500_000_000
        ]
        # Fix JPY rate properly: 1 USD = 155 JPY -> rate_e8 = 155 * 1e8 = 15_500_000_000
        pairs = [
            ("USD", "EUR", 92_000_000),         # 0.92
            ("USD", "GBP", 78_000_000),         # 0.78
            ("USD", "INR", 8_300_000_000),      # 83.00
            ("USD", "JPY", 15_500_000_000),     # 155.00
        ]
        start = date(2026, 1, 1)
        for day_offset in range(0, (AS_OF_DATE - start).days + 1, 7):  # weekly to stay under row budget
            d = start + timedelta(days=day_offset)
            for from_ccy, to_ccy, base in pairs:
                jitter = self.rnd.randint(-int(base * 0.02), int(base * 0.02))
                rate = base + jitter
                self.writer.write("billing", "exchange_rates", {
                    "from_ccy": from_ccy, "to_ccy": to_ccy,
                    "rate_date": iso_date(d),
                    "rate_e8": rate,
                    "source": "ecb_mock"
                })
                # And the reverse
                reverse = int((10**16) // rate)
                self.writer.write("billing", "exchange_rates", {
                    "from_ccy": to_ccy, "to_ccy": from_ccy,
                    "rate_date": iso_date(d),
                    "rate_e8": reverse,
                    "source": "ecb_mock"
                })

    def build_tax_jurisdictions(self):
        pool = self._pool("juris", 1000)
        juris = {}
        # Countries (level=country)
        for code, name in [
            ("US", "United States"), ("IN", "India"), ("JP", "Japan"),
            ("GB", "United Kingdom"), ("DE", "Germany")
        ]:
            jid = pool.next()
            juris[code] = jid
            self.writer.write("billing", "tax_jurisdictions", {
                "jurisdiction_id": jid, "name": name, "code": code,
                "parent_id": None, "level": "country"
            })
        # Regions/states (level=region) with parent
        regions = [
            ("US-CA", "California", "US"),
            ("US-WA", "Washington", "US"),
            ("IN-MH", "Maharashtra", "IN"),
            ("IN-KA", "Karnataka",   "IN"),
            ("JP-13", "Tokyo Metro", "JP"),
            ("JP-27", "Osaka Pref",  "JP"),
            ("GB-LND","Greater London", "GB"),
            ("GB-MAN","Greater Manchester", "GB"),
            ("DE-BE", "Berlin", "DE"),
            ("DE-BY", "Bavaria","DE"),
        ]
        for code, name, parent_code in regions:
            jid = pool.next()
            juris[code] = jid
            self.writer.write("billing", "tax_jurisdictions", {
                "jurisdiction_id": jid, "name": name, "code": code,
                "parent_id": juris[parent_code], "level": "region"
            })
        # A few cities (level=city) — exercises the recursive CTE further
        for city, region, country in [("San Francisco", "US-CA", "US"),
                                       ("Seattle", "US-WA", "US"),
                                       ("Bengaluru", "IN-KA", "IN"),
                                       ("Mumbai",    "IN-MH", "IN"),
                                       ("Tokyo",     "JP-13", "JP"),
                                       ("Osaka",     "JP-27", "JP"),
                                       ("London",    "GB-LND","GB"),
                                       ("Manchester","GB-MAN","GB"),
                                       ("Berlin",    "DE-BE", "DE"),
                                       ("Munich",    "DE-BY", "DE")]:
            jid = pool.next()
            juris[f"CITY-{city}"] = jid
            self.writer.write("billing", "tax_jurisdictions", {
                "jurisdiction_id": jid, "name": city, "code": f"{country}-{city[:3].upper()}",
                "parent_id": juris[region], "level": "city"
            })
        self.data["juris"] = juris

    def build_tax_rates(self):
        pool = self._pool("tax_rate", 2000)
        # Pick regions and add SCD2: an old rate (effective_to=2026-01-01) and a current rate (effective_to=NULL)
        regions_with_rate = [
            ("US-CA",  7_250_000, 7_250_000),    # old same new
            ("US-WA",  6_500_000, 6_500_000),
            ("IN-MH", 18_000_000, 18_000_000),
            ("IN-KA", 18_000_000, 18_000_000),
            ("JP-13", 10_000_000, 10_000_000),
            ("JP-27", 10_000_000, 10_000_000),
            ("GB-LND",20_000_000, 20_000_000),
            ("GB-MAN",20_000_000, 20_000_000),
            ("DE-BE", 19_000_000, 19_000_000),
            ("DE-BY", 19_000_000, 19_000_000),
        ]
        rates = []
        for code, old_pct, new_pct in regions_with_rate:
            jid = self.data["juris"][code]
            # Historical
            trid = pool.next()
            self.writer.write("billing", "tax_rates", {
                "tax_rate_id": trid, "jurisdiction_id": jid,
                "pct_e6": old_pct,
                "effective_from": iso_date(date(2024, 1, 1)),
                "effective_to":   iso_date(date(2026, 1, 1)),
            })
            # Current
            trid2 = pool.next()
            self.writer.write("billing", "tax_rates", {
                "tax_rate_id": trid2, "jurisdiction_id": jid,
                "pct_e6": new_pct,
                "effective_from": iso_date(date(2026, 1, 1)),
                "effective_to": None,
            })
            rates.append((code, trid, trid2))
        self.data["tax_rates"] = rates
        self.data["current_tax_rate_by_juris"] = {code: trid2 for code, _, trid2 in rates}

    def build_usage_meters(self):
        pool = self._pool("meter", 3000)
        meters = {}
        for code, name, unit, agg in METERS:
            mid = pool.next()
            meters[code] = mid
            self.writer.write("billing", "usage_meters", {
                "meter_id": mid, "code": code, "display_name": name,
                "unit": unit, "aggregation": agg
            })
        self.data["meters"] = meters

    def build_plans(self):
        pool = self._pool("plan", 4000)
        plans = []
        for code, name, interval, price, ccy in PLAN_SLUGS:
            pid = pool.next()
            plans.append({
                "plan_id": pid, "code": code, "display_name": name,
                "billing_interval": interval, "base_price_cents": price,
                "currency_code": ccy, "is_active": True,
                "created_at": iso(datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc))
            })
            self.writer.write("billing", "plans", plans[-1])
        # Add one deprecated plan (is_active=False)
        pid = pool.next()
        plans.append({
            "plan_id": pid, "code": "legacy_basic", "display_name": "Legacy Basic",
            "billing_interval": "month", "base_price_cents": 1900,
            "currency_code": "USD", "is_active": False,
            "created_at": iso(datetime(2022, 6, 1, 12, 0, tzinfo=timezone.utc))
        })
        self.writer.write("billing", "plans", plans[-1])
        self.data["plans"] = plans

    def build_features_and_plan_features(self):
        pool = self._pool("feature", 5000)
        features = {}
        for code, name, cat in FEATURES:
            fid = pool.next()
            features[code] = fid
            self.writer.write("billing", "features", {
                "feature_id": fid, "code": code, "display_name": name, "category": cat
            })
        self.data["features"] = features
        # PlanFeatures M2M with some quotas
        for plan in self.data["plans"]:
            level = plan["code"]
            if "starter" in level:
                codes = ["api_access", "webhooks", "team_seats"]
                quotas = {"api_access": 10_000, "team_seats": 5}
            elif "pro" in level:
                codes = ["api_access", "webhooks", "team_seats", "sso", "priority_support", "bulk_export"]
                quotas = {"api_access": 100_000, "team_seats": 25}
            elif "biz" in level or "business" in level:
                codes = ["api_access", "webhooks", "team_seats", "sso", "priority_support",
                         "bulk_export", "audit_log", "advanced_reports", "sandbox"]
                quotas = {"api_access": 1_000_000, "team_seats": 200}
            elif "enterprise" in level:
                codes = list(features.keys())
                quotas = {"api_access": 10_000_000, "team_seats": 1000}
            else:
                codes = ["api_access"]
                quotas = {"api_access": 5_000}
            for c in codes:
                self.writer.write("billing", "PlanFeatures", {
                    "plan_id": plan["plan_id"],
                    "feature_id": features[c],
                    "quota": quotas.get(c)
                })

    def build_usage_prices(self):
        pool = self._pool("usage_price", 6000)
        for plan in self.data["plans"]:
            if plan["code"] == "legacy_basic":
                continue
            # Price per API call: 0.0001 USD = 1 e4 unit (unit_price_e4=1)
            # 1000 included for starter, more for pro, etc.
            if "starter" in plan["code"]:
                included = 10_000
                price_e4 = 2  # 0.0002 cents per call over quota
            elif "pro" in plan["code"]:
                included = 100_000
                price_e4 = 1
            elif "biz" in plan["code"] or "business" in plan["code"]:
                included = 1_000_000
                price_e4 = 1
            elif "enterprise" in plan["code"]:
                included = 10_000_000
                price_e4 = 1
            else:
                included = 5_000
                price_e4 = 3

            upid = pool.next()
            self.writer.write("billing", "usage_prices", {
                "usage_price_id": upid,
                "plan_id": plan["plan_id"],
                "meter_id": self.data["meters"]["api_calls"],
                "unit_price_e4": price_e4,
                "included_units": included,
                "effective_from": iso_date(date(2024, 1, 1)),
                "effective_to":   iso_date(date(2026, 1, 1)),
            })
            upid2 = pool.next()
            self.writer.write("billing", "usage_prices", {
                "usage_price_id": upid2,
                "plan_id": plan["plan_id"],
                "meter_id": self.data["meters"]["api_calls"],
                "unit_price_e4": price_e4,
                "included_units": included,
                "effective_from": iso_date(date(2026, 1, 1)),
                "effective_to": None,
            })

    def build_tenants(self):
        pool = self._pool("tenant", 10_000)
        tenants = []
        # 6 tenants
        n = 6
        country_assignments = [
            ("US", "America/Los_Angeles", "USD"),
            ("IN", "Asia/Kolkata",        "INR"),
            ("JP", "Asia/Tokyo",          "USD"),
            ("GB", "Europe/London",       "GBP"),
            ("DE", "Europe/London",       "EUR"),  # DE businesses often bill via a London entity
            ("US", "America/Los_Angeles", "USD"),
        ]
        for i in range(n):
            tid = pool.next()
            country, tz, ccy = country_assignments[i]
            name = self.random_company_name()
            slug = name.lower().replace(" ", "-")
            tenants.append({
                "tenant_id": tid, "name": name, "slug": slug,
                "billing_timezone": tz, "currency_code": ccy,
                "created_at": iso(datetime(2024, 1, 1) + timedelta(days=self.rnd.randint(0, 300))),
                "deleted_at": None,
                "_country": country,
            })
        # One soft-deleted tenant
        tenants[-1]["deleted_at"] = iso(datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc))
        # Write
        for t in tenants:
            out = {k: v for k, v in t.items() if not k.startswith("_")}
            self.writer.write("billing", "tenants", out)
        self.data["tenants"] = tenants

    def build_users_and_memberships(self):
        pool = self._pool("user", 20_000)
        users = []
        memberships = []
        # ~15 users per tenant, 15*6 = 90 users
        emails_seen = set()
        for t in self.data["tenants"]:
            for _ in range(15):
                uid = pool.next()
                first = self.rnd.choice(FIRST_NAMES).lower()
                last = self.rnd.choice(LAST_NAMES).lower()
                email = f"{first}.{last}@{t['slug']}.test"
                # Ensure uniqueness
                if email in emails_seen:
                    email = f"{first}.{last}{uid}@{t['slug']}.test"
                emails_seen.add(email)
                users.append({
                    "user_id": uid,
                    "email": email,
                    "display_name": f"{first.capitalize()} {last.capitalize()}",
                    "created_at": iso(datetime(2024, 1, 1) + timedelta(days=self.rnd.randint(30, 700))),
                    "deleted_at": None,
                    "_tenant_id": t["tenant_id"],
                })
                # Assign role
                role_idx = self.rnd.randint(1, 5)
                memberships.append({
                    "user_id": uid,
                    "tenant_id": t["tenant_id"],
                    "_role_idx": role_idx,
                })
        for u in users:
            out = {k: v for k, v in u.items() if not k.startswith("_")}
            self.writer.write("billing", "users", out)
        self.data["users"] = users
        self.data["memberships"] = memberships

    def build_roles(self):
        pool = self._pool("role", 30_000)
        roles = {}
        for i, (name, perms) in enumerate(PERMISSIONS_BY_ROLE.items(), start=1):
            rid = pool.next()
            roles[name] = rid
            is_system = name in ("owner", "admin", "viewer")
            self.writer.write("billing", "roles", {
                "role_id": rid, "name": name, "is_system": is_system,
                "permissions": perms,
            })
        self.data["roles"] = roles
        self.data["role_by_idx"] = {1: "owner", 2: "admin", 3: "billing", 4: "member", 5: "viewer"}

    def emit_memberships(self):
        for m in self.data["memberships"]:
            role_name = self.data["role_by_idx"][m["_role_idx"]]
            self.writer.write("billing", "memberships", {
                "user_id": m["user_id"],
                "tenant_id": m["tenant_id"],
                "role_id": self.data["roles"][role_name],
                "joined_at": iso(datetime(2024, 1, 1) + timedelta(days=self.rnd.randint(0, 700))),
                "left_at": None,
            })

    def build_tenant_settings(self):
        for t in self.data["tenants"]:
            self.writer.write("billing", "tenant_settings", {
                "tenant_id": t["tenant_id"],
                "settings": {
                    "notifications_enabled": True,
                    "default_currency": t["currency_code"],
                    "fiscal_year_start_month": 1 if t["_country"] in ("US", "GB", "DE") else 4,
                    "allow_anonymous_api": False,
                },
                "updated_at": iso(datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc)),
            })

    def build_billing_contacts(self):
        pool = self._pool("contact", 40_000)
        for t in self.data["tenants"]:
            n = self.rnd.randint(1, 3)
            for i in range(n):
                cid = pool.next()
                first = self.rnd.choice(FIRST_NAMES)
                last = self.rnd.choice(LAST_NAMES)
                self.writer.write("billing", "billing_contacts", {
                    "contact_id": cid,
                    "tenant_id": t["tenant_id"],
                    "name": f"{first} {last}",
                    "email": f"billing+{cid}@{t['slug']}.test",
                    "is_primary": (i == 0),
                    "created_at": iso(datetime(2024, 3, 1) + timedelta(days=self.rnd.randint(0, 500))),
                })

    def build_addresses(self):
        pool = self._pool("addr", 50_000)
        for t in self.data["tenants"]:
            # Each tenant gets 1-2 current addresses + 1 historical
            city_choice = self.rnd.choice(CITIES)
            city, region, country, juris_slug = city_choice
            address_id = pool.next()
            # Historical (effective 2024-01 to 2025-07)
            hist_juris = self.data["juris"].get(f"CITY-{city}") or self.data["juris"].get(f"{country}-{region}")
            # fall back
            juris_code = f"{country}-{region}"
            hist_juris = self.data["juris"].get(juris_code, list(self.data["juris"].values())[0])
            self.writer.write("billing", "addresses", {
                "address_id": address_id,
                "tenant_id": t["tenant_id"],
                "line1": f"{self.rnd.randint(1, 999)} {self.rnd.choice(STREETS)} {self.rnd.choice(STREET_TYPE)}",
                "line2": None,
                "city": city, "region": region, "postcode": f"{self.rnd.randint(10000, 99999)}",
                "country_code": country,
                "jurisdiction_id": hist_juris,
                "effective_from": iso_date(date(2024, 1, 1)),
                "effective_to": iso_date(date(2025, 7, 1)),
            })
            # Current
            self.writer.write("billing", "addresses", {
                "address_id": address_id,
                "tenant_id": t["tenant_id"],
                "line1": f"{self.rnd.randint(1, 999)} {self.rnd.choice(STREETS)} {self.rnd.choice(STREET_TYPE)}",
                "line2": None,
                "city": city, "region": region, "postcode": f"{self.rnd.randint(10000, 99999)}",
                "country_code": country,
                "jurisdiction_id": hist_juris,
                "effective_from": iso_date(date(2025, 7, 1)),
                "effective_to": None,
            })

    def build_payment_methods(self):
        pool = self._pool("pm", 60_000)
        self.data["payment_methods"] = {}
        for t in self.data["tenants"]:
            pms = []
            n = self.rnd.randint(1, 3)
            for i in range(n):
                pmid = pool.next()
                pms.append(pmid)
                self.writer.write("billing", "payment_methods", {
                    "payment_method_id": pmid,
                    "tenant_id": t["tenant_id"],
                    "kind": "card",
                    "brand": self.rnd.choice(PAYMENT_BRANDS),
                    "last4": f"{self.rnd.randint(1000, 9999)}",
                    "is_default": (i == 0),
                    "created_at": iso(datetime(2024, 4, 1) + timedelta(days=self.rnd.randint(0, 500))),
                    "deleted_at": None,
                })
            self.data["payment_methods"][t["tenant_id"]] = pms

    def build_subscriptions_items_invoices(self):
        sub_pool = self._pool("sub", 100_000)
        subitem_pool = self._pool("subitem", 110_000)
        inv_pool = self._pool("inv", 200_000)
        line_pool = self._pool("line", 300_000)
        adj_pool = self._pool("adj", 400_000)
        pay_pool = self._pool("pay", 500_000)
        refund_pool = self._pool("refund", 550_000)

        self.data["subscriptions"] = []
        self.data["invoices"] = []
        self.data["payments"] = []

        for t in self.data["tenants"]:
            # 2-4 subscriptions per tenant, some ended, most active
            n_subs = self.rnd.randint(2, 4)
            # Plan selection: prefer tenant's currency, fall back to USD.
            tenant_plans = [p for p in self.data["plans"] if p["currency_code"] == t["currency_code"] and p["is_active"]]
            if not tenant_plans:
                tenant_plans = [p for p in self.data["plans"] if p["currency_code"] == "USD" and p["is_active"]]
            for s_ix in range(n_subs):
                plan = self.rnd.choice(tenant_plans)
                started_at = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=self.rnd.randint(0, 450))
                ended_at = None
                status = "active"
                if s_ix >= n_subs - 1 and self.rnd.random() < 0.5:
                    # last sub is sometimes ended
                    ended_at = started_at + timedelta(days=self.rnd.randint(60, 300))
                    if ended_at > AS_OF:
                        ended_at = None
                        status = "active"
                    else:
                        status = self.rnd.choice(["canceled", "past_due"])
                sub_id = sub_pool.next()
                sub = {
                    "subscription_id": sub_id,
                    "tenant_id": t["tenant_id"],
                    "plan_id": plan["plan_id"],
                    "started_at": iso(started_at),
                    "ended_at": iso(ended_at) if ended_at else None,
                    "status": status,
                    "created_at": iso(started_at),
                    "deleted_at": None,
                }
                self.writer.write("billing", "subscriptions", sub)
                self.data["subscriptions"].append({**sub, "_plan": plan})

                # Subscription items (1-3)
                n_items = self.rnd.randint(1, 3)
                for seq in range(1, n_items + 1):
                    self.writer.write("billing", "subscription_items", {
                        "subscription_id": sub_id,
                        "item_seq": seq,
                        "meter_id": self.data["meters"]["api_calls"] if seq == 1 else None,
                        "quantity": self.rnd.randint(1, 10),
                        "unit_price_e4": plan["base_price_cents"] * 100,  # convert cents to e4
                        "created_at": iso(started_at),
                    })

                # Invoices: monthly from started_at to AS_OF (capped at 6)
                cur = started_at.replace(day=1)
                inv_count = 0
                while cur < AS_OF and inv_count < 6:
                    period_start = cur.date()
                    nxt_month = cur.month + 1
                    nxt_year = cur.year
                    if nxt_month > 12:
                        nxt_month -= 12
                        nxt_year += 1
                    period_end = date(nxt_year, nxt_month, 1)
                    issued = datetime(cur.year, cur.month, min(28, self.rnd.randint(1, 28)), 10, 0, tzinfo=timezone.utc)

                    inv_id = inv_pool.next()
                    subtotal = plan["base_price_cents"]
                    tax_rate = self.rnd.choice([0, 7250000, 18000000, 20000000, 10000000])
                    tax_cents = subtotal * tax_rate // 100_000_000  # pct_e6 is percent*1e6
                    total = subtotal + tax_cents
                    inv_status = self.rnd.choices(
                        ["paid", "paid", "paid", "open", "overdue", "partial"], k=1
                    )[0]
                    inv = {
                        "invoice_id": inv_id,
                        "tenant_id": t["tenant_id"],
                        "subscription_id": sub_id,
                        "invoice_number": f"INV-{t['tenant_id']:05d}-{inv_id:07d}",
                        "issued_at": iso(issued),
                        "period_start": iso_date(period_start),
                        "period_end":   iso_date(period_end),
                        "currency_code": plan["currency_code"],
                        "subtotal_cents": subtotal,
                        "tax_cents": tax_cents,
                        "total_cents": total,
                        "status": inv_status,
                        "IsAutoBilled": True,
                        "due_date": iso_date(period_end + timedelta(days=14)),
                        "created_at": iso(issued),
                        "deleted_at": None,
                    }
                    self.writer.write("billing", "invoices", inv)
                    self.data["invoices"].append(inv)

                    # Line items (1-3)
                    n_lines = self.rnd.randint(1, 3)
                    line_sum = 0
                    for ln in range(n_lines):
                        line_amount = subtotal if ln == 0 else self.rnd.randint(500, 5000)
                        if ln > 0:
                            line_sum += line_amount
                        line_id = line_pool.next()
                        self.writer.write("billing", "invoice_line_items", {
                            "line_item_id": line_id,
                            "invoice_id": inv_id,
                            "subscription_item_seq": 1 if ln == 0 else None,
                            "subscription_id": sub_id if ln == 0 else None,
                            "description": f"Plan: {plan['display_name']}" if ln == 0 else "Overage",
                            "quantity": 1,
                            "unit_price_e4": line_amount * 100,
                            "amount_cents": line_amount,
                            "tax_rate_id": None,
                            "coupon_redemption_id": None,
                            "created_at": iso(issued),
                        })

                    # Payment if paid
                    if inv_status in ("paid", "partial"):
                        pm_id = self.rnd.choice(self.data["payment_methods"][t["tenant_id"]])
                        pay_id = pay_pool.next()
                        paid_amount = total if inv_status == "paid" else total // 2
                        self.writer.write("billing", "payments", {
                            "payment_id": pay_id,
                            "tenant_id": t["tenant_id"],
                            "invoice_id": inv_id,
                            "payment_method_id": pm_id,
                            "amount_cents": paid_amount,
                            "currency_code": plan["currency_code"],
                            "status": "succeeded",
                            "paid_at": iso(issued + timedelta(days=self.rnd.randint(0, 10))),
                            "created_at": iso(issued),
                        })
                        self.data["payments"].append({"payment_id": pay_id, "invoice_id": inv_id, "tenant_id": t["tenant_id"], "amount_cents": paid_amount})

                        # Occasional refund (~5%)
                        if self.rnd.random() < 0.05:
                            refund_id = refund_pool.next()
                            self.writer.write("billing", "refunds", {
                                "refund_id": refund_id,
                                "payment_id": pay_id,
                                "invoice_id": inv_id,
                                "tenant_id": t["tenant_id"],
                                "amount_cents": paid_amount // 2,
                                "reason": "customer_request",
                                "created_at": iso(issued + timedelta(days=self.rnd.randint(15, 60))),
                            })

                    # Occasional adjustment (~10%)
                    if self.rnd.random() < 0.10:
                        adj_id = adj_pool.next()
                        self.writer.write("billing", "invoice_adjustments", {
                            "adjustment_id": adj_id,
                            "invoice_id": inv_id,
                            "kind": "credit",
                            "amount_cents": -self.rnd.randint(100, 1000),
                            "reason": "goodwill",
                            "created_at": iso(issued + timedelta(days=self.rnd.randint(1, 20))),
                        })

                    cur = datetime(nxt_year, nxt_month, 1, tzinfo=timezone.utc)
                    inv_count += 1

    def build_credits(self):
        credit_pool = self._pool("credit", 600_000)
        tx_pool = self._pool("credittx", 700_000)
        for t in self.data["tenants"]:
            if self.rnd.random() < 0.6:
                continue
            for _ in range(self.rnd.randint(1, 2)):
                cid = credit_pool.next()
                amount = self.rnd.choice([5000, 10000, 25000, 50000])
                self.writer.write("billing", "credits", {
                    "credit_id": cid,
                    "tenant_id": t["tenant_id"],
                    "subscription_id": None,
                    "kind": "promo",
                    "amount_cents": amount,
                    "currency_code": t["currency_code"],
                    "expires_at": iso(datetime(2026, 12, 31, tzinfo=timezone.utc)),
                    "created_at": iso(datetime(2026, 1, 1) + timedelta(days=self.rnd.randint(0, 150))),
                })
                # A few transactions consuming the credit
                for _ in range(self.rnd.randint(0, 3)):
                    txid = tx_pool.next()
                    self.writer.write("billing", "credit_transactions", {
                        "credit_tx_id": txid,
                        "credit_id": cid,
                        "invoice_id": None,
                        "delta_cents": -self.rnd.randint(500, 2000),
                        "created_at": iso(datetime(2026, 2, 1) + timedelta(days=self.rnd.randint(0, 120))),
                    })

    def build_coupons_and_redemptions(self):
        coupon_pool = self._pool("coupon", 800_000)
        self.data["coupons"] = []
        for i in range(12):
            cid = coupon_pool.next()
            is_pct = self.rnd.random() < 0.5
            self.data["coupons"].append(cid)
            self.writer.write("billing", "coupons", {
                "coupon_id": cid,
                "code": f"COUPON{cid:04d}",
                "discount_pct_e6": self.rnd.choice([10_000_000, 15_000_000, 25_000_000]) if is_pct else None,
                "discount_cents": None if is_pct else self.rnd.choice([500, 1000, 2500]),
                "valid_from": iso_date(date(2026, 1, 1)),
                "valid_to":   iso_date(date(2026, 12, 31)),
                "max_redemptions": self.rnd.choice([None, 100, 500]),
                "created_at": iso(datetime(2025, 12, 15, tzinfo=timezone.utc)),
            })
        # Redemptions: a few invoices redeem coupons
        for i, inv in enumerate(self.data["invoices"]):
            if self.rnd.random() < 0.12:
                cid = self.rnd.choice(self.data["coupons"])
                self.writer.write("billing", "coupon_redemptions", {
                    "coupon_id": cid,
                    "invoice_id": inv["invoice_id"],
                    "seq": 1,
                    "amount_cents": self.rnd.randint(500, 3000),
                    "redeemed_at": inv["issued_at"],
                })

    def build_promotions(self):
        promo_pool = self._pool("promo", 900_000)
        self.data["promotions"] = []
        for i in range(8):
            pid = promo_pool.next()
            self.data["promotions"].append(pid)
            self.writer.write("billing", "promotions", {
                "promotion_id": pid,
                "name": f"Promo {chr(65 + i)} Q{self.rnd.randint(1,2)}",
                "starts_on": iso_date(date(2026, self.rnd.choice([1, 3, 5]), 1)),
                "ends_on":   iso_date(date(2026, self.rnd.choice([6, 9, 12]), 30)),
                "budget_cents": self.rnd.choice([None, 100_000, 500_000]),
                "created_at": iso(datetime(2025, 12, 1, tzinfo=timezone.utc)),
            })
        # Apply promotions to invoices (M2M)
        seen_pairs = set()
        for inv in self.data["invoices"]:
            if self.rnd.random() < 0.08:
                pid = self.rnd.choice(self.data["promotions"])
                pair = (pid, inv["invoice_id"])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                self.writer.write("billing", "promotion_applications", {
                    "promotion_id": pid,
                    "invoice_id": inv["invoice_id"],
                    "amount_cents": self.rnd.randint(200, 2000),
                    "applied_at": inv["issued_at"],
                })

    def build_usage_events(self):
        pool = self._pool("usage_event", 1_000_000)
        # ~24000 events spread Jan-Jun 2026 across tenants and meters
        total = 24000
        tenant_ids = [t["tenant_id"] for t in self.data["tenants"] if t["deleted_at"] is None]
        for _ in range(total):
            eid = pool.next()
            tid = self.rnd.choice(tenant_ids)
            meter_id = self.rnd.choice(list(self.data["meters"].values()))
            event_at = self.random_timestamp_between(
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc)
            )
            payload = {
                "endpoint": self.rnd.choice(["/v1/users", "/v1/orders", "/v1/webhooks", "/v1/reports"]),
                "latency_ms": self.rnd.randint(10, 500),
                "client": self.rnd.choice(["web", "mobile", "api"]),
            }
            self.writer.write("billing", "usage_events", {
                "event_id": eid,
                "tenant_id": tid,
                "subscription_id": None,
                "meter_id": meter_id,
                "event_at": iso(event_at),
                "quantity": self.rnd.randint(1, 100),
                "payload": payload,
            })

    # ---- audit tables ----

    def build_audit_log(self):
        pool = self._pool("audit_log", 2_000_000)
        for _ in range(2800):
            aid = pool.next()
            t = self.rnd.choice(self.data["tenants"])
            kind = self.rnd.choice(["invoice", "subscription", "payment_method", "user", "address"])
            self.writer.write("audit", "audit_log", {
                "audit_id": aid,
                "tenant_id": t["tenant_id"],
                "actor_user_id": self.rnd.choice([u["user_id"] for u in self.data["users"][:20]]),
                "entity_kind": kind,
                "entity_id": self.rnd.randint(1, 100_000),
                "action": self.rnd.choice(["create", "update", "delete", "soft_delete"]),
                "OldState": {"status": "open"} if self.rnd.random() < 0.5 else None,
                "NewState": {"status": self.rnd.choice(["paid", "closed", "void"])} if self.rnd.random() < 0.7 else None,
                "occurred_at": iso(self.random_timestamp_between(
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    AS_OF
                )),
            })

    def build_disputes(self):
        pool = self._pool("dispute", 2_100_000)
        event_pool = self._pool("dispute_event", 2_150_000)
        for _ in range(40):
            did = pool.next()
            t = self.rnd.choice(self.data["tenants"])
            paid_invs = [i for i in self.data["invoices"] if i["tenant_id"] == t["tenant_id"] and i["status"] == "paid"]
            if not paid_invs:
                continue
            inv = self.rnd.choice(paid_invs)
            opened = self.random_timestamp_between(
                datetime.fromisoformat(inv["issued_at"]) + timedelta(days=10),
                AS_OF
            )
            status = self.rnd.choice(["open", "open", "resolved", "lost"])
            self.writer.write("audit", "disputes", {
                "dispute_id": did,
                "tenant_id": t["tenant_id"],
                "invoice_id": inv["invoice_id"],
                "payment_id": None,
                "status": status,
                "reason": self.rnd.choice(DISPUTE_REASONS),
                "evidence": [{"doc": "receipt.pdf", "page": 1}],
                "opened_at": iso(opened),
                "resolved_at": iso(opened + timedelta(days=self.rnd.randint(5, 30))) if status != "open" else None,
            })
            for _ in range(self.rnd.randint(1, 4)):
                deid = event_pool.next()
                self.writer.write("audit", "dispute_events", {
                    "dispute_event_id": deid,
                    "dispute_id": did,
                    "event_kind": self.rnd.choice(["opened", "evidence_added", "closed", "note"]),
                    "note": None,
                    "occurred_at": iso(opened + timedelta(days=self.rnd.randint(0, 10))),
                })

    def build_emails_notifications(self):
        em_pool = self._pool("email", 2_200_000)
        for _ in range(1800):
            eid = em_pool.next()
            t = self.rnd.choice(self.data["tenants"])
            self.writer.write("audit", "email_events", {
                "email_event_id": eid,
                "tenant_id": t["tenant_id"],
                "to_address": f"billing+auto@{t['slug']}.test",
                "template": self.rnd.choice(EMAIL_TEMPLATES),
                "status": self.rnd.choice(["sent", "sent", "delivered", "bounced"]),
                "sent_at": iso(self.random_timestamp_between(
                    datetime(2026, 1, 1, tzinfo=timezone.utc), AS_OF
                )),
            })
        n_pool = self._pool("notif", 2_300_000)
        for _ in range(1800):
            nid = n_pool.next()
            t = self.rnd.choice(self.data["tenants"])
            user = self.rnd.choice([u for u in self.data["users"] if u["_tenant_id"] == t["tenant_id"]])
            invs = [i for i in self.data["invoices"] if i["tenant_id"] == t["tenant_id"]]
            inv = self.rnd.choice(invs) if invs else None
            self.writer.write("audit", "notifications", {
                "notification_id": nid,
                "tenant_id": t["tenant_id"],
                "user_id": user["user_id"],
                "invoice_id": inv["invoice_id"] if inv else None,
                "channel": self.rnd.choice(["email", "in_app", "sms"]),
                "status": self.rnd.choice(["pending", "sent", "read"]),
                "created_at": iso(self.random_timestamp_between(
                    datetime(2026, 1, 1, tzinfo=timezone.utc), AS_OF
                )),
                "read_at": None,
            })

    def build_webhooks(self):
        ep_pool = self._pool("endpoint", 2_400_000)
        dp_pool = self._pool("delivery", 2_500_000)
        endpoints = []
        for t in self.data["tenants"]:
            if self.rnd.random() < 0.7:
                for _ in range(self.rnd.randint(1, 3)):
                    eid = ep_pool.next()
                    endpoints.append((eid, t["tenant_id"]))
                    self.writer.write("audit", "webhook_endpoints", {
                        "endpoint_id": eid,
                        "tenant_id": t["tenant_id"],
                        "url": f"https://hooks.{t['slug']}.test/events",
                        "secret": f"whsec_{eid:08d}",
                        "is_active": True,
                        "created_at": iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                    })
        self.data["endpoints"] = endpoints
        for _ in range(900):
            did = dp_pool.next()
            eid, tid = self.rnd.choice(endpoints) if endpoints else (None, None)
            if eid is None:
                continue
            status = self.rnd.choices(["delivered", "delivered", "delivered", "failed", "retrying"], k=1)[0]
            self.writer.write("audit", "webhook_deliveries", {
                "delivery_id": did,
                "endpoint_id": eid,
                "event_kind": self.rnd.choice(["invoice.paid", "subscription.updated", "payment.failed"]),
                "status": status,
                "attempt": self.rnd.randint(1, 3),
                "body": {"id": did, "kind": "event"},
                "response_code": 200 if status == "delivered" else (self.rnd.choice([500, 502, 504]) if status == "failed" else None),
                "delivered_at": iso(self.random_timestamp_between(
                    datetime(2026, 1, 1, tzinfo=timezone.utc), AS_OF
                )),
            })

    def build_api(self):
        key_pool = self._pool("apikey", 2_600_000)
        req_pool = self._pool("apireq", 2_700_000)
        api_keys = []
        for t in self.data["tenants"]:
            for _ in range(self.rnd.randint(1, 3)):
                kid = key_pool.next()
                api_keys.append((kid, t["tenant_id"]))
                self.writer.write("audit", "api_keys", {
                    "api_key_id": kid,
                    "tenant_id": t["tenant_id"],
                    "label": f"key_{kid}",
                    "created_at": iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                    "revoked_at": None,
                })
        for _ in range(1300):
            rid = req_pool.next()
            if api_keys:
                kid, tid = self.rnd.choice(api_keys)
            else:
                kid, tid = None, None
            self.writer.write("audit", "api_requests", {
                "api_request_id": rid,
                "tenant_id": tid,
                "api_key_id": kid,
                "method": self.rnd.choice(["GET", "POST", "PATCH", "DELETE"]),
                "path": self.rnd.choice(["/v1/users", "/v1/invoices", "/v1/subscriptions"]),
                "status_code": self.rnd.choices([200, 201, 400, 401, 404, 500], k=1)[0],
                "latency_ms": self.rnd.randint(5, 2000),
                "occurred_at": iso(self.random_timestamp_between(
                    datetime(2026, 1, 1, tzinfo=timezone.utc), AS_OF
                )),
            })

    def build_feature_flags(self):
        pool = self._pool("ff", 2_800_000)
        for _ in range(400):
            fid = pool.next()
            t = self.rnd.choice(self.data["tenants"])
            u = self.rnd.choice([u for u in self.data["users"] if u["_tenant_id"] == t["tenant_id"]])
            self.writer.write("audit", "feature_flag_evaluations", {
                "eval_id": fid,
                "tenant_id": t["tenant_id"],
                "user_id": u["user_id"],
                "flag_key": self.rnd.choice(["new_dashboard", "fast_checkout", "ai_suggestions"]),
                "variant": self.rnd.choice(["on", "off", "control"]),
                "evaluated_at": iso(self.random_timestamp_between(
                    datetime(2026, 1, 1, tzinfo=timezone.utc), AS_OF
                )),
            })

    def build_background_jobs(self):
        pool = self._pool("bgj", 2_900_000)
        for _ in range(200):
            jid = pool.next()
            t = self.rnd.choice(self.data["tenants"])
            start = self.random_timestamp_between(
                datetime(2026, 1, 1, tzinfo=timezone.utc), AS_OF
            )
            status = self.rnd.choice(["queued", "running", "done", "failed"])
            finished = (start + timedelta(seconds=self.rnd.randint(5, 600))) if status in ("done", "failed") else None
            self.writer.write("audit", "background_jobs", {
                "job_id": jid,
                "tenant_id": t["tenant_id"],
                "kind": self.rnd.choice(["reconcile_invoices", "generate_statements", "refresh_mv"]),
                "status": status,
                "enqueued_at": iso(start),
                "started_at": iso(start + timedelta(seconds=1)) if status != "queued" else None,
                "finished_at": iso(finished) if finished else None,
                "error": "connection reset" if status == "failed" else None,
            })

    # ---- nasty profile injections ----
    # The clean generator already produces diverse data. The nasty profile
    # adds extra rows that exhibit hazards. These extras are appended to the
    # jsonl files during apply_nasty().

    def apply_nasty(self):
        """Add hazard rows to specific tables for the nasty profile."""
        # 1) NULLs: add users with NULL display_name — wait, display_name is NOT NULL.
        #    Add tenant_settings with partially-null settings instead? settings is NOT NULL.
        #    Add webhook deliveries with NULL response_code and empty-string event_kind.
        pool = self._pool("nasty", 9_000_000)

        # Users with trailing whitespace in email (nasty variant of a real user)
        for u in self.data["users"][:3]:
            uid = pool.next()
            self.writer.write("billing", "users", {
                "user_id": uid,
                "email": u["email"] + " ",  # trailing whitespace
                "display_name": u["display_name"],
                "created_at": u["created_at"],
                "deleted_at": None,
            })

        # Soft-deleted subscription that's the newest-updated for its tenant
        if self.data["tenants"]:
            t = self.data["tenants"][0]
            sub_id = pool.next()
            self.writer.write("billing", "subscriptions", {
                "subscription_id": sub_id,
                "tenant_id": t["tenant_id"],
                "plan_id": self.data["plans"][1]["plan_id"],
                "started_at": iso(datetime(2026, 5, 15, tzinfo=timezone.utc)),
                "ended_at": None,
                "status": "active",
                "created_at": iso(datetime(2026, 5, 15, tzinfo=timezone.utc)),
                "deleted_at": iso(datetime(2026, 6, 20, tzinfo=timezone.utc)),
            })

        # Cross-tenant invoice number collision (no unique constraint on invoice_number)
        if len(self.data["tenants"]) >= 3:
            t1, t2, t3 = self.data["tenants"][:3]
            common_num = "INV-SHARED-0001"
            for t in (t1, t2, t3):
                inv_id = pool.next()
                self.writer.write("billing", "invoices", {
                    "invoice_id": inv_id,
                    "tenant_id": t["tenant_id"],
                    "subscription_id": None,
                    "invoice_number": common_num,
                    "issued_at": iso(datetime(2026, 2, 28, tzinfo=timezone.utc)),
                    "period_start": iso_date(date(2026, 2, 1)),
                    "period_end":   iso_date(date(2026, 3, 1)),
                    "currency_code": t["currency_code"],
                    "subtotal_cents": 10000,
                    "tax_cents": 0,
                    "total_cents": 10000,
                    "status": "paid",
                    "IsAutoBilled": False,
                    "due_date": iso_date(date(2026, 3, 15)),
                    "created_at": iso(datetime(2026, 2, 28, tzinfo=timezone.utc)),
                    "deleted_at": None,
                })

        # Month-boundary and DST-boundary events (US/Pacific DST 2026-03-08, 2026-11-01)
        if self.data["tenants"]:
            t = self.data["tenants"][0]
            # 2026-03-08 09:59:59 UTC is 01:59:59 PST which jumps to 03:00 PDT
            for ts in [
                datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 2, 1,  0,  0,  0, tzinfo=timezone.utc),
                datetime(2026, 3, 8,  9, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 3, 8, 10,  0,  0, tzinfo=timezone.utc),
                datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc),
            ]:
                eid = pool.next()
                self.writer.write("billing", "usage_events", {
                    "event_id": eid,
                    "tenant_id": t["tenant_id"],
                    "subscription_id": None,
                    "meter_id": self.data["meters"]["api_calls"],
                    "event_at": iso(ts),
                    "quantity": 1,
                    "payload": None,
                })

        # Ties in commonly-ORDER-BY'd columns: two invoices with identical total_cents
        if len(self.data["tenants"]) >= 2:
            t = self.data["tenants"][0]
            for _ in range(2):
                inv_id = pool.next()
                self.writer.write("billing", "invoices", {
                    "invoice_id": inv_id,
                    "tenant_id": t["tenant_id"],
                    "subscription_id": None,
                    "invoice_number": f"INV-TIE-{inv_id:05d}",
                    "issued_at": iso(datetime(2026, 4, 15, tzinfo=timezone.utc)),
                    "period_start": iso_date(date(2026, 4, 1)),
                    "period_end":   iso_date(date(2026, 5, 1)),
                    "currency_code": t["currency_code"],
                    "subtotal_cents": 42000,
                    "tax_cents": 0,
                    "total_cents": 42000,
                    "status": "paid",
                    "IsAutoBilled": True,
                    "due_date": iso_date(date(2026, 5, 15)),
                    "created_at": iso(datetime(2026, 4, 15, tzinfo=timezone.utc)),
                    "deleted_at": None,
                })

        # Empty-string vs NULL: a billing_contact with empty-string email
        if self.data["tenants"]:
            t = self.data["tenants"][0]
            cid = pool.next()
            self.writer.write("billing", "billing_contacts", {
                "contact_id": cid,
                "tenant_id": t["tenant_id"],
                "name": "Empty Email",
                "email": "",
                "is_primary": False,
                "created_at": iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            })

        # Orphaned FK where SQLite's FK off path would allow it: notification with
        # tenant_id that doesn't exist. FK is NOT declared on notifications.tenant_id
        # in any engine's schema, so this won't violate.
        bogus_tid = 99999
        nid = pool.next()
        self.writer.write("audit", "notifications", {
            "notification_id": nid,
            "tenant_id": bogus_tid,
            "user_id": None,
            "invoice_id": None,
            "channel": "email",
            "status": "sent",
            "created_at": iso(datetime(2026, 5, 1, tzinfo=timezone.utc)),
            "read_at": None,
        })

    # -----------------------------------------------------------------------

    def run(self):
        # Order matters for referential integrity.
        self.build_currencies()
        self.build_exchange_rates()
        self.build_tax_jurisdictions()
        self.build_tax_rates()
        self.build_usage_meters()
        self.build_plans()
        self.build_features_and_plan_features()
        self.build_usage_prices()
        self.build_tenants()
        self.build_roles()
        self.build_users_and_memberships()
        self.emit_memberships()
        self.build_tenant_settings()
        self.build_billing_contacts()
        self.build_addresses()
        self.build_payment_methods()
        self.build_subscriptions_items_invoices()
        self.build_credits()
        self.build_coupons_and_redemptions()
        self.build_promotions()
        self.build_usage_events()
        self.build_audit_log()
        self.build_disputes()
        self.build_emails_notifications()
        self.build_webhooks()
        self.build_api()
        self.build_feature_flags()
        self.build_background_jobs()
        if self.profile == "nasty":
            self.apply_nasty()
        self.writer.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--profile", choices=["clean", "nasty"], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    g = Gen(args.seed, args.profile, out_dir)
    g.run()


if __name__ == "__main__":
    main()
