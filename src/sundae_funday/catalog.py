"""Immutable authoritative sundae catalog."""

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from sundae_funday.types import CatalogCategory


@dataclass(frozen=True, slots=True)
class Size:
    name: str
    included_scoops: int
    price_cents: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Ingredient:
    name: str
    stock: int
    price_cents: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name}
        if self.price_cents is not None:
            result["price_cents"] = self.price_cents
        result["stock"] = self.stock
        return result


SIZES: Mapping[str, Size] = MappingProxyType(
    {
        "MINI": Size("Mini Sundae", included_scoops=1, price_cents=450),
        "CLASSIC": Size("Classic Sundae", included_scoops=2, price_cents=650),
        "DELUXE": Size("Deluxe Sundae", included_scoops=3, price_cents=825),
    }
)

FLAVORS: Mapping[str, Ingredient] = MappingProxyType(
    {
        "VANILLA": Ingredient("Vanilla Bean", stock=18),
        "CHOCOLATE": Ingredient("Chocolate", stock=14),
        "STRAWBERRY": Ingredient("Strawberry", stock=12),
        "MINT_CHIP": Ingredient("Mint Chip", stock=6),
        "COFFEE": Ingredient("Coffee", stock=8),
    }
)

SAUCES: Mapping[str, Ingredient] = MappingProxyType(
    {
        "HOT_FUDGE": Ingredient("Hot Fudge", stock=16, price_cents=0),
        "CARAMEL": Ingredient("Caramel", stock=15, price_cents=0),
        "STRAWBERRY_DRIZZLE": Ingredient(
            "Strawberry Drizzle",
            stock=9,
            price_cents=0,
        ),
    }
)

TOPPINGS: Mapping[str, Ingredient] = MappingProxyType(
    {
        "CHERRY": Ingredient("Cherry", stock=10, price_cents=50),
        "SPRINKLES": Ingredient("Rainbow Sprinkles", stock=20, price_cents=50),
        "OREO": Ingredient("Oreo Crumble", stock=11, price_cents=75),
        "GRAHAM_CRACKERS": Ingredient("Graham Crackers", stock=9, price_cents=75),
        "PEANUTS": Ingredient("Toasted Peanuts", stock=7, price_cents=50),
        "BANANA": Ingredient("Banana", stock=5, price_cents=100),
        "WHIPPED_CREAM": Ingredient("Whipped Cream", stock=13, price_cents=75),
    }
)

CATALOGS: Mapping[CatalogCategory, Mapping[str, Ingredient]] = MappingProxyType(
    {
        CatalogCategory.FLAVORS: FLAVORS,
        CatalogCategory.SAUCES: SAUCES,
        CatalogCategory.TOPPINGS: TOPPINGS,
    }
)

ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "MINT": "MINT_CHIP",
        "MINT_CHOCOLATE_CHIP": "MINT_CHIP",
        "FUDGE": "HOT_FUDGE",
        "HOTFUDGE": "HOT_FUDGE",
        "STRAWBERRY_SAUCE": "STRAWBERRY_DRIZZLE",
        "OREO_CRUMBLE": "OREO",
        "WHIP": "WHIPPED_CREAM",
    }
)

SURPRISE_SIZE_SKUS = tuple(
    sku for sku, size in SIZES.items() if size.included_scoops <= 2
)
SURPRISE_FLAVOR_SKUS = tuple(FLAVORS)
SURPRISE_SAUCE_SKUS = tuple(SAUCES)
SURPRISE_TOPPING_SKUS = tuple(TOPPINGS)


def normalize_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.strip().upper()).strip("_")


def routing_hints(category: CatalogCategory) -> dict[str, str]:
    catalog = CATALOGS[category]
    hints: dict[str, str] = {}
    for sku, item in catalog.items():
        terms = [item.name, sku.replace("_", " ")]
        if item.name.endswith(" Sundae"):
            terms.append(item.name.removesuffix(" Sundae"))
        for term in terms:
            hints.setdefault(term.lower(), sku)
    for alias, sku in ALIASES.items():
        if sku in catalog:
            hints.setdefault(alias.replace("_", " ").lower(), sku)
    return hints


def size_routing_hints() -> dict[str, str]:
    hints: dict[str, str] = {}
    for sku, size in SIZES.items():
        hints[size.name.removesuffix(" Sundae").lower()] = sku
    return hints
