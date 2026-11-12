"""Bounded JSON values shared by client input and provider metadata."""
import json
import math


def loads(payload, *, max_depth=32):
    """Decode finite JSON with valid Unicode and bounded container nesting."""
    try:
        data = json.loads(payload)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the supported depth") from None
    # Python's decoder accepts lone surrogates and non-finite float values.
    # Validate iteratively, including dictionary keys, before persistence or CSV.
    stack = [iter((data,))]
    while stack:
        try:
            value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, dict):
            if len(stack) > max_depth:
                raise ValueError("JSON nesting exceeds the supported depth")
            stack.append(item for pair in value.items() for item in pair)
        elif isinstance(value, list):
            if len(stack) > max_depth:
                raise ValueError("JSON nesting exceeds the supported depth")
            stack.append(iter(value))
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
    return data
