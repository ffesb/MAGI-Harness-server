import pytest
from magi.core.config import (
    MindConfig,
    TOLERANCE_DEFAULT,
    TOLERANCE_SECURITY,
    get_effective_system_prompt,
    load_settings,
    update_mind_tolerance,
)


def test_tolerance_default_prompt():
    mind = MindConfig(
        name="MELCHIOR",
        display_name="MELCHIOR-1",
        model="ling-3.0-flash-fin:free",
        system_prompt="Eres Melchior, cientifica de MAGI.",
        tolerance_level="default",
    )
    eff = get_effective_system_prompt(mind)
    assert "Eres Melchior, cientifica de MAGI." in eff
    assert "PRIORIDAD: CRITERIO DE TOLERANCIA [NIVEL: DEFAULT]" in eff
    assert TOLERANCE_DEFAULT in eff


def test_tolerance_security_prompt():
    mind = MindConfig(
        name="BALTHASAR",
        display_name="BALTHASAR-2",
        model="minimax-m3:free",
        system_prompt="Eres Balthasar, madre de MAGI.",
        tolerance_level="seguridad",
    )
    eff = get_effective_system_prompt(mind)
    assert "Eres Balthasar, madre de MAGI." in eff
    assert "PRIORIDAD: CRITERIO DE TOLERANCIA [NIVEL: SEGURIDAD MÁXIMA]" in eff
    assert TOLERANCE_SECURITY in eff


def test_tolerance_solo_personalidad_prompt():
    base_prompt = "Eres Casper, mujer critica e intuitiva."
    mind = MindConfig(
        name="CASPER",
        display_name="CASPER-3",
        model="minimax-m3:free",
        system_prompt=base_prompt,
        tolerance_level="solo_personalidad",
    )
    eff = get_effective_system_prompt(mind)
    assert eff == base_prompt
    assert "PRIORIDAD: CRITERIO DE TOLERANCIA" not in eff


def test_tolerance_personalizado_prompt():
    custom_text = "Priorizar estabilidad de la red por encima de todo."
    mind = MindConfig(
        name="EXECUTOR",
        display_name="AI Ejecutadora",
        model="ling-3.0-flash-fin:free",
        system_prompt="Eres la Ejecutora de MAGI.",
        tolerance_level="personalizado",
        custom_tolerance_prompt=custom_text,
    )
    eff = get_effective_system_prompt(mind)
    assert "Eres la Ejecutora de MAGI." in eff
    assert "PRIORIDAD: CRITERIO DE TOLERANCIA [NIVEL: PERSONALIZADO]" in eff
    assert custom_text in eff


def test_update_mind_tolerance():
    settings = load_settings()
    assert "MELCHIOR" in settings.minds

    # Update to seguridad
    ok = update_mind_tolerance(settings, "MELCHIOR", "seguridad")
    assert ok is True
    assert settings.minds["MELCHIOR"].tolerance_level == "seguridad"

    # Reset back to default
    ok = update_mind_tolerance(settings, "MELCHIOR", "default")
    assert ok is True
    assert settings.minds["MELCHIOR"].tolerance_level == "default"
