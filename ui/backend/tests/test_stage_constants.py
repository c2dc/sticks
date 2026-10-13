"""Example (non-property) tests for the Stage constants and ``_validate_stage``
(task 1.2).

Verificam, por exemplos diretos (sem Hypothesis), que o ``StageService`` expõe o
pipeline renumerado de cinco Estágios e que a guarda de Estágio aceita 1..5 e
rejeita valores fora do intervalo:

1. Constantes — ``FIRST_STAGE == 1``, ``LAST_STAGE == 5`` e
   ``VALID_STAGES == (1, 2, 3, 4, 5)`` (Req. 1.1, 8.1).
2. Constantes nomeadas dos Estágios — ``ESTAGIO_MODELAGEM == 1``,
   ``ESTAGIO_GERACAO_DAG == 2``, ``ESTAGIO_OPERACIONALIZACAO == 3``,
   ``ESTAGIO_PROVISIONAMENTO == 4`` e ``ESTAGIO_EXECUCAO == 5`` (Req. 1.1).
3. ``_validate_stage`` — aceita cada um de 1, 2, 3, 4, 5 sem levantar erro e
   rejeita 0 e 6 levantando ``ValueError`` (a mensagem cita ``VALID_STAGES``).

_Requisitos: 1.1, 8.1_
"""

from __future__ import annotations

import pytest

from app.services.stage.stage_service import (
    ESTAGIO_EXECUCAO,
    ESTAGIO_GERACAO_DAG,
    ESTAGIO_MODELAGEM,
    ESTAGIO_OPERACIONALIZACAO,
    ESTAGIO_PROVISIONAMENTO,
    FIRST_STAGE,
    LAST_STAGE,
    VALID_STAGES,
    _validate_stage,
)


def test_first_stage_is_one() -> None:
    assert FIRST_STAGE == 1


def test_last_stage_is_five() -> None:
    assert LAST_STAGE == 5


def test_valid_stages_is_one_through_five() -> None:
    assert VALID_STAGES == (1, 2, 3, 4, 5)


def test_named_stage_constants() -> None:
    # Papéis dos Estágios após a renumeração para cinco Estágios.
    assert ESTAGIO_MODELAGEM == 1
    assert ESTAGIO_GERACAO_DAG == 2
    assert ESTAGIO_OPERACIONALIZACAO == 3
    assert ESTAGIO_PROVISIONAMENTO == 4
    assert ESTAGIO_EXECUCAO == 5


@pytest.mark.parametrize("estagio", [1, 2, 3, 4, 5])
def test_validate_stage_accepts_each_valid_stage(estagio: int) -> None:
    # Não deve levantar para nenhum Estágio válido.
    assert _validate_stage(estagio) is None


@pytest.mark.parametrize("estagio", [0, 6])
def test_validate_stage_rejects_out_of_range(estagio: int) -> None:
    with pytest.raises(ValueError) as excinfo:
        _validate_stage(estagio)
    # A mensagem cita o conjunto de Estágios válidos (1, 2, 3, 4, 5).
    assert str(VALID_STAGES) in str(excinfo.value)
