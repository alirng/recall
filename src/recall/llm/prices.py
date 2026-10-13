"""What metered models cost, so a nightly run can keep to a dollar budget and report its cost.

USD per million tokens at standard (synchronous) rates, from each vendor's pricing page
(Anthropic: platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-13). Batch
APIs charge half. Prices change: set `price = [input, output]` in config.toml's [model] table
to override a model's entry, or to price a model this table does not list. Without a price,
recall still runs but cannot enforce max_usd, and says so.
"""

# model: (input, output)
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Models with a dearer rate card for long prompts: (prompt tokens above which it applies, input, output)
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 0.50, 2.50)}
BATCH_DISCOUNT = 0.5


def lookup(model: str, override: list | tuple | None = None) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None when unknown."""
    if override:
        return float(override[0]), float(override[1])
    return PRICES.get(model.removeprefix("anthropic/"))  # OpenRouter-style names too


def cost(
    model: str, tokens_in: int, tokens_out: int, batch: bool = False, override: list | tuple | None = None
) -> float | None:
    """USD for one call, or None when the model's price is unknown."""
    price = lookup(model, override)
    if price is None:
        return None
    rate_in, rate_out = price
    long = LONG_PROMPT.get(model)
    if long and not override and tokens_in > long[0]:
        rate_in, rate_out = long[1], long[2]
    usd = (tokens_in * rate_in + tokens_out * rate_out) / 1e6
    return usd * BATCH_DISCOUNT if batch else usd
