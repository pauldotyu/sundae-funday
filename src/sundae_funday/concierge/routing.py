"""Concierge routing and order-plan logic."""

from typing import Any

from sundae_funday.catalog import (
    routing_hints,
    size_routing_hints,
)
from sundae_funday.concierge.api import RoutingPlan
from sundae_funday.types import CatalogCategory

SIZE_HINTS = size_routing_hints()
FLAVOR_HINTS = routing_hints(CatalogCategory.FLAVORS)
SAUCE_HINTS = routing_hints(CatalogCategory.SAUCES)
TOPPING_HINTS = routing_hints(CatalogCategory.TOPPINGS)

OPS_KEYWORDS = {
    "available",
    "availability",
    "busy",
    "eta",
    "fast",
    "faster",
    "inventory",
    "low stock",
    "out of",
    "quick",
    "ready",
    "running low",
    "stock",
    "tonight",
    "wait",
}
MENU_KEYWORDS = {
    "menu",
    "flavors",
    "toppings",
    "sizes",
    "prices",
    "options",
    "cost",
    "how much",
}
SURPRISE_PHRASES = ("surprise me", "pick whatever", "choose for me")
SPECIAL_PHRASES = (
    "any specials",
    "daily special",
    "on special",
    "special today",
    "specials",
)
ORDER_KEYWORDS = {
    "build",
    "make me",
    "order",
    "quote",
    "sundae",
    "scoop",
    "i want",
    "i would like",
    "can i get",
}


def heuristic_plan(message: str) -> RoutingPlan:
    lower = message.lower().strip()
    if is_special_request(lower):
        return RoutingPlan(route="operations", operations_question=message)
    if any(phrase in lower for phrase in SURPRISE_PHRASES):
        return RoutingPlan(route="surprise")
    size = next((value for key, value in SIZE_HINTS.items() if key in lower), None)
    flavors = _extract_terms(lower, FLAVOR_HINTS)
    sauce = _extract_first(lower, SAUCE_HINTS)
    toppings = _extract_terms(lower, TOPPING_HINTS)
    requested_ready_in_minutes = _extract_ready_time(lower)
    if any(keyword in lower for keyword in OPS_KEYWORDS):
        if size is not None or flavors or sauce is not None or toppings:
            return RoutingPlan(
                route="quote",
                size=size,
                flavors=flavors,
                sauce=sauce,
                toppings=toppings,
                requested_ready_in_minutes=requested_ready_in_minutes,
            )
        return RoutingPlan(
            route="operations",
            operations_question=message,
            requested_ready_in_minutes=requested_ready_in_minutes,
        )
    if any(keyword in lower for keyword in MENU_KEYWORDS):
        return RoutingPlan(route="menu")
    if (
        any(keyword in lower for keyword in ORDER_KEYWORDS)
        or size is not None
        or flavors
        or sauce is not None
        or toppings
    ):
        return RoutingPlan(
            route="quote",
            size=size,
            flavors=flavors,
            sauce=sauce,
            toppings=toppings,
            requested_ready_in_minutes=requested_ready_in_minutes,
        )
    return RoutingPlan(route="general")


def _extract_terms(message: str, mapping: dict[str, str]) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    for raw, normalized in mapping.items():
        start = message.find(raw)
        if start >= 0:
            candidates.append((start, -len(raw), normalized))
    matched: list[str] = []
    seen: set[str] = set()
    for _, _, normalized in sorted(candidates):
        if normalized not in seen:
            matched.append(normalized)
            seen.add(normalized)
    return matched


def _extract_first(message: str, mapping: dict[str, str]) -> str | None:
    terms = _extract_terms(message, mapping)
    return terms[0] if terms else None


def _extract_ready_time(message: str) -> int | None:
    index = 0
    while index < len(message):
        if not message[index].isdecimal():
            index += 1
            continue
        start = index
        while index < len(message) and message[index].isdecimal():
            index += 1
        end = index
        while index < len(message) and message[index].isspace():
            index += 1
        if message.startswith(("minute", "min"), index):
            return int(message[start:end])
    return None


def is_special_request(message: str) -> bool:
    lower = message.lower()
    return any(phrase in lower for phrase in SPECIAL_PHRASES)


def unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def top_inventory_items(
    inventory: dict[str, Any],
    category: CatalogCategory,
    *,
    count: int,
) -> list[dict[str, Any]]:
    items = inventory.get(category.value)
    if not isinstance(items, list):
        raise RuntimeError(f"Ops inventory response is missing {category.value}")
    available = sorted(
        (
            item
            for item in items
            if isinstance(item, dict)
            and item.get("available") is True
            and isinstance(item.get("remaining"), int)
        ),
        key=lambda item: int(item["remaining"]),
        reverse=True,
    )
    if len(available) < count:
        if count == 1:
            raise RuntimeError(
                f"Ops inventory response has no available {category.value}"
            )
        raise RuntimeError(
            f"Ops inventory response needs {count} available {category.value}"
        )
    return available[:count]


def special_order_plan(result: dict[str, Any]) -> RoutingPlan:
    inventory = unwrap_tool_result(result)
    flavor = top_inventory_items(
        inventory,
        CatalogCategory.FLAVORS,
        count=1,
    )[0]
    sauce = top_inventory_items(
        inventory,
        CatalogCategory.SAUCES,
        count=1,
    )[0]
    toppings = top_inventory_items(
        inventory,
        CatalogCategory.TOPPINGS,
        count=2,
    )
    flavor_name = str(flavor["name"])
    return RoutingPlan(
        route="quote",
        size="CLASSIC",
        flavors=[flavor_name, flavor_name],
        sauce=str(sauce["name"]),
        toppings=[str(item["name"]) for item in toppings],
    )


def merge_order_plan(
    existing: RoutingPlan | None,
    update: RoutingPlan,
) -> RoutingPlan:
    if existing is None:
        return update

    flavors = list(existing.flavors)
    for flavor in update.flavors:
        if flavor not in flavors:
            flavors.append(flavor)

    toppings = list(existing.toppings)
    for topping in update.toppings:
        if topping not in toppings:
            toppings.append(topping)

    return RoutingPlan(
        route="quote",
        size=update.size or existing.size,
        flavors=flavors,
        sauce=update.sauce or existing.sauce,
        toppings=toppings,
        requested_ready_in_minutes=(
            update.requested_ready_in_minutes or existing.requested_ready_in_minutes
        ),
    )
