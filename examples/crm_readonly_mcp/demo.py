"""Print reproducible example output without needing an LLM or MCP dependency."""

import json

from .core import dispatch


def main() -> None:
    """Print all three default tool results deterministically."""
    for name in ("crm_data_contract", "crm_funnel_summary", "crm_review_candidates"):
        print(f"\n=== {name} (synthetic data only) ===")
        print(json.dumps(dispatch(name, {}), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
