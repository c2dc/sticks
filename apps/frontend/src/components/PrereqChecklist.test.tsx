import { describe, expect, it } from "vitest"
import { within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { PrereqChecklist, derivePrereqStates } from "@/components/PrereqChecklist"
import type {
  IsolationSummary,
  HealthSummary,
  ContainerVerdict,
  HealthVerdict,
  SubStageSummary,
  EgressVerdictView,
} from "@/lib/stages"
import type { StageState } from "@/lib/campaigns"

/**
 * PrereqChecklist — Stage 5 prerequisites-confirmation tests (task 8.3).
 * Covers:
 *
 * 1. Three states per item (`ok`/`falha`/`pendente` values) — Req. 5.6.
 * 2. Failure message, naming the offending container + reason — Req. 7.2 (and
 *    the resource identification, Req. 7.4).
 * 3. Communicating Stage 6 blocking/unblocking ("Next") — Req. 7.3.
 *
 * The asserts are on the component's pt-BR `defaultValue`s (the i18n keys
 * `stage5.provisionamento.*` do not exist in the namespace yet, so the fallback
 * resolves stably).
 */

// ── Data factories ───────────────────────────────────────────────────────────

function emptyHealth(): HealthSummary {
  return { approved: false, verdicts: [] }
}

function approvedHealth(): HealthSummary {
  return {
    approved: true,
    verdicts: [
      { container: "nginx-target", running: true, healthy: true, reason: null },
      { container: "mariadb-target", running: true, healthy: true, reason: null },
    ],
  }
}

function failedHealth(verdicts: HealthVerdict[]): HealthSummary {
  return { approved: false, verdicts }
}

function emptyIsolation(): IsolationSummary {
  return { approved: false, verdicts: [] }
}

function approvedIsolation(): IsolationSummary {
  return {
    approved: true,
    verdicts: [
      {
        container: "nginx-target",
        isolated: true,
        reasons: [],
        external_networks: [],
        external_dns: [],
      },
    ],
  }
}

function failedIsolation(verdicts: ContainerVerdict[]): IsolationSummary {
  return { approved: false, verdicts }
}

/** Fully-satisfied sub-stage summary (5.1 + 5.2 verified). */
function satisfiedSubstages(): SubStageSummary {
  return {
    build_online_ok: true,
    egress_closed: true,
    egress_lost_verified: true,
    offline_applied: true,
    egress_verdicts: [
      {
        container: "nginx-target",
        egress_lost: true,
        conclusive: true,
        reason: "egresso perdido",
      },
    ],
  }
}

/** Sub-stage summary with the egress verification reproving a given host. */
function egressFailedSubstages(verdicts: EgressVerdictView[]): SubStageSummary {
  return {
    build_online_ok: true,
    egress_closed: true,
    egress_lost_verified: false,
    offline_applied: false,
    egress_verdicts: verdicts,
  }
}

/** Minimal props, overridden per scenario. */
function props(overrides: Partial<Parameters<typeof PrereqChecklist>[0]> = {}) {
  return {
    state: "nao_iniciado" as StageState,
    health: emptyHealth(),
    isolation: emptyIsolation(),
    abilitiesLoaded: false,
    ...overrides,
  }
}

/** Locates an item's `<li>` by its label (pt-BR defaultValue) and returns the
 * text of the status badge (last `<span>` of the header). */
function itemStatus(pattern: RegExp): string {
  const root = document.body
  const items = Array.from(root.querySelectorAll("li"))
  const li = items.find((el) => pattern.test(el.textContent ?? ""))
  if (!li) throw new Error(`item not found: ${pattern}`)
  const badges = li.querySelectorAll("span")
  return badges[badges.length - 1]?.textContent ?? ""
}

// ── 1. derivePrereqStates — pure function (three-state mapping, Req. 5.6) ──

describe("derivePrereqStates (pure three-state mapping — Req. 5.6)", () => {
  it("concluido → the five items ok", () => {
    expect(
      derivePrereqStates({
        state: "concluido",
        health: approvedHealth(),
        isolation: approvedIsolation(),
        abilitiesLoaded: true,
        substages: satisfiedSubstages(),
      }),
    ).toEqual({
      aguardar_saude: "ok",
      build_online: "ok",
      verificar_egresso: "ok",
      carregar_abilities: "ok",
      verificar_isolamento: "ok",
    })
  })

  it("mid-pipeline failure (carregar_abilities) → earlier ok, abilities fail, isolation pending", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        substages: satisfiedSubstages(),
        failedStep: "carregar_abilities",
      }),
    ).toEqual({
      aguardar_saude: "ok",
      build_online: "ok",
      verificar_egresso: "ok",
      carregar_abilities: "falha",
      verificar_isolamento: "pendente",
    })
  })

  it("failure while awaiting health → containers fail, later ones pending", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: failedHealth([
          { container: "nginx-target", running: false, healthy: null, reason: "parado" },
        ]),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "aguardar_saude",
      }),
    ).toEqual({
      aguardar_saude: "falha",
      build_online: "pendente",
      verificar_egresso: "pendente",
      carregar_abilities: "pendente",
      verificar_isolamento: "pendente",
    })
  })

  it("subir_conteineres fails before health → fails the healthy-containers item", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: emptyHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "subir_conteineres",
      }).aguardar_saude,
    ).toBe("falha")
  })

  it("build_online failure (5.1) → health ok, online fail, later ones pending (Req. 6.3)", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "build_online",
      }),
    ).toEqual({
      aguardar_saude: "ok",
      build_online: "falha",
      verificar_egresso: "pendente",
      carregar_abilities: "pendente",
      verificar_isolamento: "pendente",
    })
  })

  it("encerrar_egresso failure folds into the online item (sub-stage 5.1)", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "encerrar_egresso",
      }).build_online,
    ).toBe("falha")
  })

  it("verificar_egresso failure (5.2) → online ok, egress fail, later ones pending (Req. 6.3)", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "verificar_egresso",
      }),
    ).toEqual({
      aguardar_saude: "ok",
      build_online: "ok",
      verificar_egresso: "falha",
      carregar_abilities: "pendente",
      verificar_isolamento: "pendente",
    })
  })

  it("reisolar and setup_offline failures fold into the egress item (sub-stage 5.2)", () => {
    expect(
      derivePrereqStates({
        state: "erro",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "reisolar",
      }).verificar_egresso,
    ).toBe("falha")
    expect(
      derivePrereqStates({
        state: "erro",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        failedStep: "setup_offline",
      }).verificar_egresso,
    ).toBe("falha")
  })

  it("no failedStep → derives the new items from the substages data", () => {
    expect(
      derivePrereqStates({
        state: "em_andamento",
        health: approvedHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
        substages: {
          build_online_ok: true,
          egress_closed: true,
          egress_lost_verified: false,
          offline_applied: false,
          egress_verdicts: [],
        },
      }),
    ).toMatchObject({
      aguardar_saude: "ok",
      build_online: "ok",
      verificar_egresso: "pendente",
    })
  })

  it("nothing ran (nao_iniciado, empty summaries, abilities false) → the five pending", () => {
    expect(
      derivePrereqStates({
        state: "nao_iniciado",
        health: emptyHealth(),
        isolation: emptyIsolation(),
        abilitiesLoaded: false,
      }),
    ).toEqual({
      aguardar_saude: "pendente",
      build_online: "pendente",
      verificar_egresso: "pendente",
      carregar_abilities: "pendente",
      verificar_isolamento: "pendente",
    })
  })
})

// ── 2. Três estados por item renderizados no DOM (Req. 5.6) ──────────────────

describe("PrereqChecklist — three states per item rendered (Req. 5.6)", () => {
  it("concluido: the three items 'OK' and the environment-ready confirmation", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "concluido",
          health: approvedHealth(),
          isolation: approvedIsolation(),
          abilitiesLoaded: true,
        })}
      />,
    )

    expect(itemStatus(/Contêineres saudáveis/i)).toBe("OK")
    expect(itemStatus(/Abilities carregadas/i)).toBe("OK")
    expect(itemStatus(/Isolamento aprovado/i)).toBe("OK")

    const confirmation = document.querySelector('[role="status"]')
    expect(confirmation).not.toBeNull()
    expect(confirmation).toHaveTextContent(/Ambiente pronto/i)
  })

  it("mid failure (carregar_abilities): containers OK, abilities Falha, isolation Pendente", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "erro",
          health: approvedHealth(),
          isolation: emptyIsolation(),
          abilitiesLoaded: false,
          failedStep: "carregar_abilities",
          errorMessage: "Falha ao carregar Abilities no Caldera",
        })}
      />,
    )

    expect(itemStatus(/Contêineres saudáveis/i)).toBe("OK")
    expect(itemStatus(/Abilities carregadas/i)).toBe("Falha")
    expect(itemStatus(/Isolamento aprovado/i)).toBe("Pendente")
  })

  it("nao_iniciado with empty summaries + abilities false: the three 'Pendente'", () => {
    renderWithI18n(<PrereqChecklist {...props({ state: "nao_iniciado" })} />)

    expect(itemStatus(/Contêineres saudáveis/i)).toBe("Pendente")
    expect(itemStatus(/Abilities carregadas/i)).toBe("Pendente")
    expect(itemStatus(/Isolamento aprovado/i)).toBe("Pendente")
  })
})

// ── 3. Failure message, naming the offending resource (Req. 7.2 / 7.4) ───────

describe("PrereqChecklist — failure message (Req. 7.2, 7.4)", () => {
  it("health failure: names the offending container + reason and shows the errorMessage in the alert", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "erro",
          health: failedHealth([
            {
              container: "mariadb-target",
              running: false,
              healthy: false,
              reason: "contêiner parado",
            },
          ]),
          isolation: emptyIsolation(),
          abilitiesLoaded: false,
          failedStep: "aguardar_saude",
          errorMessage: "Verificação de saúde reprovou",
        })}
      />,
    )

    // Container name + reason appear (resource identification, Req. 7.4).
    expect(document.body).toHaveTextContent(/mariadb-target/i)
    expect(document.body).toHaveTextContent(/contêiner parado/i)

    // errorMessage shown in the failure alert.
    const alert = document.querySelector('[role="alert"]')
    expect(alert).not.toBeNull()
    expect(alert).toHaveTextContent(/Verificação de saúde reprovou/i)
    expect(within(alert as HTMLElement).getByText(/permanece bloqueado/i)).toBeInTheDocument()
  })

  it("isolation failure: names the non-isolated container + reason (Req. 7.4)", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "erro",
          health: approvedHealth(),
          isolation: failedIsolation([
            {
              container: "kali-attacker",
              isolated: false,
              reasons: ["rede externa detectada: bridge"],
              external_networks: ["bridge"],
              external_dns: [],
            },
          ]),
          abilitiesLoaded: true,
          failedStep: "verificar_isolamento",
          errorMessage: "Isolamento reprovado",
        })}
      />,
    )

    expect(document.body).toHaveTextContent(/kali-attacker/i)
    expect(document.body).toHaveTextContent(/rede externa detectada: bridge/i)

    const alert = document.querySelector('[role="alert"]')
    expect(alert).not.toBeNull()
    expect(alert).toHaveTextContent(/Isolamento reprovado/i)
  })
})

// ── 4. Blocking/unblocking "Next" — checklist communication (Req. 7.3) ──

describe("PrereqChecklist — Stage 6 blocking/unblocking (Req. 7.3)", () => {
  it("on failure: shows the Stage 6 blocked indicator and NOT the ready confirmation", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "erro",
          health: approvedHealth(),
          isolation: emptyIsolation(),
          abilitiesLoaded: false,
          failedStep: "carregar_abilities",
          errorMessage: "Falha ao carregar Abilities",
        })}
      />,
    )

    expect(document.body).toHaveTextContent(/permanece bloqueado/i)
    expect(document.body).not.toHaveTextContent(/Ambiente pronto/i)
    expect(document.querySelector('[role="status"]')).toBeNull()
  })

  it("pending/in-progress: communicates blocking without a failure alert", () => {
    renderWithI18n(
      <PrereqChecklist {...props({ state: "em_andamento" })} />,
    )

    expect(document.body).toHaveTextContent(/permanece bloqueado/i)
    expect(document.querySelector('[role="alert"]')).toBeNull()
    expect(document.querySelector('[role="status"]')).toBeNull()
  })

  it("concluido: unblocks (ready confirmation) and does NOT show the blocked indicator", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "concluido",
          health: approvedHealth(),
          isolation: approvedIsolation(),
          abilitiesLoaded: true,
        })}
      />,
    )

    expect(document.body).toHaveTextContent(/Ambiente pronto/i)
    expect(document.body).not.toHaveTextContent(/permanece bloqueado/i)
    expect(document.querySelector('[role="status"]')).not.toBeNull()
  })

  it("respects explicit stage6Blocked=false (no alert/no blocking) when there is no failure", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "em_andamento",
          abilitiesLoaded: true,
          stage6Blocked: false,
        })}
      />,
    )

    expect(document.body).not.toHaveTextContent(/permanece bloqueado/i)
    expect(document.querySelector('[role="alert"]')).toBeNull()
  })
})

// ── 5. New sub-stage prerequisites (Req. 6.3, 6.4, 8.2) ──────────────────────

describe("PrereqChecklist — sub-stage prerequisites (Req. 6.3, 6.4, 8.2)", () => {
  it("renders the two new items alongside the inherited three", () => {
    renderWithI18n(<PrereqChecklist {...props({ state: "nao_iniciado" })} />)

    expect(document.body).toHaveTextContent(/Contêineres saudáveis/i)
    expect(document.body).toHaveTextContent(/Pré-requisitos online obtidos/i)
    expect(document.body).toHaveTextContent(/Egresso perdido \(verificado\)/i)
    expect(document.body).toHaveTextContent(/Abilities carregadas/i)
    expect(document.body).toHaveTextContent(/Isolamento aprovado/i)
    // Five items in the checklist list.
    expect(itemStatus(/Pré-requisitos online obtidos/i)).toBe("Pendente")
    expect(itemStatus(/Egresso perdido \(verificado\)/i)).toBe("Pendente")
  })

  it("concluido with satisfied substages: the five items OK and the ready confirmation", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "concluido",
          health: approvedHealth(),
          isolation: approvedIsolation(),
          abilitiesLoaded: true,
          substages: satisfiedSubstages(),
        })}
      />,
    )

    expect(itemStatus(/Pré-requisitos online obtidos/i)).toBe("OK")
    expect(itemStatus(/Egresso perdido \(verificado\)/i)).toBe("OK")
    expect(document.querySelector('[role="status"]')).toHaveTextContent(/Ambiente pronto/i)
  })

  it("verificar_egresso failure: online OK, egress Falha, cause named + Stage 6 blocked (Req. 8.2)", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "erro",
          health: approvedHealth(),
          isolation: emptyIsolation(),
          abilitiesLoaded: false,
          substages: egressFailedSubstages([
            {
              container: "nginx-target",
              egress_lost: false,
              conclusive: true,
              reason: "egresso ainda presente: alcançou 1.1.1.1",
            },
          ]),
          failedStep: "verificar_egresso",
          errorMessage: "Perda de egresso não verificada",
        })}
      />,
    )

    // Preceding item passed; the egress item failed; later ones pending.
    expect(itemStatus(/Pré-requisitos online obtidos/i)).toBe("OK")
    expect(itemStatus(/Egresso perdido \(verificado\)/i)).toBe("Falha")
    expect(itemStatus(/Abilities carregadas/i)).toBe("Pendente")

    // The offending host + probe outcome are named (Req. 8.2).
    expect(document.body).toHaveTextContent(/nginx-target/i)
    expect(document.body).toHaveTextContent(/egresso ainda presente/i)

    // Failure alert + Stage 6 blocked.
    const alert = document.querySelector('[role="alert"]')
    expect(alert).not.toBeNull()
    expect(alert).toHaveTextContent(/Perda de egresso não verificada/i)
    expect(within(alert as HTMLElement).getByText(/permanece bloqueado/i)).toBeInTheDocument()
    expect(document.querySelector('[role="status"]')).toBeNull()
  })

  it("build_online failure (5.1): online Falha, later items pending, Stage 6 blocked", () => {
    renderWithI18n(
      <PrereqChecklist
        {...props({
          state: "erro",
          health: approvedHealth(),
          isolation: emptyIsolation(),
          abilitiesLoaded: false,
          failedStep: "build_online",
          errorMessage: "Falha ao instalar os pré-requisitos online",
        })}
      />,
    )

    expect(itemStatus(/Contêineres saudáveis/i)).toBe("OK")
    expect(itemStatus(/Pré-requisitos online obtidos/i)).toBe("Falha")
    expect(itemStatus(/Egresso perdido \(verificado\)/i)).toBe("Pendente")
    expect(document.querySelector('[role="alert"]')).toHaveTextContent(
      /Falha ao instalar os pré-requisitos online/i,
    )
    expect(document.body).toHaveTextContent(/permanece bloqueado/i)
  })
})
