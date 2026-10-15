"""Example (non-property) tests for the Stage constants and ``_validate_stage``
(task 1.5).

Verificam, por exemplos diretos (sem Hypothesis), que o ``StageService`` expõe o
pipeline renumerado de **seis** Estágios e que a guarda de Estágio aceita 1..6 e
rejeita valores fora do intervalo:

1. Constantes — ``FIRST_STAGE == 1``, ``LAST_STAGE == 6`` e
   ``VALID_STAGES == (1, 2, 3, 4, 5, 6)`` (Req. 1.1, 1.2).
2. Constantes nomeadas dos seis Estágios — ``ESTAGIO_SELECAO_BASE == 1``,
   ``ESTAGIO_SELECAO_CAMPANHA == 2``, ``ESTAGIO_GERACAO_DAG == 3``,
   ``ESTAGIO_OPERACIONALIZACAO == 4``, ``ESTAGIO_AMBIENTE == 5`` e
   ``ESTAGIO_EXECUCAO == 6`` (Req. 1.1).
3. ``_validate_stage`` — aceita cada um de 1, 2, 3, 4, 5, 6 sem levantar erro e
   rejeita 0 e 7 levantando ``ValueError`` (a mensagem cita ``VALID_STAGES``).

_Requisitos: 1.1, 1.2_
"""

from __future__ import annotations

import pytest

from app.services.stage.stage_service import (
    ESTAGIO_AMBIENTE,
    ESTAGIO_EXECUCAO,
    ESTAGIO_GERACAO_DAG,
    ESTAGIO_OPERACIONALIZACAO,
    ESTAGIO_SELECAO_BASE,
    ESTAGIO_SELECAO_CAMPANHA,
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
    assert ESTAGIO_SELECAO_BASE == 1
    assert ESTAGIO_SELECAO_CAMPANHA == 2
    assert ESTAGIO_GERACAO_DAG == 3
    assert ESTAGIO_OPERACIONALIZACAO == 4
    assert ESTAGIO_AMBIENTE == 5
    assert ESTAGIO_EXECUCAO == 6


@pytest.mark.parametrize("estagio", [1, 2, 3, 4, 5, 6])
def test_validate_stage_accepts_each_valid_stage(estagio: int) -> None:
    # Não deve levantar para nenhum Estágio válido.
    assert _validate_stage(estagio) is None


@pytest.mark.parametrize("estagio", [0, 7])
def test_validate_stage_rejects_out_of_range(estagio: int) -> None:
    with pytest.raises(ValueError) as excinfo:
        _validate_stage(estagio)
    # A mensagem cita o conjunto de Estágios válidos (1, 2, 3, 4, 5, 6).
    assert str(VALID_STAGES) in str(excinfo.value)
