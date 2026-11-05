"""Subscription price migration against an isolated SQLite database."""

from pathlib import Path
import re
import sqlite3

import pytest


WEB = Path(__file__).resolve().parents[1] / "web"
MIGRATIONS = WEB / "prisma" / "migrations"
PRICE_MIGRATION = MIGRATIONS / "20261105120000_subscription_price_increase"


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    for migration in sorted(MIGRATIONS.glob("*/migration.sql")):
        if migration.parent.name >= PRICE_MIGRATION.name:
            break
        connection.executescript(migration.read_text(encoding="utf-8"))
    yield connection
    connection.close()


def increase_prices(database):
    database.executescript((PRICE_MIGRATION / "migration.sql").read_text(encoding="utf-8"))


@pytest.mark.parametrize("status", ["NEW", "CONFIRMED"])
def test_subscription_prices_match_seed_and_preserve_existing_orders(database, status):
    plan_id = database.execute("SELECT id FROM plans WHERE code = 'test'").fetchone()[0]
    database.execute(
        "INSERT INTO accounts (name, plan_id, paid_until) VALUES ('synthetic', ?, 2000000000000)",
        (plan_id,),
    )
    database.execute(
        """INSERT INTO payments
        (order_id, account_id, plan_code, months, amount_kopecks, status, updated_at)
        VALUES ('synthetic-order', 1, 'test', 12, 550000, ?, 1700000000000)""", (status,)
    )
    account_before = database.execute("SELECT * FROM accounts").fetchall()
    payments_before = database.execute("SELECT * FROM payments").fetchall()
    limits_before = database.execute(
        "SELECT code, pages_per_month, max_users FROM plans ORDER BY code"
    ).fetchall()

    increase_prices(database)

    expected = {
        "test": (75_000, 825_000),
        "business": (675_000, 7_800_000),
        "corporate": (1_200_000, 13_800_000),
    }
    seed = (WEB / "prisma" / "seed.ts").read_text(encoding="utf-8")
    for code, prices in expected.items():
        assert database.execute(
            "SELECT price_kopecks, price_year_kopecks FROM plans WHERE code = ?", (code,)
        ).fetchone() == prices
        seed_prices = re.search(
            rf'code: "{code}"[^\n]+priceKopecks: ([\d_]+), priceYearKopecks: ([\d_]+)', seed
        )
        assert seed_prices is not None
        assert tuple(int(value.replace("_", "")) for value in seed_prices.groups()) == prices

    assert database.execute("SELECT * FROM accounts").fetchall() == account_before
    assert database.execute("SELECT * FROM payments").fetchall() == payments_before
    assert database.execute(
        "SELECT code, pages_per_month, max_users FROM plans ORDER BY code"
    ).fetchall() == limits_before


def test_free_plans_null_annual_prices_and_integer_rounding(database):
    for code, monthly, annual in [
        ("trial", 0, None), ("unlimited", 0, 0), ("custom", 101, None),
        ("custom-year", 103, 105), ("free", 0, None),
    ]:
        database.execute(
            """INSERT INTO plans (code, title, price_kopecks, price_year_kopecks)
            VALUES (?, ?, ?, ?)""", (code, code, monthly, annual),
        )

    increase_prices(database)

    for code, expected in {
        "trial": (0, None), "unlimited": (0, 0), "custom": (152, None),
        "custom-year": (155, 158), "free": (0, None),
    }.items():
        assert database.execute(
            "SELECT price_kopecks, price_year_kopecks FROM plans WHERE code = ?", (code,)
        ).fetchone() == expected
    assert database.execute(
        "SELECT COUNT(*) FROM plans WHERE typeof(price_kopecks) != 'integer' "
        "OR (price_year_kopecks IS NOT NULL AND typeof(price_year_kopecks) != 'integer')"
    ).fetchone()[0] == 0
