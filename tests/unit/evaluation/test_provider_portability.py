from athena.evaluation.provider_portability import (
    ProviderParityObservation,
    compare_provider_parity,
)


def _obs(provider, text="done", tool=("fs.read", '{"path":"README.md"}')):
    return ProviderParityObservation(
        provider=provider,
        model=f"{provider}-model",
        block_kinds=("text", "tool"),
        text=text,
        tool_calls=(tool,),
        input_tokens=10,
        output_tokens=4,
    )


def test_provider_parity_accepts_equivalent_normalized_responses():
    report = compare_provider_parity(
        "mixed-response",
        {
            "openai": _obs("openai"),
            "anthropic": _obs("anthropic"),
        },
    )
    assert report["shape_parity"] is True
    assert report["mismatches"] == {}
    assert "no live external provider" in report["limitations"]


def test_provider_parity_exposes_normalized_mismatch():
    report = compare_provider_parity(
        "mixed-response",
        {
            "openai": _obs("openai"),
            "anthropic": _obs("anthropic", text="different"),
        },
    )
    assert report["shape_parity"] is False
    assert "openai" in report["mismatches"]
