# STICKS (Pipeline UI + Adversary Emulation)

---

## 1. In one sentence

Since the last meeting, I took the project off paper: I went from a written
specification and 8 manually curated cases to a **pipeline working end to
end**, with a web interface, backend, security containment, and — the most
important milestone — the **real emulation of an adversary (ShadowRay)
executed successfully inside the isolated Docker lab**.

To move faster while keeping quality, I used **AI as a support tool** (pairing
to code, debug, and document), always driving the decisions on architecture,
security, and validation.

---

## 2. Quick context (to orient the professor)

STICKS accompanies the paper on the *procedural semantics gap in
ATT&CK-in-STIX*. The pipeline has **3 stages**:

1. **Structural Modeling** (automated): extracts techniques, relationships,
   indicators, infrastructure, and malware from CTI in STIX.
2. **Curated Translation** (human today): converts abstract behavior into
   minimal executable steps — **this is the gap the research wants to
   automate**.
3. **Adversary Emulation**: executes the steps in MITRE Caldera, inside an
   isolated Docker environment.

There are **8 campaigns already curated manually**. My work this phase was to
build the foundation — interface, orchestration, and lab — to first
**understand and reproduce** this flow well before automating Stage 2.

---

## 3. What I delivered from the last meeting until today

### 3.1. Pipeline web interface (frontend)
- A React + TypeScript SPA that makes the pipeline evident: the 3 stages side
  by side, with state (not started / in progress / complete / error), Input /
  Processing / Output sections, and progress.
- Selection among the 8 campaigns, driving the order 1 → 2 → 3 with
  **sequential locking** (it does not allow starting a blocked stage).
- **Emulation confirmation modal**: before executing anything, it shows the
  full list of concrete commands and the target container of each one. Nothing
  runs without explicit confirmation.
- Light/dark theme (following the system and persistent) and pt-BR / en
  internationalization.

### 3.2. Backend and orchestration (FastAPI)
- Services that read the 8 campaigns, run the real Stage 1, orchestrate the
  stages, talk to Caldera, and persist state.
- REST API + real-time channels (WebSocket) for progress.
- State persistence to carry the work across sessions.

### 3.3. Security core — containment (the point I treated as non-negotiable)
- **Target validation**: any command pointing outside the lab's internal
  subnets **blocks the entire operation** and identifies the offending command.
- **Isolation check**: before executing, I inspect (read-only) the target
  containers; if any is not isolated, the operation is aborted.
- **1:1 audit trail**: each executed command generates exactly one record
  (command + target container + result).

### 3.4. The lab environment (the SUT — System Under Test)
I set up and left running the isolated network that simulates the target:

```
caldera (C2)  →  kali (attacker)  →  nginx (web target)  →  db (backend target)
```

- Everything on `internal: true` networks — **no route to the host or to the
  internet**.
- The attacker (Kali) runs the Caldera agent and executes each campaign's
  commands against the targets.

### 3.5. The milestone: real end-to-end emulation
I brought the lab up and **ran the ShadowRay campaign for real**: the
**11 techniques** ran in the real environment, in a contained way, with the
audit trail persisted. This was the only milestone left to close the phase.

---

## 4. Metrics (as proof)

| Metric | Number |
|---|---|
| Pipeline stages working | 3 |
| Curated campaigns integrated | 8 |
| Techniques executed on ShadowRay (real emulation) | 11 |
| Stage 1 extraction (ShadowRay) | 10 techniques, 9 relationships, 14 indicators, 2 infra, 2 malware |
| Automated backend tests | 188 (across 25 files) |
| Frontend tests | 17 (across 6 files) |
| Verified correctness properties (property-based) | 12 |
| Containers in the lab | 4 (caldera, kali, nginx, db) |
| Real ShadowRay E2E result | **PASSED** |

Note: the **12 properties** include the security ones (no emulation starts with
an external target; it only proceeds with all targets isolated; execution
requires explicit confirmation; 1:1 audit; an audit failure aborts the
operation). These were tested with **hundreds of iterations** each.

---

## 5. What was easier and what was harder

### Easier / flowed well
- The **interface and the backend**: with a clear specification (requirements →
  design → tasks), the build was incremental and predictable.
- The **containment logic and the tests**: being pure logic, I could validate
  everything with automated tests, without depending on the real environment.
- **Stage 1** (structural modeling): I reused the existing parsing and
  validated the real extraction with the local files.

### Harder / where I spent the most effort
- **Bringing up the real lab was the biggest challenge** — and where I learned
  the most. I ran into a chain of real problems that I had to diagnose one by
  one:
  1. The **Kali image** broke on download (giant metapackages). I solved it by
     slimming the image down to only the tools the campaigns use
     (from ~2.4 GB to ~400 MB).
  2. **Caldera came up but without the web interface** — I found that a folder
     share was hiding the already-compiled UI and was also stalling the boot.
  3. The **targets would not come up** because they tried to install packages
     from the internet at runtime, but the network is isolated on purpose. I
     moved those installs to image build time.
  4. The **first emulation ran "empty"**: the adversary profile was not loaded
     into Caldera and the system did not wait for the execution to finish. I
     fixed the loading and the operation tracking.
- The lesson: containment (no internet) is exactly what makes the lab safe, but
  also what demands the most care to set up — everything has to be resolved
  offline, ahead of time.

---

## 6. An engineering decision worth highlighting: lab addressing

I noticed the lab used large class B network ranges that **collided with other
networks already present on the machine**. Such a collision could route an
attack command to the wrong place — a containment risk.

I redesigned the addressing to smaller class C ranges (192.168.x), with a
readable convention: **the final number of the IP identifies the machine** (the
attacker is always `.20`, the web target always `.30`), on any network. And I
added an **automatic check that runs before bringing up the lab** and aborts if
any range is already in use on the machine — to guarantee that no attack packet
escapes outside the environment.

---

## 7. Current state and next steps

**State:** the foundation phase is complete. The pipeline works end to end, the
real emulation is proven, and everything is covered by tests and documented.

**Next steps:**
1. Reproduce **1–2 more campaigns** in the real lab, besides ShadowRay.
2. Do the **visual demonstration** through the interface: pick a campaign and
   walk through the 3 stages up to the emulation.
3. Start **Phase 2 — automating Stage 2** (the translation that is manual
   today), which is the central goal of the research. The foundation is already
   prepared to accommodate this: the "translation source" is an extensible
   attribute (today "human curation"; in the future "automated agent").

---

## 8. Suggested outline for the 15–20 min

1. **(2 min)** Context: the Stage 2 gap and the 3 stages.
2. **(3 min)** What I delivered: interface, backend, containment, lab.
3. **(4 min)** Demonstration: open the UI, show the 3 stages and the
   confirmation modal with the concrete commands.
4. **(4 min)** The milestone: the real ShadowRay emulation (show that the 11
   techniques ran, contained, with audit).
5. **(3 min)** Metrics and what was hardest (the real lab).
6. **(2 min)** Next steps: automating Stage 2.
