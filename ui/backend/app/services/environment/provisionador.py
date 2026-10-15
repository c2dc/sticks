"""Costura de criação do ambiente: ``ProvisionadorDeAmbiente`` + DTOs + curadoria.

Este módulo define o **ponto de extensão por origem** do Estágio 5 (Criação do
ambiente de simulação) no pipeline de seis Estágios, além de carregar a costura
histórica do Estágio 4 (Provisionamento) para compatibilidade. Separa *como* o
ambiente de um Caso é criado/verificado (hoje por curadoria sobre o laboratório
estático do ``docker-compose``, amanhã por um agente de IA que **cria**
contêineres/VMs/redes dinamicamente) do *resto* do sistema, de modo que uma
implementação ``gerado_ia`` possa ser encaixada depois **sem** alterar esta
assinatura (Req. 9.1, 16.3, 16.4).

Saída tipada do Estágio 5 (task 7.1):

``IsolationResumo`` / ``VereditoContainer``
    Projeção serializável (Pydantic) do :class:`IsolationResult` do pacote
    ``containment`` — veredito por contêiner + resultado geral (Req. 9.8, 14.3).

``AmbienteProvisionado``
    A Saída do Estágio 5: ambiente pronto e isolado (caso, plataforma,
    contêineres-alvo, ``compose``, resumo de isolamento, Abilities carregadas,
    origem) — Req. 9.8.

``ProvisionadorDeAmbiente``
    Um :class:`typing.Protocol` ``@runtime_checkable`` com uma única operação —
    ``preparar(caso) -> ResultadoProvisionamento`` — que prepara/verifica o
    ambiente de um Caso e devolve um resultado tipado carregando: se o ambiente
    está **pronto/isolado**, os veredictos de isolamento por contêiner e a
    **topologia** (hosts/redes/mermaid) para exibição. A implementação de
    curadoria apenas **verifica e expõe** (read-only); a futura
    ``AiProvisionadorDeAmbiente`` **criará** o ambiente sob a mesma assinatura.

``ResultadoProvisionamento``
    O resultado tipado de uma preparação/verificação: ``pronto``/``isolado``, o
    :class:`~app.services.containment.IsolationResult` com os veredictos por
    contêiner e a :class:`~app.services.environment.topologia_builder.TopologyResponse`
    com a topologia do ambiente contido.

``CuradoriaProvisionadorDeAmbiente``
    A implementação de curadoria desta iteração. **NÃO** inicia, cria ou modifica
    contêineres (read-only); **sem IA e sem rede pública**. Em ``provisionar`` ela
    (1) resolve os contêineres-alvo dos ``ParametrosDeAmbiente``, (2) **carrega as
    Abilities ordenadas** (pela ``atomic_ordering`` do DAG) e o Adversary no
    Caldera reusando ``CalderaClient.load_abilities``/``load_adversary`` — o
    cliente é **injetável** (um *fake* nos testes, o real em produção) — e (3)
    **verifica o isolamento** via :func:`app.services.containment.inspect_isolation`
    com um :class:`~app.services.containment.ContainerInspector` **injetável**
    (um inspector falso nos testes, o adaptador real do Docker em produção),
    ANTES de concluir (fail-closed: lista vazia **ou** daemon indisponível
    reprova). Verifica exatamente os contêineres que a Execução irá atacar.

Como no restante dos pacotes de serviço (``dag``/``stage``), a costura é um
``@runtime_checkable Protocol`` e as dependências externas (inspector, Caso) são
**injetadas**, com os defaults de produção resolvidos preguiçosamente.

_Requisitos: 5.1, 5.4, 5.5, 5.7, 10.2_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import TranslationSource
from app.services.case_service import CaseData, CaseService
from app.services.containment import (
    ContainerInspector,
    IsolationResult,
    inspect_isolation,
)
from app.services.environment.compose_executor import (
    ComposeExecutor,
    DockerComposeExecutor,
    LinhaLog,
    ResultadoComando,
)
from app.services.environment.operacionalizacao import (
    ParametrosDeAmbiente,
    PlataformaAmbiente,
)
from app.services.environment.saude import (
    ContainerStateInspector,
    ResumoSaude,
    inspect_saude,
)
from app.services.environment.topologia_builder import (
    HOST_CONTAINERS,
    TopologyResponse,
    compose_path,
    construir_hosts,
    construir_redes,
    container_names,
    montar_mermaid,
)


# ---------------------------------------------------------------------------
# DTOs tipados da Saída do Estágio 5 (task 7.1)
# ---------------------------------------------------------------------------


class VereditoContainer(BaseModel):
    """Veredito de isolamento de **um** contêiner-alvo (Req. 9.8, 14.3, 14.4).

    Espelho serializável (Pydantic) de
    :class:`~app.services.containment.ContainerIsolationVerdict` (um
    ``dataclass`` interno do pacote ``containment``), para compor a Saída tipada
    do Estágio 5 e os contratos JSON pt-BR da API sem vazar o dataclass interno.

    Attributes:
        container: Nome/id do contêiner inspecionado.
        isolado: ``True`` sse o contêiner passou em **todas** as verificações de
            isolamento (anexado exclusivamente a redes ``internal: true``, sem
            bridge externo e sem DNS externo — Req. 14.2).
        razoes: Razões legíveis pelas quais o contêiner **não** está isolado;
            vazia quando ``isolado`` é ``True`` (Req. 14.4).
        redes_externas: Nomes das redes anexadas julgadas não-internas.
        dns_externo: Servidores de DNS julgados externos.
    """

    container: str
    isolado: bool
    razoes: list[str] = Field(default_factory=list)
    redes_externas: list[str] = Field(default_factory=list)
    dns_externo: list[str] = Field(default_factory=list)


class IsolationResumo(BaseModel):
    """Resumo tipado do isolamento — veredito por contêiner + geral (Req. 9.8, 14.3).

    Projeção serializável (Pydantic) de
    :class:`~app.services.containment.IsolationResult` para a Saída do Estágio 5 e
    os contratos JSON pt-BR da API. Carrega o resultado **geral** (``aprovado``) e
    o veredito **por contêiner** (``veredictos``), de modo que a tela do Estágio 5
    exiba a razão de cada contêiner não isolado (Req. 9.7, 14.4).

    Attributes:
        aprovado: ``True`` sse **todos** os contêineres-alvo estão isolados
            (resultado geral pass/fail — Req. 14.3). Fail-closed: lista vazia de
            contêineres reprova.
        veredictos: Veredito por contêiner, na ordem em que foram verificados.
    """

    aprovado: bool = False
    veredictos: list[VereditoContainer] = Field(default_factory=list)

    @classmethod
    def de_isolation_result(cls, resultado: IsolationResult) -> "IsolationResumo":
        """Converte o :class:`IsolationResult` do ``containment`` neste resumo.

        A costura do Estágio 5 reusa o pacote ``containment`` (fonte única da
        verificação read-only de isolamento, Req. 14.1) e **projeta** o seu
        ``dataclass`` neste modelo Pydantic — sem duplicar a lógica de
        classificação interno/externo.
        """
        return cls(
            aprovado=resultado.passed,
            veredictos=[
                VereditoContainer(
                    container=v.container,
                    isolado=v.isolated,
                    razoes=list(v.reasons),
                    redes_externas=list(v.external_networks),
                    dns_externo=list(v.external_dns),
                )
                for v in resultado.verdicts
            ],
        )

    @property
    def nao_isolados(self) -> list[VereditoContainer]:
        """Os veredictos dos contêineres que falharam no isolamento."""
        return [v for v in self.veredictos if not v.isolado]


class AmbienteProvisionado(BaseModel):
    """Saída do Estágio 5 — ambiente pronto e isolado (Req. 9.8).

    Artefato tipado produzido por :meth:`ProvisionadorDeAmbiente.provisionar`
    quando o ambiente da campanha está pronto: os contêineres-alvo resolvidos a
    partir dos :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`,
    o arquivo de ``compose`` do laboratório curado, o resumo de isolamento
    (veredito por contêiner + geral) e o indicador de que as Abilities ordenadas
    foram carregadas no Caldera (Req. 9.5, 9.8).

    O Estágio 5 só conclui quando os três pré-requisitos passam —
    ``saude.aprovado`` é ``True`` (contêineres saudáveis — Req. 5.1),
    ``abilities_carregadas`` é ``True`` (Abilities no Caldera — Req. 5.2) e
    ``isolamento.aprovado`` é ``True`` (isolamento aprovado — Req. 5.3) —, o que a
    propriedade :attr:`pronto` resume (Req. 5.5); caso contrário o Estágio 6 fica
    bloqueado (Req. 6.4) e ``etapa_falha``/``mensagem_erro`` identificam **o que**
    e **por quê** a etapa falhou (Req. 7.1, 7.2).

    Attributes:
        caso_id: *Slug* do Caso cujo ambiente foi provisionado.
        plataforma: Plataforma do ambiente — ``CONTAINER`` nesta spec (Req. 9.3,
            17.1); VM admitida sem redesenho (Req. 17.2).
        containers_alvo: Contêineres-alvo resolvidos dos ``ParametrosDeAmbiente``
            (os mesmos que a Execução do Estágio 6 irá atacar).
        compose_file: Caminho do ``docker/docker-compose.yml`` do laboratório
            curado (Req. 9.4); ``None`` quando não aplicável.
        isolamento: :class:`IsolationResumo` com o veredito por contêiner e o
            resultado geral (Req. 9.8, 14.3). Vazio (não aprovado) quando o
            pipeline falha antes da etapa ``verificar_isolamento``.
        saude: :class:`~app.services.environment.saude.ResumoSaude` com o veredito
            de saúde por contêiner e o resultado geral da etapa ``aguardar_saude``
            (Req. 5.1). Vazio (não aprovado) quando o pipeline falha antes dessa
            etapa (p.ex. a subida dos contêineres falhou).
        abilities_carregadas: ``True`` sse as Abilities ordenadas foram carregadas
            no Caldera na ordem do DAG (Req. 5.2, 9.5).
        origem: Origem do provisionamento — ``curadoria_humana`` hoje; extensível
            a ``gerado_ia`` sem alterar o modelo (Req. 16.3, 16.4).
        etapa_falha: A etapa do pipeline que falhou — ``"subir_conteineres"``,
            ``"aguardar_saude"``, ``"carregar_abilities"`` ou
            ``"verificar_isolamento"`` — ou ``None`` quando o pipeline concluiu
            (Req. 7.1). A tela usa este campo para destacar a etapa ofensora.
        mensagem_erro: Causa legível (pt-BR) da falha da etapa, consistente com a
            última linha de erro do terminal (Req. 7.2, 7.5); ``None`` em sucesso.
    """

    caso_id: str
    plataforma: PlataformaAmbiente = PlataformaAmbiente.CONTAINER
    containers_alvo: list[str] = Field(default_factory=list)
    compose_file: Optional[str] = None
    isolamento: IsolationResumo = Field(default_factory=IsolationResumo)
    saude: ResumoSaude = Field(default_factory=ResumoSaude)
    abilities_carregadas: bool = False
    origem: TranslationSource = TranslationSource.HUMAN_CURATION
    etapa_falha: Optional[str] = None
    mensagem_erro: Optional[str] = None

    @property
    def pronto(self) -> bool:
        """``True`` sse os três pré-requisitos passaram e nenhuma etapa falhou.

        A homologação do ambiente (Req. 5.5) exige **todos** os pré-requisitos de
        execução: contêineres saudáveis (``saude.aprovado``), Abilities carregadas
        no Caldera (``abilities_carregadas``) e isolamento aprovado
        (``isolamento.aprovado``). ``etapa_falha is None`` confirma que o pipeline
        percorreu todas as etapas sem retorno antecipado. Enquanto ``pronto`` for
        ``False``, o Estágio 6 permanece bloqueado (Req. 6.4).
        """
        return (
            self.etapa_falha is None
            and self.saude.aprovado
            and self.abilities_carregadas
            and self.isolamento.aprovado
        )


class DesprovisionamentoResult(BaseModel):
    """Resultado tipado do teardown do ambiente ao final do Estágio 6 (Req. 12).

    O desprovisionamento é **sempre** invocado ao final do Estágio 6, em sucesso
    ou falha das técnicas (Req. 10.8, 12.1). Em vez de levantar exceção pelo
    caminho de falha esperado, :meth:`ProvisionadorDeAmbiente.desprovisionar`
    devolve este resultado tipado, de modo que a costura de conclusão (task 8.4)
    decida — a conclusão do Estágio 6 só ocorre quando o ambiente foi de fato
    desprovisionado (Req. 10.9). Quando o teardown falha, os **recursos
    remanescentes** são identificados para o Pesquisador (Req. 12.3).

    Attributes:
        desprovisionado: ``True`` sse o ambiente da campanha foi removido com
            sucesso (os contêineres criados foram removidos — Req. 12.2).
        recursos_remanescentes: Quando ``desprovisionado`` é ``False``, os
            recursos (contêineres/redes) que **permaneceram** provisionados, para
            o relatório identificar ao Pesquisador (Req. 12.3). Vazia em sucesso.
        origem: A :class:`~app.models.enums.TranslationSource` do provisionador
            que executou o teardown — mantém a origem coesa (Req. 12, 16.3).
        mensagem: Texto pt-BR legível do resultado (sucesso ou a causa da falha).
    """

    desprovisionado: bool = False
    recursos_remanescentes: list[str] = Field(default_factory=list)
    origem: TranslationSource = TranslationSource.HUMAN_CURATION
    mensagem: str = ""


@dataclass(frozen=True)
class ResultadoProvisionamento:
    """Resultado tipado de preparar/verificar o ambiente de um Caso (Estágio 4).

    Carrega tudo que a auto-conclusão do Estágio 4 (task 4.2) e a tela de
    Provisionamento (``EnvironmentStageView``) precisam para decidir e exibir:

    Attributes:
        caso_id: *Slug* do Caso cujo ambiente foi preparado/verificado.
        pronto: ``True`` sse o ambiente está **pronto para a Execução** — isto é,
            verificado como isolado. Para a curadoria, ``pronto == isolado`` (ela
            não cria nada; só pode declarar pronto um ambiente já isolado). Para
            uma futura IA que cria o ambiente, ``pronto`` pode englobar também o
            provisionamento efetivo, mantendo a mesma assinatura.
        isolado: ``True`` sse **todos** os contêineres-alvo estão isolados
            (``isolamento.passed``). É o sinal de contenção do Req. 5 — se
            ``False``, o Estágio 4 não conclui e o Estágio 5 fica bloqueado.
        isolamento: O :class:`~app.services.containment.IsolationResult` com os
            veredictos por contêiner (razões de falha para exibição/erro).
        topologia: A :class:`~app.services.environment.topologia_builder.TopologyResponse`
            do ambiente contido (hosts, redes internas, Mermaid), para a tela do
            Estágio 4 — exibida mesmo quando ``isolado`` é ``False`` (a razão
            acompanha os veredictos).
    """

    caso_id: str
    pronto: bool
    isolado: bool
    isolamento: IsolationResult
    topologia: TopologyResponse


@runtime_checkable
class CalderaLoader(Protocol):
    """Superfície mínima do Caldera usada pelo Estágio 5 — carregar Abilities/Adversary.

    Projeção estrutural (``@runtime_checkable``) das operações de carga do
    :class:`~app.services.caldera.CalderaClient` que o Estágio 5 reusa (Req. 9.5):
    empurrar as Abilities curadas e o Adversary para o Caldera **antes** de
    concluir. É um ``Protocol`` para que os testes injetem um *fake* (sem rede,
    sem daemon) e a produção injete o cliente real — o provisionador de curadoria
    não acopla ao transporte HTTP.

    Qualquer objeto que exponha ``load_abilities``/``load_adversary`` com estas
    assinaturas satisfaz o protocolo (duck typing estrutural), exatamente como o
    :class:`~app.services.caldera.CalderaClient` já faz.
    """

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> Any:
        """Carrega a lista de Abilities curadas no Caldera (``POST /api/v2/abilities``)."""
        ...

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        """Carrega o Adversary curado no Caldera (``POST /api/v2/adversaries``)."""
        ...


@runtime_checkable
class ProvisionadorDeAmbiente(Protocol):
    """Costura do Estágio 5 (Criação do ambiente) por **origem** — Req. 9.1, 16.3.

    Um ``Protocol`` ``@runtime_checkable`` que define o **ponto de extensão** da
    criação do ambiente, seguindo o precedente das demais costuras por origem
    (``GeradorDeDag`` no Estágio 3, ``OperacionalizadorPorOrigem`` no Estágio 4):
    cada origem (``curadoria_humana`` hoje, ``gerado_ia`` no futuro) é uma
    implementação intercambiável por injeção, **sem** redesenho (Req. 16.3, 16.4).

    Qualquer objeto que exponha estas operações satisfaz o protocolo — nenhuma
    herança explícita é exigida (duck typing estrutural):

    - ``origem()`` — a :class:`~app.models.enums.TranslationSource` que esta
      implementação carimba na Saída (``AmbienteProvisionado.origem``).
    - ``provisionar(params)`` — cria/prepara o ambiente a partir dos
      :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`
      (Saída do Estágio 4), carrega as Abilities ordenadas no Caldera e verifica
      o isolamento, devolvendo o :class:`AmbienteProvisionado` pronto e isolado
      (Req. 9.5, 9.6, 9.8).
    - ``desprovisionar(...)`` — remove o ambiente provisionado ao final do
      Estágio 6 (teardown), mantendo a origem coesa (Req. 12).

    Hoje existe a implementação de curadoria
    (:class:`CuradoriaProvisionadorDeAmbiente`), que usa o laboratório curado do
    ``docker/docker-compose.yml`` e apenas **verifica** o isolamento (read-only,
    sem rede/IA). Uma implementação de IA (``AiProvisionadorDeAmbiente``) que de
    fato **cria** contêineres/VMs/redes sob medida encaixa depois sob a mesma
    assinatura (Req. 16.3).

    .. note::
        ``preparar(caso)`` é mantido para compatibilidade com a auto-conclusão do
        Estágio 4 já existente (``EnvironmentStageService``) e os endpoints de
        topologia; os novos métodos ``origem``/``provisionar``/``desprovisionar``
        implementam a costura do Estágio 5 do pipeline de seis Estágios.
    """

    def origem(self) -> TranslationSource:
        """A :class:`~app.models.enums.TranslationSource` desta implementação.

        Carimbada em ``AmbienteProvisionado.origem`` — ``HUMAN_CURATION`` na
        curadoria; ``gerado_ia`` numa futura implementação de IA (Req. 16.3).
        """
        ...

    def provisionar(
        self,
        params: ParametrosDeAmbiente,
        *,
        on_log: Callable[[LinhaLog], None] = lambda _l: None,
    ) -> AmbienteProvisionado:
        """Cria/prepara o ambiente a partir dos ``params`` e devolve a Saída.

        Executa o provisionamento como um **pipeline ordenado e observável** de
        etapas — ``subir_conteineres`` → ``aguardar_saude`` → ``carregar_abilities``
        → ``verificar_isolamento`` — cada uma emitindo :class:`LinhaLog` por
        ``on_log`` (Req. 2). A implementação de curadoria **sobe** o laboratório
        curado antes de carregar as Abilities (Req. 9.5) e **verificar o
        isolamento** read-only (Req. 5.3); a futura IA cria o ambiente sob medida
        sob a mesma assinatura (Req. 10.2).

        O ``on_log`` é **keyword-only** com default *no-op*, de modo que os
        chamadores existentes (``provisionar(params)``) continuem válidos sem
        alteração; o serviço do Estágio 5 passa um callback que reencaminha as
        linhas ao buffer e ao terminal da tela (Req. 2.2).

        Args:
            params: A Saída do Estágio 4
                (:class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`)
                — contêineres-alvo e plataforma do ambiente a provisionar.
            on_log: Callback invocado para cada :class:`LinhaLog` emitida pelas
                etapas, na ordem em que saem; default *no-op* para compatibilidade.

        Returns:
            O :class:`AmbienteProvisionado` — pronto (``pronto is True``) quando as
            quatro etapas passam, ou com ``etapa_falha``/``mensagem_erro`` quando
            alguma falha, mantendo o Estágio 5 não concluído (Req. 7.1).
        """
        ...

    def desprovisionar(
        self,
        ambiente: AmbienteProvisionado,
        *,
        on_log: Callable[[LinhaLog], None] = lambda _l: None,
    ) -> DesprovisionamentoResult:
        """Remove o ambiente provisionado (teardown ao final do Estágio 6 — Req. 8).

        Sempre invocado após medir e gerar o relatório, em sucesso ou falha das
        técnicas (Req. 8.1). Conduzido pela **mesma** costura por origem, mantendo
        a origem coesa com o provisionamento (Req. 8.3). Devolve um
        :class:`DesprovisionamentoResult` tipado: em sucesso
        ``desprovisionado=True``; em falha ``desprovisionado=False`` com os
        ``recursos_remanescentes`` identificados para o Pesquisador (Req. 8.4). A
        implementação de curadoria **derruba** o laboratório curado via
        :meth:`ComposeExecutor.derrubar` (``docker compose down`` — Req. 8.2); uma
        futura implementação de IA remove os recursos que criou sob medida aqui,
        sob a mesma assinatura.

        O ``on_log`` é **keyword-only** com default *no-op*, de modo que os
        chamadores existentes (``desprovisionar(ambiente)``) continuem válidos sem
        alteração; a costura de conclusão do Estágio 6 passa um callback que
        reencaminha as linhas do teardown ao terminal da tela (Req. 2.2).

        Args:
            ambiente: A Saída do Estágio 5 (:class:`AmbienteProvisionado`) cujo
                ambiente deve ser desprovisionado — carrega o ``compose_file`` do
                laboratório curado e os ``containers_alvo`` da campanha.
            on_log: Callback invocado para cada :class:`LinhaLog` emitida pelo
                teardown, na ordem em que sai; default *no-op* para compatibilidade.
        """
        ...

    def preparar(self, caso: str) -> ResultadoProvisionamento:
        """Prepara/verifica o ambiente do *slug* ``caso`` (compat. Estágio 4).

        Mantido para a auto-conclusão do Estágio 4 (``EnvironmentStageService``)
        e os endpoints de topologia já existentes. A implementação de curadoria
        **não cria nada** — apenas verifica o isolamento do ambiente existente e
        expõe a topologia.
        """
        ...


class CuradoriaProvisionadorDeAmbiente:
    """Implementação de curadoria da costura :class:`ProvisionadorDeAmbiente` (Req. 5.4, 5.5, 5.7).

    Esta iteração **não provisiona dinamicamente**: o ambiente é o laboratório
    estático do ``docker/docker-compose.yml``. A curadoria apenas:

    1. **Expõe a topologia** do ambiente contido — hosts, redes ``internal: true``
       e diagrama Mermaid — montada a partir do mapa estático reaproveitado de
       :mod:`app.services.environment.topologia_builder` (a mesma fonte de verdade
       de IPs/subnets do pré-voo de contenção, **sem duplicar** o mapa).
    2. **Verifica o isolamento** dos contêineres-alvo via
       :func:`app.services.containment.inspect_isolation`, com um
       :class:`~app.services.containment.ContainerInspector` **injetável** (um
       inspector falso nos testes — sem daemon Docker; o adaptador real em
       produção quando ``None``). Verifica exatamente os contêineres que a
       Execução (Estágio 5) irá atacar (:func:`...topologia_builder.container_names`).

    **Garantia read-only / sem rede / sem IA:** nenhum caminho desta implementação
    inicia, cria ou modifica contêineres/redes, acessa a rede ou invoca IA — toda
    a inspeção Docker é read-only e fica atrás do inspector injetado (Req. 5.5).

    Satisfaz o :class:`ProvisionadorDeAmbiente` estruturalmente — expõe
    ``origem``/``provisionar``/``desprovisionar`` (costura do Estágio 5) além de
    ``preparar`` (compat. Estágio 4) — sem herança explícita, exatamente o ponto
    da costura; carimba ``curadoria_humana`` na Saída (Req. 16.3).

    _Requisitos: 5.4, 5.5, 5.7, 9.1, 9.8, 16.3_
    """

    def __init__(
        self,
        *,
        inspector: Optional[ContainerInspector] = None,
        case_service: Optional[CaseService] = None,
        caldera: Optional[CalderaLoader] = None,
        compose_executor: Optional[ComposeExecutor] = None,
        state_inspector: Optional[ContainerStateInspector] = None,
    ) -> None:
        """Monta o provisionador de curadoria com dependências injetáveis.

        Args:
            inspector: Inspector Docker **read-only** usado na verificação de
                isolamento. ``None`` em produção (o adaptador real é construído
                preguiçosamente por :func:`inspect_isolation` apenas quando a
                verificação roda); os testes injetam um inspector falso para não
                tocar em nenhum daemon.
            case_service: :class:`CaseService` para resolver o nome de exibição do
                Caso e carregar as Abilities/Adversary curados do Caso. Default:
                uma instância de produção.
            caldera: Cliente Caldera (:class:`CalderaLoader`) usado para carregar
                as Abilities ordenadas e o Adversary (Req. 9.5). ``None`` em
                produção — um :class:`~app.services.caldera.CalderaClient` real é
                construído preguiçosamente apenas quando o carregamento roda (o
                import nunca abre socket); os testes injetam um *fake* sem rede.
            compose_executor: Costura :class:`~app.services.environment.compose_executor.ComposeExecutor`
                usada para **subir**/**derrubar** o laboratório contido do
                ``docker-compose`` em streaming (Req. 3.1). ``None`` em produção —
                um :class:`~app.services.environment.compose_executor.DockerComposeExecutor`
                real é construído preguiçosamente por :meth:`_get_compose_executor`
                apenas quando o provisionamento roda (o import nunca abre
                subprocess nem toca o daemon); os testes injetam um *fake* sem
                subprocess (Req. 1.4, 10.1).
            state_inspector: Inspector read-only de estado/saúde
                (:class:`~app.services.environment.saude.ContainerStateInspector`)
                usado pela etapa ``aguardar_saude`` (Req. 5.1). ``None`` em
                produção — a inspeção de saúde é feita via
                :func:`~app.services.environment.saude.inspect_saude`, que constrói
                o adaptador real do SDK preguiçosamente apenas quando nenhum
                inspector é injetado (nunca no import); os testes injetam um *fake*
                sem daemon. Aqui é apenas **armazenado** para ser repassado à etapa
                de saúde na task 3.2.
        """
        self._inspector = inspector
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )
        self._caldera = caldera
        self._compose_executor = compose_executor
        self._state_inspector = state_inspector

    @property
    def case_service(self) -> CaseService:
        """O :class:`CaseService` usado para resolver nome/validação do Caso."""
        return self._case_service

    def _nome_do_caso(self, caso: str) -> str:
        """Nome de exibição do Caso para a topologia (fallback: o próprio slug)."""
        return self._case_service.list_cases().get(caso, caso)

    def origem(self) -> TranslationSource:
        """Carimba ``curadoria_humana`` na Saída do Estágio 5 (Req. 16.3)."""
        return TranslationSource.HUMAN_CURATION

    def _containers_alvo(self, params: ParametrosDeAmbiente) -> list[str]:
        """Resolve os contêineres-alvo a verificar a partir dos ``params``.

        Prefere os contêineres resolvidos nos ``hosts`` dos
        :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`
        (derivados dos comandos do DAG curado); recorre aos contêineres do
        laboratório curado (:func:`...topologia_builder.container_names`) quando
        os parâmetros não resolveram nenhum (Req. 9.4). Preserva a ordem e remove
        duplicatas.
        """
        resolvidos: list[str] = []
        for host in params.hosts:
            if host.container:
                # Resolve o nome logico do host (ex.: ``nginx``, ``db``) para o
                # ``container_name`` real do laboratorio (ex.: ``sticks-nginx-target``,
                # ``sticks-mariadb-target``) via o mapa estavel ``HOST_CONTAINERS``,
                # de modo que a inspecao read-only de saude/isolamento encontre o
                # container pelo nome que o Docker conhece. Nomes ja resolvidos (ou
                # sem mapeamento) sao preservados. Remove duplicatas, preserva ordem.
                nome = HOST_CONTAINERS.get(host.container, host.container)
                if nome not in resolvidos:
                    resolvidos.append(nome)
        if resolvidos:
            return resolvidos
        return container_names()

    def _get_caldera(self) -> CalderaLoader:
        """Devolve o cliente Caldera, construindo um real preguiçosamente se preciso.

        Em produção (``caldera is None``) um
        :class:`~app.services.caldera.CalderaClient` é construído apenas aqui, na
        primeira carga — nunca no import, de modo que importar este módulo jamais
        abra socket. Os testes injetam um *fake* no construtor e este caminho não
        é tocado.
        """
        if self._caldera is None:
            # Import local: mantém o módulo importável sem abrir conexão e evita
            # import circular com o pacote caldera.
            from app.services.caldera import CalderaClient

            self._caldera = CalderaClient()
        return self._caldera

    def _get_compose_executor(self) -> ComposeExecutor:
        """Devolve o executor de ``compose``, construindo um real preguiçosamente se preciso.

        Em produção (``compose_executor is None``) um
        :class:`~app.services.environment.compose_executor.DockerComposeExecutor`
        é construído apenas aqui, na primeira necessidade de subir/derrubar o
        laboratório — nunca no import, de modo que importar este módulo jamais
        abra subprocess nem toque o daemon Docker (amigável a dev sem Docker). Os
        testes injetam um *fake* no construtor e este caminho não é tocado
        (Req. 1.4, 3.1, 10.1).
        """
        if self._compose_executor is None:
            self._compose_executor = DockerComposeExecutor()
        return self._compose_executor

    @staticmethod
    def _abilities_ordenadas(case: CaseData) -> list[Any]:
        """Ordena as Abilities do Caso pela ``atomic_ordering`` do Adversary (Req. 9.5).

        A ordem do DAG é materializada pela ``atomic_ordering`` do Adversary
        curado (os ``ability_id`` na ordem de execução). Preserva essa ordem;
        Abilities fora da ``atomic_ordering`` (defensivo) vão ao final, na ordem
        original. Sem ``atomic_ordering`` cai na ordem natural do arquivo.
        """
        ordering = case.atomic_ordering
        if not ordering:
            return list(case.abilities)

        por_id = {a.ability_id: a for a in case.abilities}
        ordenadas: list[Any] = []
        vistos: set[str] = set()
        for ability_id in ordering:
            ability = por_id.get(ability_id)
            if ability is not None and ability_id not in vistos:
                ordenadas.append(ability)
                vistos.add(ability_id)
        # Abilities presentes no arquivo mas ausentes da atomic_ordering (defensivo).
        for ability in case.abilities:
            if ability.ability_id not in vistos:
                ordenadas.append(ability)
                vistos.add(ability.ability_id)
        return ordenadas

    @staticmethod
    def _ability_payloads(abilities: Sequence[Any]) -> list[Mapping[str, Any]]:
        """Monta os payloads ``POST /api/v2/abilities`` a partir das Abilities.

        Envia os campos que o schema v2 de Ability do Caldera usa; campos
        ausentes são omitidos (o Caldera preenche seus próprios defaults). Reusa a
        mesma projeção do ``OperationRunner`` para manter a carga coesa.
        """
        payloads: list[Mapping[str, Any]] = []
        for ability in abilities:
            payload: dict[str, Any] = {}
            ability_id = getattr(ability, "ability_id", None)
            if ability_id:
                payload["ability_id"] = ability_id
            for attr in ("name", "tactic", "technique_name", "technique_id", "description"):
                value = getattr(ability, attr, None)
                if value is not None:
                    payload[attr] = value
            executors = getattr(ability, "executors", None)
            if executors:
                payload["executors"] = [
                    e.model_dump() if hasattr(e, "model_dump") else e
                    for e in executors
                ]
            payloads.append(payload)
        return payloads

    @staticmethod
    def _adversary_payload(case: CaseData) -> Optional[Mapping[str, Any]]:
        """Monta o payload ``POST /api/v2/adversaries`` (``adversary_id`` v2).

        Sem Adversary curado, devolve ``None`` — o Caldera não recebe grupo e a
        carga de Abilities ainda ocorre.
        """
        adversary = case.adversary
        if adversary is None:
            return None
        return {
            "adversary_id": adversary.id,
            "name": adversary.name or adversary.id,
            "description": adversary.description or "",
            "atomic_ordering": list(adversary.atomic_ordering),
        }

    def _carregar_abilities(self, params: ParametrosDeAmbiente) -> bool:
        """Carrega as Abilities ordenadas + o Adversary curado no Caldera (Req. 9.5).

        Lê o Caso curado pelo *slug* (``CaseService.try_load_case``, defensivo e
        sem rede), ordena as Abilities pela ``atomic_ordering`` do DAG, e as
        empurra para o Caldera na ordem, seguidas do Adversary. Fail-closed: se o
        Caso não carrega, não há Abilities, ou o Caldera falha, devolve ``False``
        (o Estágio 5 não conclui) — nunca derruba o provisionamento (a topologia
        e o veredito de isolamento ainda são exibidos).

        Não invoca IA em nenhum caminho (Req. 9.4). O único egresso é a carga no
        Caldera do laboratório (não é a Internet pública).
        """
        try:
            resultado = self._case_service.try_load_case(params.caso_id)
        except KeyError:
            # Slug fora do catálogo curado: fail-closed, sem Abilities carregadas.
            return False
        if not resultado.ok or resultado.case is None:
            return False
        case = resultado.case
        if not case.has_abilities:
            return False

        abilities = self._abilities_ordenadas(case)
        payloads = self._ability_payloads(abilities)
        if not payloads:
            return False

        try:
            caldera = self._get_caldera()
            caldera.load_abilities(payloads)
            adversary_payload = self._adversary_payload(case)
            if adversary_payload is not None:
                caldera.load_adversary(adversary_payload)
        except Exception:
            # Caldera indisponível/erro de API: fail-closed, o Estágio 5 não
            # conclui, mas o provisionamento ainda devolve a topologia/isolamento.
            return False
        return True

    def provisionar(
        self,
        params: ParametrosDeAmbiente,
        *,
        on_log: Callable[[LinhaLog], None] = lambda _l: None,
    ) -> AmbienteProvisionado:
        """Provisiona o ambiente curado como um **pipeline de etapas** em streaming.

        Nesta iteração de curadoria o ambiente é o laboratório do
        ``docker/docker-compose.yml`` (apenas contêineres, três redes internas —
        Req. 3.2, 3.3). O provisionamento vira uma sequência **ordenada e
        observável** de etapas, cada uma emitindo :class:`LinhaLog` por ``on_log``
        (Req. 2.2, 2.4) e, ao falhar, **retornando cedo** com ``etapa_falha`` + a
        causa — o Estágio 5 não conclui e o Estágio 6 fica bloqueado (Req. 7.1):

        1. ``subir_conteineres`` — sobe o laboratório via
           :meth:`_get_compose_executor` (``docker compose up -d --wait``,
           streaming — Req. 3.1). ``codigo != 0`` ⇒ falha com a causa da última
           linha de ``stderr`` (Req. 3.5).
        2. ``aguardar_saude`` — confirma que **todos** os contêineres-alvo estão
           ``running`` (e ``healthy`` quando há ``healthcheck``) por inspeção
           read-only (:func:`~app.services.environment.saude.inspect_saude`,
           Req. 5.1). Reprovado ⇒ falha identificando o contêiner não saudável e a
           razão.
        3. ``carregar_abilities`` — reusa :meth:`_carregar_abilities` (Abilities
           ordenadas pela ``atomic_ordering`` do DAG + Adversary no Caldera —
           Req. 4.1). ``False`` ⇒ falha (Caldera indisponível / artefato ausente —
           Req. 4.4).
        4. ``verificar_isolamento`` — reusa :meth:`_verificar_isolamento`
           (:func:`inspect_isolation` read-only — Req. 5.3). Reprovado (ou daemon
           indisponível, fail-closed) ⇒ falha identificando o contêiner não isolado
           e a razão (Req. 7.4).

        Quando as quatro etapas passam, monta a Saída tipada completa com o resumo
        de isolamento, o :class:`~app.services.environment.saude.ResumoSaude`,
        ``abilities_carregadas=True``, os contêineres-alvo, o ``compose_file`` e a
        origem ``curadoria_humana`` (Req. 5.5, 6.1); :attr:`AmbienteProvisionado.pronto`
        fica ``True``.

        A verificação de saúde e de isolamento são **somente leitura** — nenhuma
        das duas inicia/para/modifica contêineres ou redes (Req. 9.2); apenas a
        etapa ``subir_conteineres`` cria recursos. Não acessa a rede pública e não
        invoca IA em nenhum caminho (Req. 1.5).

        Args:
            params: A Saída do Estágio 4 — contêineres-alvo e plataforma.
            on_log: Callback para cada :class:`LinhaLog` emitida pelas etapas, em
                ordem; default *no-op* para compatibilidade com os chamadores
                existentes (Req. 2.2).

        Returns:
            O :class:`AmbienteProvisionado` — pronto quando as quatro etapas passam,
            ou com ``etapa_falha``/``mensagem_erro`` da etapa que falhou.
        """
        containers = self._containers_alvo(params)

        def _saida(
            *,
            isolamento: Optional[IsolationResult] = None,
            saude: Optional[ResumoSaude] = None,
            abilities_carregadas: bool = False,
            etapa_falha: Optional[str] = None,
            mensagem_erro: Optional[str] = None,
        ) -> AmbienteProvisionado:
            """Monta a Saída tipada, preenchendo os resumos ainda não computados.

            As etapas que ainda não rodaram (porque uma anterior falhou) ficam com
            os resumos vazios (não aprovados), de modo que a tela sempre receba a
            forma completa do :class:`AmbienteProvisionado`.
            """
            return AmbienteProvisionado(
                caso_id=params.caso_id,
                plataforma=params.plataforma,
                containers_alvo=containers,
                compose_file=compose_path(),
                isolamento=(
                    IsolationResumo.de_isolation_result(isolamento)
                    if isolamento is not None
                    else IsolationResumo()
                ),
                saude=saude if saude is not None else ResumoSaude(),
                abilities_carregadas=abilities_carregadas,
                origem=self.origem(),
                etapa_falha=etapa_falha,
                mensagem_erro=mensagem_erro,
            )

        # --- Etapa 1: subir_conteineres (docker compose up -d --wait) — Req. 3.1.
        resultado_subida = self._get_compose_executor().subir(
            compose_path(), on_log=on_log
        )
        if resultado_subida.codigo != 0:
            causa = self._causa_da_subida(resultado_subida)
            return _saida(etapa_falha="subir_conteineres", mensagem_erro=causa)

        # --- Etapa 2: aguardar_saude (running + healthy, read-only) — Req. 5.1.
        saude = self._verificar_saude(containers, on_log=on_log)
        if not saude.aprovado:
            causa = self._causa_da_saude(saude)
            return _saida(
                saude=saude, etapa_falha="aguardar_saude", mensagem_erro=causa
            )

        # --- Etapa 3: carregar_abilities (Caldera, ordem do DAG) — Req. 4.1.
        on_log(
            LinhaLog(
                etapa="carregar_abilities",
                fluxo="info",
                texto="Carregando as Abilities ordenadas e o Adversary no Caldera",
            )
        )
        abilities_carregadas = self._carregar_abilities(params)
        if not abilities_carregadas:
            causa = (
                "falha ao carregar as Abilities no Caldera "
                "(Caldera indisponível, erro de API ou artefato ausente)"
            )
            on_log(
                LinhaLog(etapa="carregar_abilities", fluxo="stderr", texto=causa)
            )
            return _saida(
                saude=saude,
                etapa_falha="carregar_abilities",
                mensagem_erro=causa,
            )
        on_log(
            LinhaLog(
                etapa="carregar_abilities",
                fluxo="info",
                texto="Abilities e Adversary carregados no Caldera",
            )
        )

        # --- Etapa 4: verificar_isolamento (inspect_isolation, read-only) — Req. 5.3.
        on_log(
            LinhaLog(
                etapa="verificar_isolamento",
                fluxo="info",
                texto="Verificando o isolamento dos contêineres-alvo (read-only)",
            )
        )
        isolamento = self._verificar_isolamento(containers)
        if not isolamento.passed:
            causa = self._causa_do_isolamento(isolamento)
            on_log(
                LinhaLog(
                    etapa="verificar_isolamento", fluxo="stderr", texto=causa
                )
            )
            return _saida(
                isolamento=isolamento,
                saude=saude,
                abilities_carregadas=abilities_carregadas,
                etapa_falha="verificar_isolamento",
                mensagem_erro=causa,
            )
        on_log(
            LinhaLog(
                etapa="verificar_isolamento",
                fluxo="info",
                texto="Isolamento aprovado para todos os contêineres-alvo",
            )
        )

        # --- Conclusão: os três pré-requisitos passaram (Req. 5.5, 6.1).
        return _saida(
            isolamento=isolamento,
            saude=saude,
            abilities_carregadas=abilities_carregadas,
        )

    @staticmethod
    def _causa_da_subida(resultado: ResultadoComando) -> str:
        """Deriva a causa legível da falha ao subir, da última linha de erro (Req. 3.5, 7.2).

        Procura a última linha de ``stderr`` emitida pelo ``up`` (consistência
        tela ↔ console — Req. 7.5); na sua ausência (todas em ``stdout``) usa a
        última linha de saída; sem nenhuma linha, reporta o código de saída.
        """
        stderr = [l.texto for l in resultado.linhas if l.fluxo == "stderr"]
        if stderr:
            ultima = stderr[-1]
        else:
            saida = [l.texto for l in resultado.linhas if l.fluxo in ("stdout", "stderr")]
            ultima = saida[-1] if saida else ""
        prefixo = (
            f"falha ao subir os contêineres (código {resultado.codigo})"
        )
        return f"{prefixo}: {ultima}" if ultima else prefixo

    def _verificar_saude(
        self, containers: list[str], *, on_log: Callable[[LinhaLog], None]
    ) -> ResumoSaude:
        """Confirma a saúde dos contêineres-alvo (read-only) emitindo linhas (Req. 5.1).

        Reusa :func:`~app.services.environment.saude.inspect_saude` com o
        ``state_inspector`` injetado (um *fake* nos testes; o adaptador real do SDK
        construído preguiçosamente em produção quando ``None``). É **somente
        leitura** — nunca inicia/para/modifica contêineres (Req. 9.2). Emite uma
        linha ``info`` de início e uma linha por contêiner com o veredito, de modo
        que o terminal da tela reflita o resultado (Req. 2.4).
        """
        on_log(
            LinhaLog(
                etapa="aguardar_saude",
                fluxo="info",
                texto="Confirmando a saúde dos contêineres-alvo (running + healthy)",
            )
        )
        saude = inspect_saude(containers, self._state_inspector)
        for veredito in saude.veredictos:
            if veredito.aprovado:
                texto = f"{veredito.container}: saudável"
                fluxo = "stdout"
            else:
                texto = f"{veredito.container}: {veredito.razao or 'não saudável'}"
                fluxo = "stderr"
            on_log(LinhaLog(etapa="aguardar_saude", fluxo=fluxo, texto=texto))
        return saude

    @staticmethod
    def _causa_da_saude(saude: ResumoSaude) -> str:
        """Deriva a causa legível da reprovação de saúde (Req. 5.1, 7.2).

        Identifica o(s) contêiner(es) não saudável(is) e a razão, consistente com
        as linhas de ``stderr`` emitidas na etapa (Req. 7.5). Fail-closed: uma
        lista vazia de contêineres reprova sem veredictos — reporta isso.
        """
        nao_saudaveis = saude.nao_saudaveis
        if not nao_saudaveis:
            return (
                "nenhum contêiner-alvo a verificar; a saúde reprova "
                "fail-closed (lista vazia)"
            )
        partes = [
            f"{v.container} ({v.razao or 'não saudável'})" for v in nao_saudaveis
        ]
        return "contêineres não saudáveis: " + "; ".join(partes)

    @staticmethod
    def _causa_do_isolamento(isolamento: IsolationResult) -> str:
        """Deriva a causa legível da reprovação de isolamento (Req. 5.3, 7.4).

        Identifica o(s) contêiner(es) não isolado(s) e a(s) razão(ões), para a
        mensagem de falha da tela e a linha de ``stderr`` do console (Req. 7.4,
        7.5). Fail-closed: sem veredictos (lista vazia) reprova — reporta isso.
        """
        nao_isolados = [v for v in isolamento.verdicts if not v.isolated]
        if not nao_isolados:
            return (
                "nenhum contêiner-alvo a verificar; o isolamento reprova "
                "fail-closed (lista vazia)"
            )
        partes = [
            f"{v.container} ({'; '.join(v.reasons) or 'não isolado'})"
            for v in nao_isolados
        ]
        return "contêineres não isolados: " + "; ".join(partes)

    def _verificar_isolamento(self, containers: list[str]) -> IsolationResult:
        """Verifica o isolamento (read-only) com fail-closed completo (Req. 9.6, 14).

        Reusa :func:`inspect_isolation` (fonte única da inspeção read-only). A
        própria função já reprova lista vazia (``passed=False``); aqui, além
        disso, o **daemon Docker indisponível** é capturado: em produção
        (``inspector is None``) :func:`inspect_isolation` constrói o adaptador
        real, que levanta ``RuntimeError`` quando o daemon não responde — tratamos
        isso como reprovação (fail-closed, Req. 9.7, 14.4) em vez de deixar a
        exceção derrubar o provisionamento.
        """
        try:
            return inspect_isolation(containers, self._inspector)
        except RuntimeError:
            # Daemon Docker indisponível: fail-closed. Um veredito reprovado por
            # container-alvo, com a razão, mantém o Estágio 5 não concluído.
            from app.services.containment import ContainerIsolationVerdict

            motivo = "Docker Engine API indisponível; isolamento não verificável"
            verdicts = tuple(
                ContainerIsolationVerdict(
                    container=c, isolated=False, reasons=(motivo,)
                )
                for c in containers
            )
            return IsolationResult(passed=False, verdicts=verdicts)

    def desprovisionar(
        self,
        ambiente: AmbienteProvisionado,
        *,
        on_log: Callable[[LinhaLog], None] = lambda _l: None,
    ) -> DesprovisionamentoResult:
        """Teardown do ambiente da campanha — ``compose down`` de fato (Req. 8).

        Ao final do Estágio 6 o laboratório que foi **subido** para a campanha é
        de fato derrubado: a curadoria chama :meth:`ComposeExecutor.derrubar`
        (``docker compose -f <compose> down``) para parar/remover os contêineres
        do laboratório e liberar os recursos de hardware (Req. 8.1, 8.2). O
        teardown é conduzido pela **mesma** costura do provisionamento, mantendo a
        origem coesa (``curadoria_humana`` — Req. 8.3).

        O arquivo de ``compose`` vem do próprio ``ambiente`` provisionado
        (``ambiente.compose_file``), recorrendo a :func:`compose_path` quando a
        Saída não o carrega. Cada linha de saída do ``down`` é empurrada por
        ``on_log`` em tempo quase real, de modo que o terminal da tela reflita o
        teardown (Req. 2.2).

        Mapeia o :class:`ResultadoComando` do executor para o
        :class:`DesprovisionamentoResult` tipado:

        - ``codigo == 0`` ⇒ ``desprovisionado=True`` sem recursos remanescentes —
          o ambiente foi removido com sucesso (Req. 8.1).
        - ``codigo != 0`` ⇒ ``desprovisionado=False`` com os
          ``recursos_remanescentes`` (os ``containers_alvo`` da campanha que
          permaneceram provisionados) e uma ``mensagem`` pt-BR derivada da última
          linha de ``stderr`` do ``down``, para o relatório identificar ao
          Pesquisador o que ficou de pé (Req. 8.4).

        Args:
            ambiente: A Saída do Estágio 5 cujo ambiente deve ser desprovisionado
                — carrega o ``compose_file`` e os ``containers_alvo`` da campanha.
            on_log: Callback para cada :class:`LinhaLog` do teardown, em ordem;
                default *no-op* para compatibilidade.

        Returns:
            O :class:`DesprovisionamentoResult` — ``desprovisionado=True`` em
            sucesso, ou ``desprovisionado=False`` com os ``recursos_remanescentes``
            e a causa quando o ``down`` falha.
        """
        compose_file = ambiente.compose_file or compose_path()
        resultado = self._get_compose_executor().derrubar(
            compose_file, on_log=on_log
        )
        if resultado.codigo == 0:
            return DesprovisionamentoResult(
                desprovisionado=True,
                recursos_remanescentes=[],
                origem=self.origem(),
                mensagem="Ambiente desprovisionado: contêineres do laboratório removidos.",
            )
        causa = self._causa_da_derrubada(resultado)
        return DesprovisionamentoResult(
            desprovisionado=False,
            recursos_remanescentes=list(ambiente.containers_alvo),
            origem=self.origem(),
            mensagem=causa,
        )

    @staticmethod
    def _causa_da_derrubada(resultado: ResultadoComando) -> str:
        """Deriva a causa legível da falha ao derrubar, da última linha de erro (Req. 8.4).

        Espelha :meth:`_causa_da_subida`: procura a última linha de ``stderr``
        emitida pelo ``down`` (consistência tela ↔ console — Req. 7.5); na sua
        ausência (todas em ``stdout``) usa a última linha de saída; sem nenhuma
        linha, reporta apenas o código de saída.
        """
        stderr = [l.texto for l in resultado.linhas if l.fluxo == "stderr"]
        if stderr:
            ultima = stderr[-1]
        else:
            saida = [
                l.texto for l in resultado.linhas if l.fluxo in ("stdout", "stderr")
            ]
            ultima = saida[-1] if saida else ""
        prefixo = f"falha ao desprovisionar os contêineres (código {resultado.codigo})"
        return f"{prefixo}: {ultima}" if ultima else prefixo

    def preparar(self, caso: str) -> ResultadoProvisionamento:
        """Verifica o isolamento do ambiente contido e expõe sua topologia (read-only).

        Monta a topologia estática (hosts/redes/Mermaid) e verifica o isolamento
        dos contêineres-alvo (os mesmos que a Execução atacará) via
        :func:`inspect_isolation` com o inspector injetado. **Não cria/inicia
        nada.** O ambiente é declarado ``pronto`` sse ``isolado`` — a curadoria
        não provisiona, então só pode aprovar um ambiente já isolado (Req. 5.4,
        5.6); se não estiver isolado, ``isolado=False`` e a topologia ainda é
        devolvida com os veredictos (a tela exibe a razão e o Estágio 5 fica
        bloqueado).

        Não acessa a rede e não invoca IA em nenhum caminho (Req. 5.5, 5.7).
        """
        hosts = construir_hosts()
        redes = construir_redes()

        containers = container_names()
        isolamento = inspect_isolation(containers, self._inspector)

        topologia = TopologyResponse(
            caso_id=caso,
            nome=self._nome_do_caso(caso),
            isolado=isolamento.passed,
            hosts=hosts,
            redes=redes,
            mermaid=montar_mermaid(hosts, redes),
            compose_file=compose_path(),
        )

        return ResultadoProvisionamento(
            caso_id=caso,
            pronto=isolamento.passed,
            isolado=isolamento.passed,
            isolamento=isolamento,
            topologia=topologia,
        )


__all__ = [
    # DTOs tipados da Saída do Estágio 5 (task 7.1)
    "VereditoContainer",
    "IsolationResumo",
    "AmbienteProvisionado",
    # resultado do teardown ao final do Estágio 6 (task 8.4)
    "DesprovisionamentoResult",
    # costura + resultado de compat. (Estágio 4) + curadoria
    "ResultadoProvisionamento",
    "CalderaLoader",
    "ProvisionadorDeAmbiente",
    "CuradoriaProvisionadorDeAmbiente",
]
