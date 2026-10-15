import { describe, expect, it } from "vitest"
import { within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { PrereqChecklist, derivarEstados } from "@/components/PrereqChecklist"
import type {
  IsolationResumo,
  ResumoSaude,
  VereditoContainer,
  VereditoSaude,
} from "@/lib/estagios"
import type { EstadoEstagio } from "@/lib/casos"

/**
 * PrereqChecklist — testes de confirmação de pré-requisitos do Estágio 5
 * (task 8.3). Cobre:
 *
 * 1. Três estados por item (`ok`/`falha`/`pendente`) — Req. 5.6.
 * 2. Mensagem de falha, nomeando o contêiner ofensor + razão — Req. 7.2 (e a
 *    identificação do recurso, Req. 7.4).
 * 3. Comunicação de bloqueio/liberação do Estágio 6 ("Próximo") — Req. 7.3.
 *
 * Os asserts são sobre os `defaultValue` pt-BR do componente (as chaves i18n
 * `stage5.provisionamento.*` ainda não existem no namespace, então o fallback
 * resolve de forma estável).
 */

// ── Fábricas de dados ────────────────────────────────────────────────────────

function saudeVazia(): ResumoSaude {
  return { aprovado: false, veredictos: [] }
}

function saudeAprovada(): ResumoSaude {
  return {
    aprovado: true,
    veredictos: [
      { container: "nginx-target", rodando: true, saudavel: true, razao: null },
      { container: "mariadb-target", rodando: true, saudavel: true, razao: null },
    ],
  }
}

function saudeReprovada(veredictos: VereditoSaude[]): ResumoSaude {
  return { aprovado: false, veredictos }
}

function isolamentoVazio(): IsolationResumo {
  return { aprovado: false, veredictos: [] }
}

function isolamentoAprovado(): IsolationResumo {
  return {
    aprovado: true,
    veredictos: [
      {
        container: "nginx-target",
        isolado: true,
        razoes: [],
        redes_externas: [],
        dns_externo: [],
      },
    ],
  }
}

function isolamentoReprovado(veredictos: VereditoContainer[]): IsolationResumo {
  return { aprovado: false, veredictos }
}

/** Props mínimas, sobrescritas por cenário. */
function props(overrides: Partial<Parameters<typeof PrereqChecklist>[0]> = {}) {
  return {
    estado: "nao_iniciado" as EstadoEstagio,
    saude: saudeVazia(),
    isolamento: isolamentoVazio(),
    abilitiesCarregadas: false,
    ...overrides,
  }
}

/** Localiza o `<li>` de um item pelo rótulo (defaultValue pt-BR) e devolve o
 * texto do badge de status (último `<span>` do cabeçalho). */
function statusDoItem(rotulo: RegExp): string {
  const label = document.body
  const itens = Array.from(label.querySelectorAll("li"))
  const li = itens.find((el) => rotulo.test(el.textContent ?? ""))
  if (!li) throw new Error(`item não encontrado: ${rotulo}`)
  const badges = li.querySelectorAll("span")
  return badges[badges.length - 1]?.textContent ?? ""
}

// ── 1. derivarEstados — função pura (mapeamento dos três estados, Req. 5.6) ──

describe("derivarEstados (mapeamento puro dos três estados — Req. 5.6)", () => {
  it("concluido → os três itens ok", () => {
    expect(
      derivarEstados({
        estado: "concluido",
        saude: saudeAprovada(),
        isolamento: isolamentoAprovado(),
        abilitiesCarregadas: true,
      }),
    ).toEqual({
      aguardar_saude: "ok",
      carregar_abilities: "ok",
      verificar_isolamento: "ok",
    })
  })

  it("falha no meio da pipeline (carregar_abilities) → saúde ok, abilities falha, isolamento pendente", () => {
    expect(
      derivarEstados({
        estado: "erro",
        saude: saudeAprovada(),
        isolamento: isolamentoVazio(),
        abilitiesCarregadas: false,
        etapaFalha: "carregar_abilities",
      }),
    ).toEqual({
      aguardar_saude: "ok",
      carregar_abilities: "falha",
      verificar_isolamento: "pendente",
    })
  })

  it("falha ao aguardar saúde → contêineres falha, posteriores pendentes", () => {
    expect(
      derivarEstados({
        estado: "erro",
        saude: saudeReprovada([
          { container: "nginx-target", rodando: false, saudavel: null, razao: "parado" },
        ]),
        isolamento: isolamentoVazio(),
        abilitiesCarregadas: false,
        etapaFalha: "aguardar_saude",
      }),
    ).toEqual({
      aguardar_saude: "falha",
      carregar_abilities: "pendente",
      verificar_isolamento: "pendente",
    })
  })

  it("subir_conteineres falha antes da saúde → reprova o item de contêineres saudáveis", () => {
    expect(
      derivarEstados({
        estado: "erro",
        saude: saudeVazia(),
        isolamento: isolamentoVazio(),
        abilitiesCarregadas: false,
        etapaFalha: "subir_conteineres",
      }).aguardar_saude,
    ).toBe("falha")
  })

  it("nada rodou (nao_iniciado, resumos vazios, abilities false) → os três pendentes", () => {
    expect(
      derivarEstados({
        estado: "nao_iniciado",
        saude: saudeVazia(),
        isolamento: isolamentoVazio(),
        abilitiesCarregadas: false,
      }),
    ).toEqual({
      aguardar_saude: "pendente",
      carregar_abilities: "pendente",
      verificar_isolamento: "pendente",
    })
  })
})

// ── 2. Três estados por item renderizados no DOM (Req. 5.6) ──────────────────

describe("PrereqChecklist — três estados por item renderizados (Req. 5.6)", () => {
  it("concluido: os três itens 'OK' e a confirmação de ambiente pronto", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "concluido",
          saude: saudeAprovada(),
          isolamento: isolamentoAprovado(),
          abilitiesCarregadas: true,
        })}
      />,
    )

    expect(statusDoItem(/Contêineres saudáveis/i)).toBe("OK")
    expect(statusDoItem(/Abilities carregadas/i)).toBe("OK")
    expect(statusDoItem(/Isolamento aprovado/i)).toBe("OK")

    const confirmacao = document.querySelector('[role="status"]')
    expect(confirmacao).not.toBeNull()
    expect(confirmacao).toHaveTextContent(/Ambiente pronto/i)
  })

  it("falha no meio (carregar_abilities): contêineres OK, abilities Falha, isolamento Pendente", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "erro",
          saude: saudeAprovada(),
          isolamento: isolamentoVazio(),
          abilitiesCarregadas: false,
          etapaFalha: "carregar_abilities",
          mensagemErro: "Falha ao carregar Abilities no Caldera",
        })}
      />,
    )

    expect(statusDoItem(/Contêineres saudáveis/i)).toBe("OK")
    expect(statusDoItem(/Abilities carregadas/i)).toBe("Falha")
    expect(statusDoItem(/Isolamento aprovado/i)).toBe("Pendente")
  })

  it("nao_iniciado com resumos vazios + abilities false: os três 'Pendente'", () => {
    renderWithI18n(<PrereqChecklist {...props({ estado: "nao_iniciado" })} />)

    expect(statusDoItem(/Contêineres saudáveis/i)).toBe("Pendente")
    expect(statusDoItem(/Abilities carregadas/i)).toBe("Pendente")
    expect(statusDoItem(/Isolamento aprovado/i)).toBe("Pendente")
  })
})

// ── 3. Mensagem de falha, nomeando o recurso ofensor (Req. 7.2 / 7.4) ────────

describe("PrereqChecklist — mensagem de falha (Req. 7.2, 7.4)", () => {
  it("falha de saúde: nomeia o contêiner ofensor + razão e exibe a mensagemErro no alerta", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "erro",
          saude: saudeReprovada([
            {
              container: "mariadb-target",
              rodando: false,
              saudavel: false,
              razao: "contêiner parado",
            },
          ]),
          isolamento: isolamentoVazio(),
          abilitiesCarregadas: false,
          etapaFalha: "aguardar_saude",
          mensagemErro: "Verificação de saúde reprovou",
        })}
      />,
    )

    // Nome do contêiner + razão aparecem (identificação do recurso, Req. 7.4).
    expect(document.body).toHaveTextContent(/mariadb-target/i)
    expect(document.body).toHaveTextContent(/contêiner parado/i)

    // mensagemErro exibida no alerta de falha.
    const alerta = document.querySelector('[role="alert"]')
    expect(alerta).not.toBeNull()
    expect(alerta).toHaveTextContent(/Verificação de saúde reprovou/i)
    expect(within(alerta as HTMLElement).getByText(/permanece bloqueado/i)).toBeInTheDocument()
  })

  it("falha de isolamento: nomeia o contêiner não isolado + razão (Req. 7.4)", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "erro",
          saude: saudeAprovada(),
          isolamento: isolamentoReprovado([
            {
              container: "kali-attacker",
              isolado: false,
              razoes: ["rede externa detectada: bridge"],
              redes_externas: ["bridge"],
              dns_externo: [],
            },
          ]),
          abilitiesCarregadas: true,
          etapaFalha: "verificar_isolamento",
          mensagemErro: "Isolamento reprovado",
        })}
      />,
    )

    expect(document.body).toHaveTextContent(/kali-attacker/i)
    expect(document.body).toHaveTextContent(/rede externa detectada: bridge/i)

    const alerta = document.querySelector('[role="alert"]')
    expect(alerta).not.toBeNull()
    expect(alerta).toHaveTextContent(/Isolamento reprovado/i)
  })
})

// ── 4. Bloqueio/liberação do "Próximo" — comunicação do checklist (Req. 7.3) ──

describe("PrereqChecklist — bloqueio/liberação do Estágio 6 (Req. 7.3)", () => {
  it("em falha: mostra o indicador de Estágio 6 bloqueado e NÃO a confirmação de pronto", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "erro",
          saude: saudeAprovada(),
          isolamento: isolamentoVazio(),
          abilitiesCarregadas: false,
          etapaFalha: "carregar_abilities",
          mensagemErro: "Falha ao carregar Abilities",
        })}
      />,
    )

    expect(document.body).toHaveTextContent(/permanece bloqueado/i)
    expect(document.body).not.toHaveTextContent(/Ambiente pronto/i)
    expect(document.querySelector('[role="status"]')).toBeNull()
  })

  it("pendente/em andamento: comunica bloqueio sem alerta de falha", () => {
    renderWithI18n(
      <PrereqChecklist {...props({ estado: "em_andamento" })} />,
    )

    expect(document.body).toHaveTextContent(/permanece bloqueado/i)
    expect(document.querySelector('[role="alert"]')).toBeNull()
    expect(document.querySelector('[role="status"]')).toBeNull()
  })

  it("concluido: libera (confirmação de pronto) e NÃO mostra o indicador de bloqueado", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "concluido",
          saude: saudeAprovada(),
          isolamento: isolamentoAprovado(),
          abilitiesCarregadas: true,
        })}
      />,
    )

    expect(document.body).toHaveTextContent(/Ambiente pronto/i)
    expect(document.body).not.toHaveTextContent(/permanece bloqueado/i)
    expect(document.querySelector('[role="status"]')).not.toBeNull()
  })

  it("respeita estagio6Bloqueado=false explícito (sem alerta/sem bloqueio) quando não há falha", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          estado: "em_andamento",
          abilitiesCarregadas: true,
          estagio6Bloqueado: false,
        })}
      />,
    )

    expect(document.body).not.toHaveTextContent(/permanece bloqueado/i)
    expect(document.querySelector('[role="alert"]')).toBeNull()
  })
})
