"""Provider registry. Selection is undecided, so nothing is registered in M0;
M1 adds the first real implementation(s) here."""

AVAILABLE: dict[str, set[str]] = {"search": set(), "llm": set(), "fetch": set()}
