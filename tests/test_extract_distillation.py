from scripts.extract_distillation import _score_paper


def test_scores_llm_distillation_paper():
    score, groups, terms = _score_paper({
        "title": "Knowledge Distillation for Large Language Models",
        "abstract": "We distill a large language model into a compact student model.",
    })

    assert score > 0
    assert "distillation" in groups
    assert "llm" in groups
    assert any("knowledge distillation" in term for term in terms)


def test_scores_model_extraction_attack():
    score, groups, terms = _score_paper({
        "title": "Data-Free Model Extraction Attack",
        "abstract": "A black-box extraction method trains a surrogate model.",
    })

    assert score > 0
    assert "attack" in groups
    assert "technique" in groups
    assert any("model extraction" in term for term in terms)


def test_surrogate_model_alone_is_not_an_attack():
    score, groups, _ = _score_paper({
        "title": "Knowledge Distillation with Surrogate Models",
        "abstract": "We use a surrogate model for knowledge distillation.",
    })

    assert score > 0
    assert "distillation" in groups
    assert "technique" in groups
    assert "attack" not in groups


def test_generic_llm_paper_is_not_enough():
    score, groups, terms = _score_paper({
        "title": "A Survey of Large Language Models",
        "abstract": "We discuss training and deployment of LLMs.",
    })

    assert score == 0
    assert groups == []
    assert terms == []


def test_watermarking_defense_is_included():
    score, groups, terms = _score_paper({
        "title": "Watermark Robustness to Knowledge Distillation",
        "abstract": "We study ownership verification and model watermarking under distillation.",
    })

    assert score > 0
    assert "defense" in groups
    assert "distillation" in groups
    assert any("watermark" in term for term in terms)


def test_model_compression_overlap_is_included():
    score, groups, terms = _score_paper({
        "title": "Compact Models via Pruning and Knowledge Distillation",
        "abstract": "Model compression improves small model efficiency.",
    })

    assert score > 0
    assert "compression" in groups
    assert "distillation" in groups
    assert any("model compression" in term for term in terms)
