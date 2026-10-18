"""Example (non-property) tests for the Stage constants and ``_validate_stage``
(task 1.5).

Verificam, por exemplos diretos (sem Hypothesis), que o ``StageService`` expõe o
pipeline renumerado de **seis** Estágios e que a guarda de Estágio aceita 1..6 e
rejeita valores fora do intervalo:

1. Constantes — ``FIRST_STAGE == 1``, ``LAST_STAGE == 6`` e
   ``VALID_STAGES == (1, 2, 3, 4, 5, 6)`` (Req. 1.1, 1.2).
2. Constantes nomeadas dos seis Estágios — ``STAGE_BASE_SELECTION == 1``,
   ``STAGE_CAMPAIGN_SELECTION == 2``, ``STAGE_DAG_GENERATION == 3``,
   ``STAGE_OPERATIONALIZATION == 4``, ``STAGE_ENVIRONMENT == 5`` e
   ``STAGE_EXECUTION == 6`` (Req. 1.1).
3. ``_validate_stage`` — aceita cada um de 1, 2, 3, 4, 5, 6 sem levantar erro e
   rejeita 0 e 7 levantando ``ValueError`` (a mensagem cita ``VALID_STAGES``).

_Requirements: 1.1, 1.2_
"""

from __future__ import annotations

import pytest

from app.services.stage.stage_service import (
    STAGE_ENVIRONMENT,
    STAGE_EXECUTION,
    STAGE_DAG_GENERATION,
    STAGE_OPERATIONALIZATION,
    STAGE_BASE_SELECTION,
    STAGE_CAMPAIGN_SELECTION,
    FIRST_STAGE,
    LAST_STAGE,
    VALID_STAGES,
    _validate_stage,
)


def test_first_stage_is_one() -> None:
    assert FIRST_STAGE == 1


def test_last_stage_is_six() -> None:
    assert LAST_STAGE == 6


def test_valid_stages_is_one_through_six() -> None:
    assert VALID_STAGES == (1, 2, 3, 4, 5, 6)


def test_named_stage_constants() -> None:
    # Papéis dos seis Estágios após a renumeração (fonte única por número).
    assert STAGE_BASE_SELECTION == 1
    assert STAGE_CAMPAIGN_SELECTION == 2
    assert STAGE_DAG_GENERATION == 3
    assert STAGE_OPERATIONALIZATION == 4
    assert STAGE_ENVIRONMENT == 5
    assert STAGE_EXECUTION == 6


@pytest.mark.parametrize("stage", [1, 2, 3, 4, 5, 6])
def test_validate_stage_accepts_each_valid_stage(stage: int) -> None:
    # Não deve levantar para nenhum Estágio válido.
    assert _validate_stage(stage) is None


@pytest.mark.parametrize("stage", [0, 7])
def test_validate_stage_rejects_out_of_range(stage: int) -> None:
    with pytest.raises(ValueError) as excinfo:
        _validate_stage(stage)
    # A mensagem cita o conjunto de Estágios válidos (1, 2, 3, 4, 5, 6).
    assert str(VALID_STAGES) in str(excinfo.value)
