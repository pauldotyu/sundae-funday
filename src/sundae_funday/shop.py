"""Deterministic in-memory sundae shop state."""

import re
import uuid
from collections import Counter
from copy import deepcopy
from threading import Lock
from typing import Any

SIZES: dict[str, dict[str, Any]] = {
    "MINI": {"name": "Mini Sundae", "included_scoops": 1, "price_cents": 450},
    "CLASSIC": {
        "name": "Classic Sundae",
        "included_scoops": 2,
        "price_cents": 650,
    },
    "DELUXE": {
        "name": "Deluxe Sundae",
        "included_scoops": 3,
        "price_cents": 825,
    },
}

FLAVORS: dict[str, dict[str, Any]] = {
    "VANILLA": {"name": "Vanilla Bean", "stock": 18},
    "CHOCOLATE": {"name": "Chocolate", "stock": 14},
    "STRAWBERRY": {"name": "Strawberry", "stock": 12},
    "MINT_CHIP": {"name": "Mint Chip", "stock": 6},
    "COFFEE": {"name": "Coffee", "stock": 8},
}

SAUCES: dict[str, dict[str, Any]] = {
    "HOT_FUDGE": {"name": "Hot Fudge", "price_cents": 0, "stock": 16},
    "CARAMEL": {"name": "Caramel", "price_cents": 0, "stock": 15},
    "STRAWBERRY_DRIZZLE": {
        "name": "Strawberry Drizzle",
        "price_cents": 0,
        "stock": 9,
    },
}

TOPPINGS: dict[str, dict[str, Any]] = {
    "CHERRY": {"name": "Cherry", "price_cents": 50, "stock": 10},
    "SPRINKLES": {"name": "Rainbow Sprinkles", "price_cents": 50, "stock": 20},
    "OREO": {"name": "Oreo Crumble", "price_cents": 75, "stock": 11},
    "PEANUTS": {"name": "Toasted Peanuts", "price_cents": 50, "stock": 7},
    "BANANA": {"name": "Banana", "price_cents": 100, "stock": 5},
    "WHIPPED_CREAM": {"name": "Whipped Cream", "price_cents": 75, "stock": 13},
}

ALIASES = {
    "MINT": "MINT_CHIP",
    "MINT_CHOCOLATE_CHIP": "MINT_CHIP",
    "FUDGE": "HOT_FUDGE",
    "HOTFUDGE": "HOT_FUDGE",
    "STRAWBERRY_SAUCE": "STRAWBERRY_DRIZZLE",
    "OREO_CRUMBLE": "OREO",
    "WHIP": "WHIPPED_CREAM",
}


class InMemorySundaeShop:
    def __init__(self) -> None:
        self._lock = Lock()
        self._inventory = {
            "flavors": {sku: item["stock"] for sku, item in FLAVORS.items()},
            "sauces": {sku: item["stock"] for sku, item in SAUCES.items()},
            "toppings": {sku: item["stock"] for sku, item in TOPPINGS.items()},
        }
        self._drafts: dict[str, dict[str, Any]] = {}
        self._submitted_drafts: set[str] = set()
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}

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
                    **data,
                    "price_display": cents_to_display(data["price_cents"]),
                }
                for sku, data in SIZES.items()
            ],
            "flavors": [
                {
                    "sku": sku,
                    "name": data["name"],
                    "available": self._inventory["flavors"][sku] > 0,
                }
                for sku, data in FLAVORS.items()
            ],
            "sauces": [
                {
                    "sku": sku,
                    "name": data["name"],
                    "price_cents": data["price_cents"],
                    "price_display": cents_to_display(data["price_cents"]),
                    "available": self._inventory["sauces"][sku] > 0,
                }
                for sku, data in SAUCES.items()
            ],
            "toppings": [
                {
                    "sku": sku,
                    "name": data["name"],
                    "price_cents": data["price_cents"],
                    "price_display": cents_to_display(data["price_cents"]),
                    "available": self._inventory["toppings"][sku] > 0,
                }
                for sku, data in TOPPINGS.items()
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
            "flavors": flavors or [],
            "sauce": [sauce] if sauce else [],
            "toppings": toppings or [],
        }
        if not any(requested.values()):
            return self._inventory_snapshot()

        flavor_resolution = self._resolve_many(requested["flavors"], FLAVORS)
        sauce_resolution = self._resolve_many(requested["sauce"], SAUCES)
        topping_resolution = self._resolve_many(requested["toppings"], TOPPINGS)
        lines = (
            self._availability_lines(flavor_resolution, "flavors")
            + self._availability_lines(sauce_resolution, "sauces")
            + self._availability_lines(topping_resolution, "toppings")
        )
        return {
            "can_make_now": all(line["available"] for line in lines),
            "requested_items": lines,
            "unknown_items": [
                *flavor_resolution["unknown"],
                *sauce_resolution["unknown"],
                *topping_resolution["unknown"],
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

        resolved_size = self._resolve_one(size, SIZES)
        if resolved_size is None:
            return {
                "status": "needs_clarification",
                "message": "Pick mini, classic, or deluxe for the sundae size.",
                "options": [data["name"] for data in SIZES.values()],
            }
        size_sku, size_data = resolved_size

        if not selected_flavors:
            return {
                "status": "needs_clarification",
                "message": "Pick at least one ice cream flavor before I can price it.",
            }

        scoop_count = size_data["included_scoops"]
        if len(selected_flavors) > scoop_count:
            return {
                "status": "needs_clarification",
                "message": (
                    f"{size_data['name']} includes {scoop_count} scoop"
                    f"{'s' if scoop_count != 1 else ''}."
                ),
            }
        if len(selected_flavors) < scoop_count:
            remaining = scoop_count - len(selected_flavors)
            return {
                "status": "needs_clarification",
                "message": (
                    f"Add {remaining} more flavor choice"
                    f"{'s' if remaining != 1 else ''} for {size_data['name']}."
                ),
            }

        flavor_resolution = self._resolve_many(selected_flavors, FLAVORS)
        sauce_resolution = self._resolve_many([sauce] if sauce else [], SAUCES)
        topping_resolution = self._resolve_many(selected_toppings, TOPPINGS)
        unknown = [
            *flavor_resolution["unknown"],
            *sauce_resolution["unknown"],
            *topping_resolution["unknown"],
        ]
        if unknown:
            return {
                "status": "needs_clarification",
                "message": "I could not match every requested item to the menu.",
                "unknown_items": unknown,
            }

        flavor_counts = Counter(flavor_resolution["skus"])
        if not self._items_available(flavor_counts, "flavors"):
            return self._unavailable_response(flavor_counts, "flavors")
        if sauce_resolution["skus"] and not self._items_available(
            Counter(sauce_resolution["skus"]), "sauces"
        ):
            return self._unavailable_response(
                Counter(sauce_resolution["skus"]),
                "sauces",
            )
        if topping_resolution["skus"] and not self._items_available(
            Counter(topping_resolution["skus"]), "toppings"
        ):
            return self._unavailable_response(
                Counter(topping_resolution["skus"]), "toppings"
            )

        total_cents = size_data["price_cents"] + sum(
            TOPPINGS[sku]["price_cents"] for sku in topping_resolution["skus"]
        )
        eta_minutes = 4 + scoop_count * 2 + len(topping_resolution["skus"])
        draft_id = f"draft-{uuid.uuid4().hex[:10]}"
        draft = {
            "draft_id": draft_id,
            "session_id": session_id,
            "size": {"sku": size_sku, **size_data},
            "flavors": [
                self._named_option(FLAVORS, sku) for sku in flavor_resolution["skus"]
            ],
            "sauce": (
                self._named_option(SAUCES, sauce_resolution["skus"][0])
                if sauce_resolution["skus"]
                else None
            ),
            "toppings": [
                self._named_option(TOPPINGS, sku) for sku in topping_resolution["skus"]
            ],
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
                "size": {"sku": size_sku, "name": size_data["name"]},
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

            flavor_counts = Counter(item["sku"] for item in draft["flavors"])
            sauce_counts = Counter(
                [draft["sauce"]["sku"]] if isinstance(draft["sauce"], dict) else []
            )
            topping_counts = Counter(item["sku"] for item in draft["toppings"])
            if not self._items_available(flavor_counts, "flavors"):
                raise RuntimeError("Flavor inventory changed before confirmation")
            if sauce_counts and not self._items_available(sauce_counts, "sauces"):
                raise RuntimeError("Sauce inventory changed before confirmation")
            if topping_counts and not self._items_available(topping_counts, "toppings"):
                raise RuntimeError("Topping inventory changed before confirmation")

            self._apply_counts(flavor_counts, "flavors")
            self._apply_counts(sauce_counts, "sauces")
            self._apply_counts(topping_counts, "toppings")
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
            self._orders[order_id] = deepcopy(response)
            self._idempotency[idempotency_key] = deepcopy(response)
            return response

    def _inventory_snapshot(self) -> dict[str, Any]:
        return {
            "can_make_now": True,
            "flavors": self._stock_lines(FLAVORS, "flavors"),
            "sauces": self._stock_lines(SAUCES, "sauces"),
            "toppings": self._stock_lines(TOPPINGS, "toppings"),
            "low_stock": self._low_stock_items(),
        }

    def _low_stock_items(self) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        for category, catalog in (
            ("flavors", FLAVORS),
            ("sauces", SAUCES),
            ("toppings", TOPPINGS),
        ):
            for sku, item in catalog.items():
                remaining = self._inventory[category][sku]
                if remaining <= 3:
                    lines.append(
                        {
                            "category": category,
                            "sku": sku,
                            "name": item["name"],
                            "remaining": remaining,
                        }
                    )
        return lines

    def _stock_lines(
        self,
        catalog: dict[str, dict[str, Any]],
        category: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "sku": sku,
                "name": item["name"],
                "remaining": self._inventory[category][sku],
                "available": self._inventory[category][sku] > 0,
            }
            for sku, item in catalog.items()
        ]

    def _availability_lines(
        self,
        resolution: dict[str, Any],
        category: str,
    ) -> list[dict[str, Any]]:
        counts = Counter(resolution["skus"])
        catalog = {
            "flavors": FLAVORS,
            "sauces": SAUCES,
            "toppings": TOPPINGS,
        }[category]
        return [
            {
                "category": category,
                "sku": sku,
                "name": catalog[sku]["name"],
                "requested": requested,
                "remaining": self._inventory[category][sku],
                "available": self._inventory[category][sku] >= requested,
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
        catalog: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        resolved: list[str] = []
        unknown: list[str] = []
        for value in values:
            match = self._resolve_one(value, catalog)
            if match is None:
                unknown.append(value)
                continue
            resolved.append(match[0])
        return {"skus": resolved, "unknown": unknown}

    def _resolve_one(
        self,
        value: str,
        catalog: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        normalized = normalize_token(value)
        normalized = ALIASES.get(normalized, normalized)
        if normalized in catalog:
            return normalized, catalog[normalized]
        for sku, item in catalog.items():
            if normalize_token(item["name"]) == normalized:
                return sku, item
        return None

    def _named_option(
        self,
        catalog: dict[str, dict[str, Any]],
        sku: str,
    ) -> dict[str, Any]:
        item = catalog[sku]
        return {"sku": sku, **item}

    def _items_available(self, counts: Counter[str], category: str) -> bool:
        return all(
            self._inventory[category][sku] >= count for sku, count in counts.items()
        )

    def _apply_counts(self, counts: Counter[str], category: str) -> None:
        for sku, count in counts.items():
            self._inventory[category][sku] -= count

    def _unavailable_response(
        self,
        counts: Counter[str],
        category: str,
    ) -> dict[str, Any]:
        catalog = {"flavors": FLAVORS, "sauces": SAUCES, "toppings": TOPPINGS}[category]
        unavailable = [
            {
                "sku": sku,
                "name": catalog[sku]["name"],
                "requested": requested,
                "remaining": self._inventory[category][sku],
            }
            for sku, requested in counts.items()
            if self._inventory[category][sku] < requested
        ]
        return {
            "status": "unavailable",
            "message": "Some requested items are not available right now.",
            "unavailable_items": unavailable,
            "low_stock": self._low_stock_items(),
        }


def cents_to_display(cents: int) -> str:
    return f"${cents / 100:.2f}"


def normalize_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.strip().upper()).strip("_")
