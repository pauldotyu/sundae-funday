"""In-memory deterministic sundae shop state."""

import uuid
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any

from sundae_funday.catalog import (
    ALIASES,
    CATALOGS,
    FLAVORS,
    SAUCES,
    SIZES,
    TOPPINGS,
    Ingredient,
    Size,
    normalize_token,
)
from sundae_funday.types import CatalogCategory


@dataclass(frozen=True, slots=True)
class Resolution:
    skus: tuple[str, ...]
    unknown: tuple[str, ...]


class InMemorySundaeShop:
    def __init__(self) -> None:
        self._lock = Lock()
        self._inventory = {
            category: {sku: item.stock for sku, item in catalog.items()}
            for category, catalog in CATALOGS.items()
        }
        self._drafts: dict[str, dict[str, Any]] = {}
        self._submitted_drafts: set[str] = set()
        self._idempotency: dict[str, dict[str, Any]] = {}

    def list_menu(self) -> dict[str, Any]:
        return {
            "shop_name": "Sundae Funday",
            "currency": "USD",
            "confirm_flow": (
                "Quotes create a draft. Only the concierge confirm action submits it."
            ),
            "sizes": [
                {
                    "sku": sku,
                    **size.as_dict(),
                    "price_display": cents_to_display(size.price_cents),
                }
                for sku, size in SIZES.items()
            ],
            "flavors": [
                {
                    "sku": sku,
                    "name": item.name,
                    "available": self._remaining(CatalogCategory.FLAVORS, sku) > 0,
                }
                for sku, item in FLAVORS.items()
            ],
            "sauces": [
                self._priced_menu_item(CatalogCategory.SAUCES, sku, item)
                for sku, item in SAUCES.items()
            ],
            "toppings": [
                self._priced_menu_item(CatalogCategory.TOPPINGS, sku, item)
                for sku, item in TOPPINGS.items()
            ],
        }

    def check_availability(
        self,
        *,
        flavors: list[str] | None = None,
        sauce: str | None = None,
        toppings: list[str] | None = None,
    ) -> dict[str, Any]:
        requested = {
            CatalogCategory.FLAVORS: flavors or [],
            CatalogCategory.SAUCES: [sauce] if sauce else [],
            CatalogCategory.TOPPINGS: toppings or [],
        }
        if not any(requested.values()):
            return self._inventory_snapshot()

        resolutions = {
            category: self._resolve_many(values, CATALOGS[category])
            for category, values in requested.items()
        }
        lines = [
            line
            for category, resolution in resolutions.items()
            for line in self._availability_lines(resolution, category)
        ]
        return {
            "can_make_now": all(line["available"] for line in lines),
            "requested_items": lines,
            "unknown_items": [
                value
                for resolution in resolutions.values()
                for value in resolution.unknown
            ],
            "low_stock": self._low_stock_items(),
        }

    def quote_order(
        self,
        *,
        session_id: str,
        size: str = "CLASSIC",
        flavors: list[str] | None = None,
        sauce: str | None = None,
        toppings: list[str] | None = None,
        requested_ready_in_minutes: int | None = None,
    ) -> dict[str, Any]:
        selected_flavors = flavors or []
        selected_toppings = toppings or []

        resolved_size = self._resolve_size(size)
        if resolved_size is None:
            return {
                "status": "needs_clarification",
                "message": "Pick mini, classic, or deluxe for the sundae size.",
                "options": [item.name for item in SIZES.values()],
            }
        size_sku, size_data = resolved_size

        if not selected_flavors:
            return {
                "status": "needs_clarification",
                "message": "Pick at least one ice cream flavor before I can price it.",
            }

        scoop_count = size_data.included_scoops
        if len(selected_flavors) > scoop_count:
            return {
                "status": "needs_clarification",
                "message": (
                    f"{size_data.name} includes {scoop_count} scoop"
                    f"{'s' if scoop_count != 1 else ''}."
                ),
            }
        if len(selected_flavors) < scoop_count:
            remaining = scoop_count - len(selected_flavors)
            return {
                "status": "needs_clarification",
                "message": (
                    f"Add {remaining} more flavor choice"
                    f"{'s' if remaining != 1 else ''} for {size_data.name}."
                ),
            }

        requested = {
            CatalogCategory.FLAVORS: selected_flavors,
            CatalogCategory.SAUCES: [sauce] if sauce else [],
            CatalogCategory.TOPPINGS: selected_toppings,
        }
        resolutions = {
            category: self._resolve_many(values, CATALOGS[category])
            for category, values in requested.items()
        }
        unknown = [
            value for resolution in resolutions.values() for value in resolution.unknown
        ]
        if unknown:
            return {
                "status": "needs_clarification",
                "message": "I could not match every requested item to the menu.",
                "unknown_items": unknown,
            }

        counts = {
            category: Counter(resolution.skus)
            for category, resolution in resolutions.items()
        }
        for category, category_counts in counts.items():
            if category_counts and not self._items_available(
                category_counts,
                category,
            ):
                return self._unavailable_response(category_counts, category)

        topping_skus = resolutions[CatalogCategory.TOPPINGS].skus
        total_cents = size_data.price_cents + sum(
            TOPPINGS[sku].price_cents or 0 for sku in topping_skus
        )
        eta_minutes = 4 + scoop_count * 2 + len(topping_skus)
        draft_id = f"draft-{uuid.uuid4().hex[:10]}"
        sauce_skus = resolutions[CatalogCategory.SAUCES].skus
        draft = {
            "draft_id": draft_id,
            "session_id": session_id,
            "size": {"sku": size_sku, **size_data.as_dict()},
            "flavors": [
                self._named_option(FLAVORS, sku)
                for sku in resolutions[CatalogCategory.FLAVORS].skus
            ],
            "sauce": (
                self._named_option(SAUCES, sauce_skus[0]) if sauce_skus else None
            ),
            "toppings": [self._named_option(TOPPINGS, sku) for sku in topping_skus],
            "total_cents": total_cents,
            "eta_minutes": eta_minutes,
            "requested_ready_in_minutes": requested_ready_in_minutes,
        }
        with self._lock:
            self._drafts[draft_id] = deepcopy(draft)
        can_meet_requested_time = (
            True
            if requested_ready_in_minutes is None
            else eta_minutes <= requested_ready_in_minutes
        )
        return {
            "status": "ready",
            "draft_created": True,
            "draft_id": draft_id,
            "order": {
                "size": {"sku": size_sku, "name": size_data.name},
                "flavors": draft["flavors"],
                "sauce": draft["sauce"],
                "toppings": draft["toppings"],
            },
            "quote": {
                "line_items": self._line_items(draft),
                "total_cents": total_cents,
                "total_display": cents_to_display(total_cents),
                "eta_minutes": eta_minutes,
                "requested_ready_in_minutes": requested_ready_in_minutes,
                "can_meet_requested_time": can_meet_requested_time,
            },
        }

    def submit_order(
        self,
        *,
        draft_id: str,
        session_id: str,
        idempotency_key: str,
        customer_name: str = "Guest",
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                replay = deepcopy(existing)
                replay["idempotent_replay"] = True
                return replay

            draft = self._drafts.get(draft_id)
            if draft is None:
                raise RuntimeError("Draft not found")
            if draft["session_id"] != session_id:
                raise RuntimeError("Draft does not belong to this session")
            if draft_id in self._submitted_drafts:
                raise RuntimeError("Draft already submitted")

            counts = {
                CatalogCategory.FLAVORS: Counter(
                    item["sku"] for item in draft["flavors"]
                ),
                CatalogCategory.SAUCES: Counter(
                    [draft["sauce"]["sku"]] if isinstance(draft["sauce"], dict) else []
                ),
                CatalogCategory.TOPPINGS: Counter(
                    item["sku"] for item in draft["toppings"]
                ),
            }
            inventory_errors = {
                CatalogCategory.FLAVORS: "Flavor inventory changed before confirmation",
                CatalogCategory.SAUCES: "Sauce inventory changed before confirmation",
                CatalogCategory.TOPPINGS: (
                    "Topping inventory changed before confirmation"
                ),
            }
            for category, category_counts in counts.items():
                if category_counts and not self._items_available(
                    category_counts,
                    category,
                ):
                    raise RuntimeError(inventory_errors[category])

            for category, category_counts in counts.items():
                self._apply_counts(category_counts, category)
            order_id = f"sundae-{uuid.uuid4().hex[:8]}"
            response = {
                "order_id": order_id,
                "customer_name": customer_name,
                "status": "submitted",
                "idempotent_replay": False,
                "pickup_eta_minutes": draft["eta_minutes"],
                "total_cents": draft["total_cents"],
                "total_display": cents_to_display(draft["total_cents"]),
                "order": {
                    "size": draft["size"],
                    "flavors": draft["flavors"],
                    "sauce": draft["sauce"],
                    "toppings": draft["toppings"],
                },
            }
            self._submitted_drafts.add(draft_id)
            self._idempotency[idempotency_key] = deepcopy(response)
            return response

    def _priced_menu_item(
        self,
        category: CatalogCategory,
        sku: str,
        item: Ingredient,
    ) -> dict[str, Any]:
        price_cents = item.price_cents or 0
        return {
            "sku": sku,
            "name": item.name,
            "price_cents": price_cents,
            "price_display": cents_to_display(price_cents),
            "available": self._remaining(category, sku) > 0,
        }

    def _inventory_snapshot(self) -> dict[str, Any]:
        return {
            "can_make_now": True,
            **{
                category.value: self._stock_lines(category)
                for category in CatalogCategory
            },
            "low_stock": self._low_stock_items(),
        }

    def _low_stock_items(self) -> list[dict[str, Any]]:
        return [
            {
                "category": category.value,
                "sku": sku,
                "name": item.name,
                "remaining": self._remaining(category, sku),
            }
            for category, catalog in CATALOGS.items()
            for sku, item in catalog.items()
            if self._remaining(category, sku) <= 3
        ]

    def _stock_lines(self, category: CatalogCategory) -> list[dict[str, Any]]:
        return [
            {
                "sku": sku,
                "name": item.name,
                "remaining": self._remaining(category, sku),
                "available": self._remaining(category, sku) > 0,
            }
            for sku, item in CATALOGS[category].items()
        ]

    def _availability_lines(
        self,
        resolution: Resolution,
        category: CatalogCategory,
    ) -> list[dict[str, Any]]:
        counts = Counter(resolution.skus)
        catalog = CATALOGS[category]
        return [
            {
                "category": category.value,
                "sku": sku,
                "name": catalog[sku].name,
                "requested": requested,
                "remaining": self._remaining(category, sku),
                "available": self._remaining(category, sku) >= requested,
            }
            for sku, requested in counts.items()
        ]

    def _line_items(self, draft: dict[str, Any]) -> list[dict[str, Any]]:
        line_items = [
            {
                "name": draft["size"]["name"],
                "price_cents": draft["size"]["price_cents"],
                "price_display": cents_to_display(draft["size"]["price_cents"]),
            }
        ]
        for item in draft["toppings"]:
            line_items.append(
                {
                    "name": item["name"],
                    "price_cents": item["price_cents"],
                    "price_display": cents_to_display(item["price_cents"]),
                }
            )
        return line_items

    def _resolve_many(
        self,
        values: list[str],
        catalog: Mapping[str, Ingredient],
    ) -> Resolution:
        resolved: list[str] = []
        unknown: list[str] = []
        for value in values:
            match = self._resolve_ingredient(value, catalog)
            if match is None:
                unknown.append(value)
            else:
                resolved.append(match[0])
        return Resolution(tuple(resolved), tuple(unknown))

    def _resolve_size(self, value: str) -> tuple[str, Size] | None:
        normalized = normalize_token(value)
        if normalized in SIZES:
            return normalized, SIZES[normalized]
        for sku, item in SIZES.items():
            if normalize_token(item.name) == normalized:
                return sku, item
        return None

    def _resolve_ingredient(
        self,
        value: str,
        catalog: Mapping[str, Ingredient],
    ) -> tuple[str, Ingredient] | None:
        normalized = normalize_token(value)
        normalized = ALIASES.get(normalized, normalized)
        if normalized in catalog:
            return normalized, catalog[normalized]
        for sku, item in catalog.items():
            if normalize_token(item.name) == normalized:
                return sku, item
        return None

    def _named_option(
        self,
        catalog: Mapping[str, Ingredient],
        sku: str,
    ) -> dict[str, Any]:
        return {"sku": sku, **catalog[sku].as_dict()}

    def _remaining(self, category: CatalogCategory, sku: str) -> int:
        return self._inventory[category][sku]

    def _items_available(
        self,
        counts: Counter[str],
        category: CatalogCategory,
    ) -> bool:
        return all(
            self._remaining(category, sku) >= count for sku, count in counts.items()
        )

    def _apply_counts(
        self,
        counts: Counter[str],
        category: CatalogCategory,
    ) -> None:
        for sku, count in counts.items():
            self._inventory[category][sku] -= count

    def _unavailable_response(
        self,
        counts: Counter[str],
        category: CatalogCategory,
    ) -> dict[str, Any]:
        catalog = CATALOGS[category]
        unavailable = [
            {
                "sku": sku,
                "name": catalog[sku].name,
                "requested": requested,
                "remaining": self._remaining(category, sku),
            }
            for sku, requested in counts.items()
            if self._remaining(category, sku) < requested
        ]
        return {
            "status": "unavailable",
            "message": "Some requested items are not available right now.",
            "unavailable_items": unavailable,
            "low_stock": self._low_stock_items(),
        }


def cents_to_display(cents: int) -> str:
    return f"${cents / 100:.2f}"
