"""Property test da derivação dos parâmetros de environment — Estágio 4 (task 5.4).

# Feature: pipeline-seis-stages, Property 6: Derivação dos parâmetros de
# environment a partir dos nós curados. Para qualquer DAG curated,
# ``CurationOperationalizer._derive_parameters`` deriva um
# ``EnvironmentParameters`` fiel aos nós curados: todo destino interno resolvido
# vira um host-target com o contêiner correto (via ``container_for_ip``); as
# vulnerabilities (``target_vuln_host_a``/``_b``), o ``host_type``
# (``target_host_type``) e as notas de rede (``network_context``) são carregados
# dos nós; e a platform é SEMPRE ``CONTAINER``.

Validates: Requirements 8.3, 8.4, 17.1

Esta é uma property test de *feature* (``pipeline-seis-stages`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual
de :meth:`~app.services.environment.operationalizer.CurationOperationalizer._derive_parameters`.

Estratégia / isolation
-----------------------
Cada exemplo gera, via Hypothesis, um conjunto de nós curados (``structural_nodes``)
que combinam:

* ``attacker_commands`` apontando para um subconjunto arbitrário dos IPs
  estáticos conhecidos do laboratório (``STATIC_IP_CONTAINERS``) e, opcionalmente,
  para IPs internos SEM contêiner estático e para destinations externos (que NÃO
  devem virar host-target);
* ``target_vuln_host_a``/``target_vuln_host_b`` (vulnerabilities),
  ``target_host_type`` (type de host) e ``network_context`` (nota de rede)
  arbitrários.

O conjunto é escrito como ``{slug}_dag.json`` em um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` e um
:class:`~app.services.case_service.CaseService` apontado para ele — exatamente
como os arquivos reais são parseados. Um ``CaseService`` injetado mantém a
derivação pura: sem rede, sem Docker, sem IA (Req. 11.3, 11.4). Hypothesis roda
≥100 iterações.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.containment import container_for_ip, is_internal_ip, parse_command
from app.services.containment.subnets import STATIC_IP_CONTAINERS
from app.services.dag.dag_service import DagView
from app.services.environment.operationalization import EnvironmentPlatform
from app.services.environment.operationalizer import CurationOperationalizer


# Um slug de campaign curated real: ``try_load_case`` só aceita slugs registrados.
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curated

# IPs estáticos do laboratório que resolvem para um contêiner conhecido.
_STATIC_IPS: list[str] = sorted(STATIC_IP_CONTAINERS)
# Guarda: todos os IPs estáticos são internos (invariante do laboratório).
assert all(is_internal_ip(ip) for ip in _STATIC_IPS)

# Um IP interno (192.168.20.0/24) SEM atribuição estática de contêiner: deve ser
# classificado interno porém resolver para ``container == None``.
_INTERNAL_NO_CONTAINER = "192.168.20.99"
assert is_internal_ip(_INTERNAL_NO_CONTAINER)
assert container_for_ip(_INTERNAL_NO_CONTAINER) is None

# Um destino external: NUNCA deve virar host-target (não é interno).
_EXTERNAL_IP = "8.8.8.8"
assert not is_internal_ip(_EXTERNAL_IP)


# ---------------------------------------------------------------------------
# Fixture builder — escreve ability + {slug}_dag.json num data dir temporário,
# com o layout <data_dir>/{api,dag}/ que CaseService(data_dir=...) resolve.
# ---------------------------------------------------------------------------


def _write_case_fixture(data_dir: Path, slug: str, structural_nodes: list[dict]) -> None:
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    # Abilities mínimas (a derivação de parâmetros não as usa, mas o campaign curated
    # precisa de um file de abilities válido para parsear sem erro).
    (api_dir / f"{slug}_dag-ability.json").write_text(
        json.dumps(
            [
                {
                    "ability_id": f"{slug}-ab-1",
                    "name": "seed",
                    "technique_id": "T1190",
                    "executors": [
                        {"name": "sh", "platform": "linux", "command": "echo 1"}
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(
            {
                "campaign_name": "Seed",
                "structural_nodes": structural_nodes,
                "metadata": {"total_techniques": len(structural_nodes)},
            }
        ),
        encoding="utf-8",
    )


def _stub_dag() -> DagView:
    """Um ``DagView`` curated mínimo (a derivação lê os nós do disco)."""
    return DagView(campaign_id=_CURATED_SLUG, name="Seed", curated=True, can_advance=True)


# ---------------------------------------------------------------------------
# Estratégias — nós curados com destinations/vulnerabilities/host_type/contexto.
# ---------------------------------------------------------------------------

_destino = st.one_of(
    st.sampled_from(_STATIC_IPS),  # interno COM contêiner estático
    st.just(_INTERNAL_NO_CONTAINER),  # interno SEM contêiner estático
    st.just(_EXTERNAL_IP),  # external — nunca vira host-target
)

_host_type = st.one_of(st.none(), st.sampled_from(["kali", "macos", "linux", "windows"]))
_network_context = st.one_of(
    st.none(),
    st.sampled_from(
        [
            "Attacker can directly access Host A",
            "nested hop",
            "lateral movement to backend",
        ]
    ),
)
_vulns = st.lists(
    st.sampled_from(["setup A", "setup B", "flask run --host 0.0.0.0 --port 5000"]),
    min_size=0,
    max_size=2,
    unique=True,
)


@st.composite
def _curated_node(draw: st.DrawFn, index: int) -> dict:
    """Gera um nó curated com commands para 0..3 destinations e campos de environment."""
    destinations = draw(st.lists(_destino, min_size=0, max_size=3, unique=True))
    # Comandos concretos cujo parser extrai exatamente esses destinations.
    commands = [f'curl "http://{dest}:5000/?cmd=id"' for dest in destinations]
    return {
        "node_id": f"n{index}",
        "node_index": index,
        "technique_id": draw(st.sampled_from(["T1190", "T1059", "T1021", "T1055"])),
        "attacker_commands": commands,
        "target_vuln_host_a": draw(_vulns),
        "target_vuln_host_b": draw(_vulns),
        "source_host_type": draw(_host_type),
        "target_host_type": draw(_host_type),
        "network_context": draw(_network_context),
    }


@st.composite
def _curated_nodes(draw: st.DrawFn) -> list[dict]:
    n = draw(st.integers(min_value=1, max_value=4))
    return [draw(_curated_node(i)) for i in range(n)]


# ---------------------------------------------------------------------------
# Property 6 — derivação fiel dos parâmetros de environment dos nós curados.
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(nodes=_curated_nodes())
def test_derivation_environment_parameters(tmp_path: Path, nodes: list[dict]) -> None:
    """Deriva EnvironmentParameters fiel aos nós curados; platform sempre CONTAINER.

    # Feature: pipeline-seis-stages, Property 6: Derivação dos parâmetros de
    # environment a partir dos nós curados.
    Validates: Requirements 8.3, 8.4, 17.1
    """
    _write_case_fixture(tmp_path, _CURATED_SLUG, nodes)
    op = CurationOperationalizer(case_service=CaseService(data_dir=tmp_path))

    params = op.operationalize(_CURATED_SLUG, _stub_dag()).parameters

    # --- Invariante 1: platform SEMPRE CONTAINER (Req. 17.1) --------------
    assert params.platform is EnvironmentPlatform.CONTAINER
    assert params.campaign_id == _CURATED_SLUG

    # Recalcula, dos nós, o conjunto esperado de destinations internos (fonte da
    # verdade independente da implementação) e as notas/host_types presentes.
    enderecos_internos: list[str] = []
    vulns_esperadas: set[str] = set()
    host_types_esperados: set[str] = set()
    notas_esperadas: list[str] = []
    for no in nodes:
        for command in no["attacker_commands"]:
            for dest in parse_command(command).destinations:
                if is_internal_ip(dest.value) and dest.value not in enderecos_internos:
                    enderecos_internos.append(dest.value)
        vulns_esperadas.update(no["target_vuln_host_a"])
        vulns_esperadas.update(no["target_vuln_host_b"])
        if no["target_host_type"] is not None:
            host_types_esperados.add(no["target_host_type"])
        if no["network_context"]:
            notas_esperadas.append(no["network_context"])

    # --- Invariante 2: todo destino interno resolvido vira host com o -------
    # ----- contêiner correto via container_for_ip (Req. 8.3) ----------------
    # A derivação consolida hosts por contêiner (vários IPs internos que mapeiam
    # para o MESMO contêiner do laboratório viram um único host-target — vide o
    # exemplo ``test_merges_same_target_container_across_nodes``). Logo a verdade
    # independente é por CONTÊINER para os destinations com contêiner estático, e por
    # ENDEREÇO para os destinations internos sem contêiner estático (chaveados por IP).
    conteineres_esperados = {
        container_for_ip(ip) for ip in enderecos_internos if container_for_ip(ip) is not None
    }
    enderecos_internos_sem_conteiner = {
        ip for ip in enderecos_internos if container_for_ip(ip) is None
    }

    conteineres_derivados = {h.container for h in params.hosts if h.container is not None}
    enderecos_derivados = {h.address for h in params.hosts if h.address is not None}

    # Todo contêiner de um destino interno resolvido está representado, e vice-versa.
    assert conteineres_derivados == conteineres_esperados
    # Todo destino interno sem contêiner estático aparece como host pelo seu IP.
    assert enderecos_internos_sem_conteiner <= enderecos_derivados

    # Nenhum destino external foi promovido a host-target com endereço.
    assert _EXTERNAL_IP not in enderecos_derivados

    # Para cada host com endereço, o endereço é interno e o contêiner é
    # EXATAMENTE container_for_ip(address) — nada inventado (Req. 8.3).
    for host in params.hosts:
        if host.address is not None:
            assert is_internal_ip(host.address)
            assert host.container == container_for_ip(host.address)

    # --- Invariante 3: vulnerabilities / host_type / notas carregados ------
    # ----- dos nós (Req. 8.3, 8.4) -------------------------------------------
    # network_context: cada nota única presente nodes nós aparece (ordem preservada,
    # de-duplicada), e nenhuma nota inventada aparece.
    notas_unicas_ordenadas: list[str] = []
    for nota in notas_esperadas:
        if nota not in notas_unicas_ordenadas:
            notas_unicas_ordenadas.append(nota)
    assert params.network_context == notas_unicas_ordenadas

    # host_type: todo host_type carregado veio de algum target_host_type dos nós.
    host_types_derivados = {h.host_type for h in params.hosts if h.host_type is not None}
    assert host_types_derivados <= host_types_esperados

    # vulnerabilities: toda vulnerabilidade exposta por algum host veio dos nós
    # (nada inventado), e toda vulnerabilidade curated é exposta por algum host.
    vulns_derivadas: set[str] = set()
    for host in params.hosts:
        vulns_derivadas.update(host.vulnerabilities)
    assert vulns_derivadas <= vulns_esperadas
    assert vulns_esperadas <= vulns_derivadas

    # Origem carimbada é a da curation.
    assert params.origin is op.origin()


def test_missing_dag_still_container_platform(tmp_path: Path) -> None:
    """Mesmo sem DAG curated, a platform derivada é CONTAINER e os campos vazios.

    # Feature: pipeline-seis-stages, Property 6: Derivação dos parâmetros de
    # environment a partir dos nós curados (campaign-limite: sem nós).
    Validates: Requirements 8.4, 17.1
    """
    # Semeia apenas abilities, sem {slug}_dag.json.
    (tmp_path / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "api" / f"{_CURATED_SLUG}_dag-ability.json").write_text(
        "[]", encoding="utf-8"
    )
    op = CurationOperationalizer(case_service=CaseService(data_dir=tmp_path))

    params = op.operationalize(_CURATED_SLUG, _stub_dag()).parameters

    assert params.platform is EnvironmentPlatform.CONTAINER
    assert params.hosts == []
    assert params.services == []
    assert params.network_context == []
