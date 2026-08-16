"""Concierge customer-facing response rendering."""

import json
from typing import Any

from sundae_funday.concierge.api import RoutingPlan
from sundae_funday.concierge.routing import special_order_plan


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def render_special_reply(result: dict[str, Any]) -> str:
    plan = special_order_plan(result)
    return (
        "Today's special is a Classic Sundae with "
        f"two scoops of {plan.flavors[0]}, {plan.sauce}, "
        f"{plan.toppings[0]}, and {plan.toppings[1]}. "
    )


def render_fulfillment_failure(result: dict[str, Any]) -> str:
    unavailable = [
        str(item["name"])
        for item in result.get("requested_items", [])
        if isinstance(item, dict) and item.get("available") is False
    ]
    detail = f" Unavailable right now: {', '.join(unavailable)}." if unavailable else ""
    return f"Oops! Scoops cannot be fulfilled as requested.{detail}"


def display_order_number(order_id: Any) -> str:
    digits = "".join(character for character in str(order_id) if character.isdigit())
    return digits[:2]


def ops_request(operation: str, **arguments: Any) -> str:
    return "SUNDAE_OPS_REQUEST " + compact_json(
        {"operation": operation, "arguments": arguments}
    )


def render_menu_reply(menu: dict[str, Any]) -> str:
    sizes = ", ".join(
        f"{item['name']} {item['price_display']}" for item in menu.get("sizes", [])
    )
    flavors = ", ".join(item["name"] for item in menu.get("flavors", []))
    toppings = ", ".join(item["name"] for item in menu.get("toppings", [])[:4])
    return (
        f"Sizes: {sizes}. Flavors: {flavors}. Popular toppings include {toppings}. "
        "Tell me a size, flavors, sauce, and toppings and I will build it."
    )


def render_quote_reply(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "needs_clarification":
        return str(
            result.get("message", "I need a little more detail for that sundae.")
        )
    if status == "unavailable":
        items = ", ".join(
            item["name"]
            for item in result.get("unavailable_items", [])
            if isinstance(item, dict)
        )
        detail = f" Unavailable right now: {items}." if items else ""
        return f"I cannot build that exactly as requested.{detail}"
    quote = result.get("quote")
    order = result.get("order")
    if not isinstance(quote, dict) or not isinstance(order, dict):
        return "Your sundae is ready. Use Confirm if it looks right."
    flavors = ", ".join(item["name"] for item in order.get("flavors", []))
    sauce = order.get("sauce")
    sauce_text = f" with {sauce['name']}" if isinstance(sauce, dict) else ""
    toppings = ", ".join(item["name"] for item in order.get("toppings", []))
    toppings_text = f" plus {toppings}" if toppings else ""
    eta = quote.get("eta_minutes")
    eta_text = f" It should take about {eta} minutes." if isinstance(eta, int) else ""
    if quote.get("requested_ready_in_minutes") is not None:
        if quote.get("can_meet_requested_time"):
            eta_text += " That fits your requested timing."
        else:
            eta_text += " That is slower than your requested timing."
    return (
        f"Your sundae is ready: {order['size']['name']} with {flavors}"
        f"{sauce_text}{toppings_text}. "
        f"Total {quote['total_display']}.{eta_text} Press confirm to submit it."
    )


def render_general_reply() -> str:
    return (
        "I can show the menu, build a sundae, or check inventory for you. "
        "What sounds good?"
    )


def build_writer_prompt(
    context: str,
    message: str,
    plan: RoutingPlan,
    tool_result: Any,
    *,
    needs_confirmation: bool,
) -> str:
    return (
        f"Conversation context:\n{context}\n\n"
        f"Current customer message:\n{message}\n\n"
        f"Routing plan:\n{compact_json(plan.model_dump(mode='json'))}\n\n"
        "Needs confirmation:\n"
        f"{compact_json({'needs_confirmation': needs_confirmation})}\n\n"
        f"Authoritative result:\n{compact_json(tool_result)}"
    )
