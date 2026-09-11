from athena.synthesis.proof_corpus import corpus_digest, derive_proof_cases


def test_generated_proof_corpus_is_derived_from_schema_and_effects():
    cases = derive_proof_cases(
        {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        {"READ_LOCAL": True, "WRITE_LOCAL": True},
    )
    records = [case.to_record() for case in cases]
    assert {record["kind"] for record in records} == {"positive", "negative", "effect"}
    assert any(record["id"].startswith("derived:missing-required:") for record in records)
    assert any(record["expected"] == "reject" for record in records)
    assert all(record["source"] == "schema_and_effects" for record in records)
    assert corpus_digest(cases) == corpus_digest(cases)
