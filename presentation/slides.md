---
theme: default
title: "STICKS — Measuring Procedural Sufficiency for APT Emulation"
titleTemplate: "%s"
favicon: /favicon.ico
highlighter: shiki
lineNumbers: false
drawings:
  persist: false
transition: slide-left
mdc: true
fonts:
  sans: Inter
  mono: JetBrains Mono
aspectRatio: 16/9
canvasWidth: 1180
---

<div class="cover" style="position:absolute;inset:0;"></div>

<div class="absolute top-10 left-14 right-14">
  <img src="/sticks-logomarca-modo-claro.png" class="h-20" alt="STICKS logo" />
</div>

<div class="absolute left-14 right-14" style="top:34%;">
  <div class="kicker">CE-299 · Artificial Intelligence for Cybersecurity</div>
  <h1 style="font-size:2.7rem;line-height:1.08;max-width:60rem;">
    The <span class="grad-text">Procedural Semantics Gap</span> in ATT&amp;CK-in-STIX
  </h1>
  <div style="font-size:1.2rem;color:var(--ink-soft);margin-top:.5rem;max-width:54rem;">
    Measuring procedural sufficiency for APT emulation — and automating curation with an AI agent
  </div>
  <div class="grad-rule" style="margin-top:1.1rem;width:120px;height:5px;"></div>
</div>

<div class="absolute bottom-16 left-14 right-14 flex justify-between items-end">
  <div>
    <div style="font-size:1.35rem;font-weight:800;color:var(--ink);">Nilson Sangy</div>
    <div style="font-size:.95rem;color:var(--muted);">Instituto Tecnológico de Aeronáutica · STICKS research platform</div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:1.05rem;font-weight:700;color:var(--sticks-purple);">Nov 20, 2026</div>
    <div style="font-size:.8rem;color:var(--muted);">github.com/c2dc/sticks</div>
  </div>
</div>

<!--
Good evening everyone, and thank you for being here. My name is Nilson Sangy, and today I am going to present STICKS, a research platform I have been building for the course CE-299, Artificial Intelligence for Cybersecurity.

The full title of the work is "The Procedural Semantics Gap in ATT&CK-in-STIX: Measuring Procedural Sufficiency for APT Emulation." That is a long title, so let me unpack it in plain terms before we dive in. ATT&CK and STIX are the standard ways the security community describes what attackers do. The problem is that they describe attacker behavior at a fairly abstract level — they tell you which techniques an adversary used, but not the exact procedure: the concrete commands, the order they run in, and the environment they need. That missing layer is what we call the "procedural semantics gap," and measuring it is the heart of this research.

My specific contribution — the part I want you to remember from tonight — is the piece at the end of the subtitle: automating that curation with an AI agent. Today, closing the gap is done by hand. A human analyst sits down and translates abstract techniques into runnable attacks, and also designs the environment where those attacks can be emulated safely. My goal is to build an AI agent that does both of those jobs automatically.

So the talk has a clear arc. First I will set up the problem and why it matters. Then I will walk through the building blocks — ATT&CK, Caldera, Atomic Red Team, and how we provision isolated environments. After that I will take you through the six-stage pipeline that already exists, explaining the input, processing, and output of each stage, because understanding that flow is what makes my proposal concrete. Finally I will present the AI agent itself — the architecture and each component — and close with what we have already proven working and where the project goes next.

One framing note: everything in STICKS runs inside a fully contained laboratory. No attack command ever touches the host or the internet. That containment is non-negotiable, and you will hear me come back to it several times, because it is both what makes the lab safe and what makes it technically hard.

Let's begin with the motivation.
-->

---
layout: default
---

# Motivation — the gap, and the part I automate

<div class="grad-rule"></div>

<div class="two-col">
<div class="tight">

<div class="card edge-blue">
<h3>What structured CTI gives us</h3>
<p>STIX + ATT&CK tell us <strong>which</strong> techniques a campaign used — a shared, machine-readable vocabulary of adversary behavior.</p>
</div>

<div class="card edge-red">
<h3>What it does <em>not</em> give us — the procedural semantics gap</h3>
<p>It does <strong>not</strong> give the <strong>procedure</strong>: the concrete commands, their order, their dependencies, and the environment the attack needs. Without that, a campaign cannot be <strong>reproduced</strong>.</p>
</div>

<div class="card edge-grad">
<h3>My work — an AI agent that fills the gap on two fronts</h3>
<ul>
<li><span class="pill purple">1 · Command semantics</span> generate the <strong>executable commands</strong> of a campaign — the right techniques, parametrized, in the right order.</li>
<li><span class="pill red">2 · Environment emulation</span> design &amp; provision the <strong>isolated environment</strong> where those attacks are emulated.</li>
</ul>
</div>

</div>

<div>
<div class="imgframe" style="height:220px;">
<img src="/images/motivation-code.jpg" alt="binary code" />
</div>
<div class="caption">Image: “Closeup binary coding screen”, rawpixel — CC0 (public domain), via Openverse.</div>

<div class="card flat" style="margin-top:.8rem;">
<h3>Why it matters for research</h3>
<p>Today this translation is <strong>human-curated</strong> and slow. STICKS makes the gap <strong>measurable</strong>; automating the curation lets us <strong>compare AI vs. human</strong> procedural sufficiency at scale.</p>
</div>
</div>

</div>

<!--
Let me make the motivation concrete, because this one idea drives the entire project.

When an organization wants to understand a threat, it consumes cyber threat intelligence — CTI — and the standard format for that is STIX, structured on top of the MITRE ATT&CK knowledge base. This is genuinely useful: it gives us a shared, machine-readable vocabulary. If I say an APT used "Phishing" for initial access and "OS Credential Dumping" for credential access, every analyst in the world knows exactly what I mean. That is the left-hand card: structured CTI tells us WHICH techniques a campaign used.

But here is the catch, and it is the whole reason this research exists. Knowing which techniques were used is not the same as being able to reproduce the attack. ATT&CK tells you "the adversary dumped credentials." It does not tell you the exact command, which tool, with which arguments, in what order relative to the other steps, or what has to already be true in the environment for that command to even work. That missing layer — the concrete, runnable procedure — is what we call the procedural semantics gap. It is the difference between a description of an attack and an executable reproduction of it. If you cannot reproduce a campaign, you cannot reliably test your defenses against it.

Now, my specific contribution. Closing that gap is possible — in fact my colleagues did it by hand for eight real campaigns. But doing it manually is slow, needs deep expertise, and does not scale. So my work is to build an AI agent that fills the gap automatically, and crucially it has to do it on two fronts, which you can see in the gradient card.

The first front is command semantics: turning abstract techniques into the actual executable commands of the campaign — selecting the right technique implementation, parametrizing it for this target, and ordering it correctly. The second front is environment emulation: an attack command is meaningless without a target that has the right services and vulnerabilities, wired into the right network. So the agent must also design and provision the isolated environment in which the attack is emulated.

Why does this matter for the research? Because STICKS turns the gap into something we can measure — a success rate, a verdict. Once curation is automated, we can run the AI agent against the same eight campaigns a human curated and directly compare: does the AI reach the same procedural sufficiency as a human expert? That comparison is the scientific payoff.
-->

---
layout: default
---

# Agenda — our roadmap for today

<div class="grad-rule"></div>

<div class="cards c2" style="gap:1rem;">

<div class="card edge-blue">
<h3>1 · Background &amp; building blocks</h3>
<ul>
<li>The MITRE ATT&amp;CK matrix — how to read it</li>
<li>MITRE Caldera &amp; the isolated lab</li>
<li>Atomic Red Team</li>
<li>Provisioning types</li>
</ul>
</div>

<div class="card edge-purple">
<h3>2 · The six-stage pipeline</h3>
<ul>
<li>Overview &amp; sequential gating</li>
<li>One slide per stage —<br/><strong>Input · Processing · Output</strong></li>
<li>What exists &amp; works today</li>
</ul>
</div>

<div class="card edge-grad">
<h3>3 · The gap &amp; my proposal</h3>
<ul>
<li>The manual curation gap</li>
<li>The AI curation agent</li>
<li>Architecture &amp; components</li>
</ul>
</div>

<div class="card edge-red">
<h3>4 · Evidence &amp; close</h3>
<ul>
<li>What is already proven (real E2E)</li>
<li>Roadmap &amp; next steps</li>
<li>Q&amp;A</li>
</ul>
</div>

</div>

<div style="margin-top:1rem;" class="pill blue">We spend the most time on parts 2 and 3 — the pipeline and the agent.</div>

<!--
Here is how the talk is structured, so you know where we are going.

I have organized it into four parts after this agenda. Let me walk through them quickly so the structure is in your head, and so you can follow the thread.

Part one is background and building blocks. Before I can explain my contribution, we need a shared understanding of a few foundational pieces. I will start with the MITRE ATT&CK matrix itself — what it is and how to read it — because everything else sits on top of it, and some of you may not work with it day to day. Then MITRE Caldera and our isolated laboratory, which is the engine and the safe sandbox where attacks actually run. Then Atomic Red Team, which is a library of concrete attack commands. And finally the different ways we provision the target environment, which turns out to be surprisingly subtle because of the containment requirement.

Part two is the six-stage pipeline. This is the system as it exists and works today. I will give you an overview first, and then spend one slide on each of the six stages. For every stage I will use the same three-part structure — input, processing, and output — because that consistent framing is exactly what makes the later automation proposal concrete. By the end of part two you will understand the full data flow from raw threat intelligence all the way to a measured execution report.

Part three is the core of my work: the gap and my proposal. I will first make very explicit which parts of the pipeline a human does by hand today. Then I will present my proposal — an AI agent that automates that curation — followed by the architecture of the agentic environment and a breakdown of each component.

Part four is evidence and close. I will show you what is already proven to work, including a real end-to-end emulation we ran, then the roadmap and next steps, and we will finish with questions.

If you only track two things tonight, track parts two and three — the pipeline and the agent. That is where the substance is, and that is where I will spend most of our time. Everything in part one is there to make those two parts land. With that map in mind, let us start with the building blocks, beginning with the ATT&CK matrix.
-->

---
layout: default
class: section-divider
---

<div class="section" style="position:absolute;inset:0;"></div>

<div class="absolute top-8 left-14">
  <img src="/sticks-logomarca-modo-claro.png" class="h-10 opacity-90" alt="STICKS" />
</div>

<div class="absolute left-14 right-14" style="top:40%;">
  <div class="kicker">Section 01</div>
  <h1 style="font-size:3rem;">Background &amp; <span class="grad-text">Building Blocks</span></h1>
  <div class="grad-rule" style="width:140px;height:6px;"></div>
  <div style="font-size:1.1rem;color:var(--ink-soft);max-width:48rem;">
    ATT&amp;CK · MITRE Caldera &amp; the isolated lab · Atomic Red Team · provisioning types
  </div>
</div>

<!--
We are now entering the first section: background and building blocks. The purpose of this section is to make sure everyone shares the same mental model before I get to the pipeline and my proposal.

There are four building blocks I want to cover. The first is the MITRE ATT&CK matrix, which is the foundational knowledge base for describing attacker behavior. The second is MITRE Caldera together with the isolated laboratory — Caldera is the adversary-emulation engine, and the lab is the contained Docker environment where everything runs. The third is Atomic Red Team, a community library that gives us concrete commands for individual techniques. And the fourth is provisioning, meaning the different ways we stand up the target environment safely.

I am deliberately spending time here because these four concepts recur throughout the rest of the talk. When I later say "the agent picks a command from Atomic Red Team and parametrizes it for a target in the isolated lab," I want that sentence to already make complete sense to you. So let us take them one at a time, starting with the ATT&CK matrix, which is the thing everything else hangs on.
-->

---
layout: default
---

# The MITRE ATT&CK matrix

<div class="grad-rule"></div>

<div class="imgframe" style="height:330px;">
<img src="/images/attack-matrix.svg" style="object-fit:contain;background:#fff;" alt="ATT&CK Enterprise matrix" />
</div>

<div class="flex justify-between items-center" style="margin-top:.6rem;gap:1rem;">
<div style="font-size:.84rem;color:var(--ink-soft);">
<strong>Columns = Tactics</strong> (the "why") · <strong>Cells = Techniques</strong> (the "how") · a campaign is an <strong>ordered path</strong> across the matrix.
</div>
<a href="https://attack.mitre.org/" style="font-size:1rem;font-weight:700;white-space:nowrap;">https://attack.mitre.org/</a>
</div>

<!--
This is the MITRE ATT&CK matrix, and for anyone who has not worked with it, this slide is for you. ATT&CK stands for Adversarial Tactics, Techniques, and Common Knowledge. It is a globally used, freely available knowledge base of real-world adversary behavior, curated by MITRE from threat reports and incident data. If you want to explore it yourself, the address is on the slide — attack dot mitre dot org — and I encourage you to type it in and click around; it is one of the best free resources in our field.

Here is how to read the matrix, because the layout encodes meaning. Across the top you have the columns, and each column is a tactic. A tactic is the adversary's goal — the "why" of an action. Reading left to right, they roughly follow the arc of an intrusion: Reconnaissance, Resource Development, Initial Access, Execution, Persistence, Privilege Escalation, Defense Evasion, Credential Access, Discovery, Lateral Movement, Collection, Command and Control, Exfiltration, and finally Impact. Fourteen tactics in total for the Enterprise matrix.

Underneath each tactic, the cells are techniques. A technique is the "how" — a specific method an adversary uses to achieve that tactic. For example, under Initial Access you will find "Phishing"; under Credential Access you will find "OS Credential Dumping." Many techniques further break down into sub-techniques, so the real matrix has hundreds of entries. I have shown a representative handful per column so it stays readable.

Now the key insight, which is on the bottom of the figure and is the bridge to my whole talk. A real campaign — what an actual APT group did — is a path across this matrix: an ordered chain of techniques, one leading to the next. ATT&CK is excellent at telling us the "what": which techniques a given actor or campaign is associated with. That is exactly the knowledge that gets encoded into STIX threat intelligence.

But — and this is the crucial limitation — ATT&CK does not give us the procedure. It names the technique "OS Credential Dumping," but it does not hand you the exact command, the tool, the arguments, the order, or the prerequisites. That missing "how-exactly" is the procedural semantics gap we talked about in the motivation. So ATT&CK gives us a precise shared language for behavior, and simultaneously shows us exactly what is missing when we try to turn that language back into a runnable attack. Keep that tension in mind, because the rest of the pipeline is built to close it.
-->

---
layout: default
---

# MITRE Caldera &amp; the isolated lab

<div class="grad-rule"></div>

<div class="two-col even">
<div class="tight">

<div class="card edge-blue">
<h3>MITRE Caldera — the emulation engine</h3>
<p>An open-source <strong>adversary-emulation / C2</strong> platform. It runs <strong>Abilities</strong> (executable commands) grouped into an <strong>Adversary</strong> profile, against agents deployed on target hosts, and tracks each step live.</p>
</div>

<div class="card edge-red">
<h3>Containment is non-negotiable</h3>
<ul>
<li>Attack commands run <strong>only</strong> inside internal-only Docker networks</li>
<li><strong>No route</strong> to the host or the internet</li>
<li>External target → the whole run is <strong>blocked</strong></li>
<li>Non-isolated container → run is <strong>aborted</strong> (read-only check)</li>
<li>Explicit <strong>preview + confirmation</strong>; <strong>1:1 audit</strong> per command</li>
</ul>
</div>

</div>

<div>
<div class="imgframe" style="height:250px;">
<img src="/images/lab-topology.svg" style="object-fit:contain;background:#fff;" alt="Lab topology" />
</div>
<div class="caption">The System Under Test: C2 → attacker → web target → data target, on three internal /24 networks.</div>
</div>

</div>

<!--
The second building block is the pair that actually executes attacks: MITRE Caldera and our isolated laboratory.

Let me start with Caldera, on the left. Caldera is an open-source adversary-emulation platform maintained by MITRE. You can think of it as a command-and-control server built specifically for red teaming and automated emulation. It has two concepts that will come back later. The first is an "Ability," which is a single executable action — essentially a concrete command tied to an ATT&CK technique. The second is an "Adversary," which is a profile that bundles and orders a set of Abilities to represent a specific threat actor or campaign. Caldera deploys a lightweight agent onto each target host, sends the Abilities to be executed, and tracks the result of every step in real time. So Caldera is the engine that takes our curated commands and actually runs the campaign.

Now the right side — the laboratory, which we also call the System Under Test. This is the contained Docker environment where the emulation happens, and this diagram is its topology. There are four containers. Caldera is the C2 server. Kali is the attacker machine — it runs the Caldera agent and is where the attack commands actually execute from. Then there are two targets: an nginx web server and a database. They are wired together across three separate internal networks — caldera-to-kali, kali-to-nginx, and nginx-to-db — each a small class C subnet. We even adopted a readable convention where the last number of the IP identifies the machine: the attacker is always dot twenty, the web target always dot thirty, and so on, on every network it sits on.

And now the part I will keep repeating, because it is genuinely the backbone of the design: containment is non-negotiable. Every one of these networks is marked internal-only. That means there is no route from inside the lab to the host machine or to the internet. Attack commands can only ever reach the lab's own targets. On top of that physical isolation, the system enforces several software guards. Before anything runs, it parses every command's target; if any target points outside the internal subnets, the entire operation is blocked and the offending command is named. It also does a read-only inspection of the target containers; if any is not properly isolated, the run is aborted. Nothing executes without an explicit preview and confirmation of the exact commands and their targets. And every executed command produces exactly one audit record — a one-to-one trail. This is what lets us safely emulate a real APT campaign on a laptop without any risk of a command escaping into the wild.
-->

---
layout: default
---

# Atomic Red Team

<div class="grad-rule"></div>

<div class="cards c2" style="gap:1rem;">

<div class="card edge-blue">
<h3>What it is</h3>
<p>An open-source library of small, portable <strong>"atomic tests"</strong> — <strong>one concrete command per ATT&amp;CK technique</strong>, mapped to the technique ID.</p>
<ul>
<li>Each test exposes <code>dependencies</code> / <code>get_prereq_command</code></li>
<li><code>input_arguments</code> — the parametrizable targets</li>
<li><code>supported_platforms</code> — where it can run</li>
</ul>
</div>

<div class="card edge-red">
<h3>Why it is <em>not</em> enough on its own</h3>
<ul>
<li>It gives the command <strong>per technique</strong> — but not the <strong>chaining</strong> of a campaign (order + dependencies between techniques).</li>
<li>That chaining is exactly what the <strong>DAG</strong> provides (Stage 3).</li>
<li>It is a <strong>catalog</strong>, not a curator: selecting, parametrizing &amp; sequencing for a specific campaign is the manual work the AI agent will automate.</li>
</ul>
</div>

</div>

<div class="card flat edge-purple" style="margin-top:1rem;">
<h3>Its role in STICKS</h3>
<p>Atomic Red Team is the planned <strong>source of commands</strong> for the future AI agent at Stage 4 (Operationalization). Fields like <code>dependencies</code>, <code>input_arguments</code> and <code>supported_platforms</code> map directly onto the "environment parameters" the pipeline needs. Caldera also ships an <strong>Atomic</strong> plugin that ingests these tests as Abilities.</p>
</div>

<!--
The third building block is Atomic Red Team, and this slide is important because it sets up precisely where my AI agent does its most interesting work.

Atomic Red Team is an open-source project, originally from Red Canary, that is essentially a library of what they call "atomic tests." An atomic test is a small, self-contained, portable piece of attack behavior — in practice, one concrete command — and each test is mapped to a specific ATT&CK technique ID. So where ATT&CK says "Technique T-such-and-such: OS Credential Dumping," Atomic Red Team gives you an actual command you can run that performs that technique. That is extremely valuable, because it is the first time we move from the abstract "what" to a concrete "how."

Each atomic test is also nicely structured, and three fields matter for us. There is a dependencies or prerequisite-command field, which tells you what must already be set up before the test can run. There are input arguments, which are the parametrizable parts — typically the targets, like which host or which file. And there is a supported-platforms field, telling you whether it runs on Windows, Linux, and so on. Hold onto those three fields, because in a moment you will see they map almost one-to-one onto what we call "environment parameters" in the pipeline.

Now the critical point, on the right: Atomic Red Team by itself is not enough to reproduce a campaign. It gives you the command for a single technique in isolation. What it does not give you is the chaining — the order in which the techniques run across a whole campaign, and the dependencies between them. A real campaign is not a bag of independent commands; step five might only work because step three already created a user or planted a file. That ordering and those cross-step dependencies come from somewhere else in our pipeline — the DAG, which I will cover in Stage 3.

So the honest way to describe Atomic Red Team is: it is a catalog, not a curator. It is a rich inventory of per-technique commands, but somebody still has to select the right test for each technique in this specific campaign, parametrize it for the specific targets in our lab, and sequence everything correctly. Today a human does that selection and parametrization. In STICKS, Atomic Red Team is the planned source of commands for the AI agent at Stage 4, Operationalization — and that selecting-parametrizing-sequencing job is exactly the manual labor my agent is designed to automate. As a nice bonus, Caldera already includes an Atomic plugin that can ingest these tests as Abilities, so the two tools fit together naturally.
-->

---
layout: default
---

# Provisioning types

<div class="grad-rule"></div>

<div class="cards c2" style="gap:1rem;">
<div class="card edge-blue">
<h3>Platform: container today, VM-ready</h3>
<p>Targets are provisioned as <strong>Docker containers</strong> — enough to emulate hosts, services &amp; networks on a laptop. The design carries an explicit <strong>platform</strong> attribute so a <strong>virtual machine</strong> backend can be added later <strong>without redesign</strong>.</p>
</div>
<div class="card edge-purple">
<h3>Two sub-phases: online build vs. offline isolated</h3>
<ul>
<li><strong>5.1 Build (online):</strong> a controlled egress window installs prerequisites (apt / pip / wget / git).</li>
<li><strong>5.2 Runtime (isolated):</strong> re-isolate, <strong>positively verify egress is gone</strong>, then apply offline setup, load Abilities, isolation gate.</li>
</ul>
</div>
</div>

<div class="card flat edge-red" style="margin-top:1rem;">
<h3>The egress window — the containment invariant</h3>
<p>The build window and the attack window are <strong>mutually exclusive</strong>: a target is <strong>never</strong> simultaneously connected to the internet <em>and</em> attached to the attack networks. Loss of egress is <strong>positively verified</strong> (a reachability probe that must fail), never assumed — <strong>fail-closed</strong>.</p>
<div style="margin-top:.4rem;">
<span class="pill blue">attach egress net</span> → <span class="pill">install online</span> → <span class="pill">detach egress</span> → <span class="pill red">verify egress gone</span> → <span class="pill purple">attach attack nets</span>
</div>
</div>

<!--
The last building block is provisioning — how we actually stand up the target environment. This sounds mundane, but it is one of the most technically delicate parts of the whole system, precisely because of the containment rule, and it is directly relevant to the environment-emulation half of my agent's job.

Let me cover two dimensions. The first, on the top left, is the platform. Today every target is provisioned as a Docker container. Containers are lightweight enough that we can emulate several hosts, their services, and three separate networks all on a single laptop, which fits our hardware constraints. But we did not hard-code that choice. The design carries an explicit "platform" attribute on the environment model, with container as today's value and virtual machine as a reserved future value. So if a future campaign needs a full VM — say, to emulate something a container cannot — that backend can be added without redesigning the pipeline. This extensibility-by-attribute pattern is a recurring design theme you will see again with the AI agent.

The second dimension, top right, is the one that caused us real pain, and it is the heart of safe provisioning. Many attack techniques need software that is not in the base image — think nmap, or a crypto-miner pulled down with wget, or something installed via apt or pip. Installing that software needs internet access. But our attack networks are internal-only; there is deliberately no internet. Early on, setups simply hung forever trying to reach a mirror that was not reachable. So we split provisioning into two sub-phases. Phase five-point-one is the build phase, and it runs online: we open a controlled egress window and install all the prerequisites. Phase five-point-two is the runtime phase, and it runs isolated: we re-isolate the target, apply the offline setup like creating users and starting services, load the Abilities into Caldera, and run the isolation gate.

And now the invariant that ties it together, on the bottom. The build window and the attack window are mutually exclusive. A target is never, at any instant, simultaneously connected to the internet and attached to the attack networks with the other targets. If it were, you could have a command reaching out to the real internet, which is exactly what we forbid. The sequence is shown at the bottom: attach a temporary egress network, install what you need online, detach that egress network, then — and this is the key safety step — positively verify that egress is actually gone, by probing an external endpoint and requiring that probe to fail. Only after egress is confirmed dead do we attach the attack networks. Crucially, we never assume the internet is gone; we prove it, and if the check is inconclusive we treat it as still-connected and refuse to proceed. That is what "fail-closed" means, and it is the safety philosophy behind the entire lab.
-->

---
layout: default
class: section-divider
---

<div class="section" style="position:absolute;inset:0;"></div>

<div class="absolute top-8 left-14">
  <img src="/sticks-logomarca-modo-claro.png" class="h-10 opacity-90" alt="STICKS" />
</div>

<div class="absolute left-14 right-14" style="top:40%;">
  <div class="kicker">Section 02</div>
  <h1 style="font-size:3rem;">The <span class="grad-text">Six-Stage Pipeline</span></h1>
  <div class="grad-rule" style="width:140px;height:6px;"></div>
  <div style="font-size:1.1rem;color:var(--ink-soft);max-width:48rem;">
    From raw STIX to a measured execution report — one explicit stage at a time, each as Input · Processing · Output
  </div>
</div>

<!--
Now we move into section two, the six-stage pipeline. This is the system as it exists and works today, and it is the backbone that my AI agent will eventually drive.

Here is the promise for this section. I am going to give you an overview of all six stages first, and then I will spend one slide on each stage. For every single stage I will use the exact same three-part structure: what goes in, what happens inside, and what comes out — input, processing, output. I am being disciplined about that structure on purpose. The pipeline is explicitly designed so that each stage is a clean transformation with a well-defined contract, and the output of one stage is the input of the next. That clean contract is not just good engineering; it is what makes the automation proposal in the next section tractable. When I later say "the AI agent replaces the processing of stages three, four, and five," you will know precisely what inputs it receives and what outputs it must produce, because we will have walked through them here.

So think of this section as building the map. By the end of it you will be able to trace a campaign all the way from a raw threat-intelligence bundle, through modeling and curation, into a contained execution, and out to a measured report with a verdict. Let us start with the overview that shows all six stages at once.
-->

---
layout: default
---

# Pipeline overview — six stages, one contract

<div class="grad-rule"></div>

<div class="imgframe" style="height:290px;">
<img src="/images/pipeline-overview.svg" style="object-fit:contain;background:#fff;" alt="Six-stage pipeline" />
</div>

<div class="cards c3" style="margin-top:.7rem;gap:.7rem;">
<div class="card flat edge-blue" style="padding:.5rem .8rem;"><p><strong>Sequential gating</strong> — a stage starts only when the previous one is complete.</p></div>
<div class="card flat edge-purple" style="padding:.5rem .8rem;"><p><strong>Input · Processing · Output</strong> — the same contract on every stage.</p></div>
<div class="card flat edge-red" style="padding:.5rem .8rem;"><p><strong>Origin extension point</strong> — stages 3–5 read curated artifacts today; AI plugs in later.</p></div>
</div>

<!--
This is the whole pipeline on one slide, and it is worth orienting yourself here because the next six slides each zoom into one of these boxes.

Reading left to right, following the gradient from blue to red. Stage one is STIX Base Selection: we choose a version of the ATT&CK knowledge base, download it from MITRE if we do not already have it, and show a dashboard of what is inside. Stage two is Campaign Selection: from that bundle we list the available campaigns, pick one, and extract its attributes. Stage three is DAG Generation: we build the ordered dependency graph of the campaign's techniques — this is where the chaining that Atomic Red Team lacks comes from. Stage four is Operationalization: we turn the graph into concrete, ordered Abilities, and we derive the environment parameters those commands require. Stage five is Environment Provisioning: we stand up the containers, load the Abilities into Caldera, and verify isolation. And stage six is Execution, Measurement, and Report: we run the campaign in the contained lab, measure procedural sufficiency, produce the report, and tear the environment back down.

Three properties cut across all six stages, and they are summarized in the strip below the diagram. First, sequential gating. The stages are strictly ordered: a stage cannot start until the previous one has completed successfully. If you go back and change an earlier choice — say you pick a different campaign — the system invalidates and resets every downstream stage, so you can never end up with inconsistent state. This gating is enforced in the backend over simple stage records, not hard-coded, which is why extending from an earlier five-stage design to six stages was almost mechanical.

Second, and I keep coming back to this, every stage honors the same input-processing-output contract. That uniformity is what you are meant to take away from this section.

Third — and this is the hook into my proposal — look at stages three, four, and five. Today, for our eight curated campaigns, the processing in those three stages simply reads human-curated artifacts from disk. No AI, no network. But the architecture deliberately exposes an "origin" attribute on each of those stages, with a value of "human curation" today and a reserved value of "AI generated" for the future. That single design decision is what lets an AI agent slot into exactly these three stages later without any redesign. So as we walk through the stages, pay special attention to three, four, and five — those are the ones my agent will take over.
-->

---
layout: default
---

# Stage 1 — STIX Base Selection

<div class="grad-rule"></div>

<div class="ipo">
<div class="box in">
<div class="lbl">Input</div>
<ul>
<li>A requested <strong>ATT&amp;CK version</strong> (bundle) — e.g. Enterprise v18</li>
<li>User intent to start a campaign run</li>
</ul>
</div>
<div class="box pr">
<div class="lbl">Processing</div>
<ul>
<li>Check local cache; if absent, <strong>download</strong> from the official MITRE source</li>
<li><strong>Only network egress</strong> in the whole pipeline; validated version, atomic write</li>
<li>Parse the bundle → build a <strong>dashboard</strong> (counts by type, campaigns)</li>
</ul>
</div>
<div class="box out">
<div class="lbl">Output</div>
<ul>
<li>The <strong>STIX bundle on disk</strong> (persistent user cache, outside the repo)</li>
<li>A <strong>bundle dashboard</strong> → becomes the input to Stage 2</li>
</ul>
</div>
</div>

<div class="cards c2" style="margin-top:1rem;gap:1rem;">
<div class="card flat edge-blue"><p><strong>Why first?</strong> Everything downstream — campaigns, techniques, DAGs — is resolved <em>against a specific bundle version</em>. The curated cases live in one known version.</p></div>
<div class="card flat edge-red"><p><strong>Safety note:</strong> the version string is validated against a known list <em>before</em> touching disk or network, closing path/URL injection; a failed download leaves <strong>no partial file</strong>.</p></div>
</div>

<!--
Stage one is STIX Base Selection, and it is the foundation that everything else is resolved against.

Let us use the input-processing-output framing. The input is simple: a requested ATT&CK version — a specific bundle, for example the Enterprise version eighteen — plus the user's intent to begin a campaign run. Why does a version matter? Because ATT&CK evolves; techniques get added, renamed, restructured. Our eight human-curated campaigns were curated against one specific, known version. So before we can do anything meaningful, we have to pin down exactly which version of the knowledge base we are working with. That is the whole job of this stage.

The processing has a few steps. First, the system checks a local cache to see whether that bundle is already present. The bundle is a big file — around fifty megabytes — so we treat it as a runtime artifact, not something checked into the repository. It lives in a persistent cache in the user's profile, outside the repo tree, which keeps the repository small and clean. If the bundle is not there, the system downloads it from the official MITRE source. And here is a detail worth flagging: this download is the single point in the entire six-stage pipeline where we touch the network. Every other stage, in the curated flow, is fully offline. That matters for our security story. The download is also hardened: the version string is validated against a known-good list before we touch the disk or the network at all, which closes off any path-traversal or URL-injection tricks, and the write is atomic so a failed download never leaves a half-written, corrupt file behind. Once the bundle is present, we parse it and build a dashboard — counts of each object type, and the list of campaigns inside.

The output is two things: the STIX bundle sitting on disk in the cache, and that bundle dashboard. And notice the handoff — that output becomes the input to stage two. The dashboard is not just for the human to look at; it is the data stage two consumes to list campaigns.

The two cards at the bottom reinforce the why and the safety. On the left: this stage is first because everything downstream — which campaigns exist, which techniques they use, the DAGs — is only meaningful relative to a specific bundle version. On the right: the safety guarantees I just mentioned, the pre-validation and the atomic write with no partial file. Even in the most benign-looking stage, the containment-and-correctness mindset is already present. With the bundle in hand, we can now pick a campaign, which is stage two.
-->

---
layout: default
---

# Stage 2 — Campaign Selection

<div class="grad-rule"></div>

<div class="ipo">
<div class="box in">
<div class="lbl">Input</div>
<ul>
<li>The <strong>STIX bundle</strong> + dashboard from Stage 1</li>
</ul>
</div>
<div class="box pr">
<div class="lbl">Processing</div>
<ul>
<li>List the bundle's <strong>campaigns</strong>; mark which are <strong>curated</strong></li>
<li>On selection, <strong>extract the campaign's attributes</strong>: techniques, relationships, indicators, infrastructure, malware, metadata</li>
<li>Defensive, offline on the curated path</li>
</ul>
</div>
<div class="box out">
<div class="lbl">Output</div>
<ul>
<li>The <strong>campaign attributes</strong> + associated techniques</li>
<li>→ input to Stage 3 (DAG)</li>
</ul>
</div>
</div>

<div class="card flat edge-purple" style="margin-top:1rem;">
<h3>The 8 human-curated campaigns</h3>
<div>
<span class="pill">APT41 — DUST</span><span class="pill">ATT&amp;CK Campaign C0010</span><span class="pill">ATT&amp;CK Campaign C0026</span><span class="pill">CostaRicto</span><span class="pill">Operation MidnightEclipse</span><span class="pill">Operation Outer Space</span><span class="pill">Salesforce Data Exfiltration</span><span class="pill">ShadowRay</span>
</div>
<p style="margin-top:.5rem;">These eight form the <strong>ground truth</strong> for comparing human vs. future AI curation.</p>
</div>

<!--
Stage two is Campaign Selection. Having fixed the knowledge-base version, we now pick the specific campaign we want to reproduce.

The input is exactly the output of stage one: the STIX bundle on disk plus its dashboard. That is the sequential-gating contract in action — stage two literally cannot start until stage one handed over a valid bundle.

The processing has two parts. First, the system lists the campaigns contained in the bundle, and it marks which of those are curated — meaning one of our eight hand-curated cases is available for it. Then, when the researcher selects a campaign, the system extracts that campaign's attributes from the STIX data. This is real structural modeling: it pulls out the techniques the campaign used, the relationships between objects, the indicators of compromise, the infrastructure, the malware, and the campaign metadata. On the curated path this is defensive and fully offline — we are reading structured data we already have, not calling out anywhere. To give you a sense of scale, for the ShadowRay campaign this extraction yields on the order of ten techniques, nine relationships, fourteen indicators, and a couple each of infrastructure and malware objects.

The output is the set of campaign attributes together with the associated techniques, and once again that output feeds directly into the next stage — stage three, the DAG. At this point we know the "what" of the campaign in rich detail: which techniques, what infrastructure, which indicators. What we still do not have is the order and the executable form — that comes next.

The card at the bottom lists the eight human-curated campaigns, and I want you to register these names because they are central to the research. APT41 DUST, two ATT&CK campaigns C-zero-zero-one-zero and C-zero-zero-two-six, CostaRicto, Operation MidnightEclipse, Operation Outer Space, Salesforce Data Exfiltration, and ShadowRay. These eight were curated by hand by my colleagues. They are the ground truth of the project. When I eventually evaluate the AI agent, these human-curated versions are what I will compare against — the agent's generated curation versus the human's, measured on the same pipeline. So this stage is not only a step in the flow; it is also where our evaluation benchmark is anchored.
-->

---
layout: default
---

# Stage 3 — DAG Generation

<div class="grad-rule"></div>

<div class="ipo">
<div class="box in">
<div class="lbl">Input</div>
<ul>
<li>The <strong>campaign attributes</strong> + techniques (Stage 2)</li>
</ul>
</div>
<div class="box pr">
<div class="lbl">Processing</div>
<ul>
<li>Build the <strong>directed acyclic graph</strong> of techniques</li>
<li>Encode <strong>ordering</strong> and <strong>dependencies</strong> between steps</li>
<li>Curated path: read <code>{slug}_dag.json</code> from disk — <strong>no AI, no network</strong></li>
<li>Carries an explicit <code>origin</code> (<code>human_curation</code> | <code>ai_generated</code>)</li>
</ul>
</div>
<div class="box out">
<div class="lbl">Output</div>
<ul>
<li>The <strong>campaign DAG</strong> (nodes + edges, renderable as Mermaid)</li>
<li>→ input to Stage 4</li>
</ul>
</div>
</div>

<div class="cards c2" style="margin-top:1rem;gap:1rem;">
<div class="card flat edge-red"><p><strong>Why the DAG comes before commands:</strong> the DAG defines the campaign's <em>chaining</em>. Atomic Red Team gives a command <em>per technique</em>, but the <strong>order &amp; dependencies</strong> come from here.</p></div>
<div class="card flat edge-purple"><p><strong>Extension point:</strong> a future <code>AiDagGenerator</code> plugs into the same seam, emitting a DAG with <code>origin = ai_generated</code> — no schema change.</p></div>
</div>

<!--
Stage three is DAG Generation, and this is the first of the three stages that a human curates today and that my AI agent will eventually take over — so pay close attention here.

The input is the campaign attributes and techniques from stage two. We know which techniques the campaign used. The processing turns that flat set of techniques into a structure: a directed acyclic graph, a DAG. In that graph each node is a technique or step, and the edges encode two things — the order in which steps must run, and the dependencies between them. "Directed" means the edges have a direction, step A then step B. "Acyclic" means there are no loops; the campaign flows forward. This is the stage that answers the question Atomic Red Team could not: not just which techniques, but in what sequence and with what prerequisites.

On the curated path, the processing is deliberately trivial: the curated graph already exists, so the system just reads a DAG JSON file for that campaign from disk, with no AI and no network involved. But — and this is the important architectural detail — the DAG it produces always carries an explicit origin field. Today that value is "human curation." The field also admits the value "AI generated." Nothing else in the data model has to change for an AI-produced DAG to be a first-class citizen.

The output is the campaign DAG itself — nodes and edges — which we can even render visually as a Mermaid diagram on the screen, and that DAG becomes the input to stage four.

The two cards explain the two things people most often ask here. On the left: why does the DAG come before the commands? Because the DAG is where the campaign's chaining lives. Remember, Atomic Red Team hands you a command per technique in isolation. It is the DAG that says "do reconnaissance, then exploit, then move laterally, then exfiltrate," and that step four depends on step two having succeeded. The command catalog does not know about campaign-level ordering; the DAG does. So logically, you establish the structure first, and only then fill in the concrete commands — which is stage four.

On the right: the extension point. Because the DAG generator is a clean seam with an origin attribute, a future AI DAG generator slots in right here. It would take the campaign attributes and emit a DAG with origin equal to AI-generated, using the same interface and the same stored structure. This is the first of the three places my agent plugs in, and you can already see how the architecture was prepared for it.
-->

---
layout: default
---

# Stage 4 — Operationalization

<div class="grad-rule"></div>

<div class="ipo">
<div class="box in">
<div class="lbl">Input</div>
<ul>
<li>The <strong>campaign DAG</strong> (Stage 3)</li>
</ul>
</div>
<div class="box pr">
<div class="lbl">Processing</div>
<ul>
<li>Assemble the <strong>executable Abilities</strong> (command per executor) in <strong>DAG order</strong> + the Adversary profile</li>
<li><strong>Derive environment parameters</strong> from the DAG nodes: target hosts, addresses, services, vulnerabilities, network context</li>
</ul>
</div>
<div class="box out">
<div class="lbl">Output — two typed artifacts</div>
<ul>
<li><strong>Ordered Abilities</strong> + Adversary</li>
<li><strong>Environment parameters</strong> → drive Stage 5</li>
</ul>
</div>
</div>

<div class="cards c2" style="margin-top:1rem;gap:1rem;">
<div class="card flat edge-purple"><p><strong>This is the command-semantics gap.</strong> Selecting the right command per technique, parametrizing it for the target, and sequencing it — today curated by hand from sources like Atomic Red Team.</p></div>
<div class="card flat edge-blue"><p><strong>The command dictates the environment.</strong> A technique's <code>dependencies</code>, <code>input_arguments</code> (targets) and <code>supported_platforms</code> <em>are</em> the environment parameters Stage 5 consumes.</p></div>
</div>

<!--
Stage four is Operationalization, and this is the beating heart of the command-semantics half of my work. If stage three gave us the skeleton — the order and dependencies — stage four puts the flesh on it: the actual runnable commands.

The input is the campaign DAG from stage three. The processing produces two distinct things, and I want to be precise that they are two separate, typed artifacts, because they feed two different places.

The first artifact is the set of executable Abilities. For each node in the DAG, we assemble a concrete Ability — the actual command, tied to an executor — and we arrange them in DAG order. We also produce the Adversary profile, which is Caldera's bundling of those ordered Abilities. This is the stage where "Technique: credential dumping" finally becomes "run this exact command with these exact arguments."

The second artifact is the environment parameters. This is subtle and important. As we look at each DAG node, we read fields that are embedded in the curated data — things like the attacker commands, the target vulnerable hosts, the source and target host types, and the network context. From those we derive a structured description of what the environment must provide: which target hosts exist, their addresses, which container each maps to, what services and vulnerabilities they need, and notes about the network. So stage four does not only produce commands; it produces a specification of the battlefield those commands require.

The two cards crystallize why this stage matters. On the left: this is the command-semantics gap in its purest form. Selecting the right command for each technique, parametrizing it for the specific target, and sequencing it correctly — that is exactly the expert, manual work. Today a human does it, drawing on sources like Atomic Red Team. This is the single biggest chunk of what my AI agent will automate.

On the right is the insight that ties stage four to stage five, and back to the Atomic Red Team slide. The command dictates the environment. Remember those three fields I asked you to hold onto — dependencies, input arguments, and supported platforms? Those fields are, almost literally, the environment parameters. A command's dependencies tell you what must be installed; its input arguments tell you which targets it hits; its supported platforms tell you what kind of host it needs. So the act of operationalizing the commands naturally produces the requirements for the environment. That is why operationalization comes before provisioning: you cannot build the right environment until you know what the commands demand of it. And that neatly sets up stage five.
-->

---
layout: default
---

# Stage 5 — Environment Provisioning

<div class="grad-rule"></div>

<div class="ipo">
<div class="box in">
<div class="lbl">Input</div>
<ul>
<li>The <strong>environment parameters</strong> (Stage 4)</li>
<li>The <strong>ordered Abilities</strong> + Adversary</li>
</ul>
</div>
<div class="box pr">
<div class="lbl">Processing</div>
<ul>
<li><strong>Provision</strong> the target containers (online build → re-isolate → verify egress gone)</li>
<li><strong>Load Abilities</strong> into Caldera in DAG order</li>
<li><strong>Isolation gate</strong> — read-only, fail-closed verification</li>
</ul>
</div>
<div class="box out">
<div class="lbl">Output</div>
<ul>
<li>A <strong>ready, isolated environment</strong> with Abilities loaded</li>
<li>→ unlocks Stage 6</li>
</ul>
</div>
</div>

<div class="cards c2" style="margin-top:1rem;gap:1rem;">
<div class="card flat edge-red"><p><strong>Isolation is the completion condition.</strong> If any target container is not isolated, Stage 5 does <em>not</em> complete and Stage 6 stays locked. Empty list or dead daemon → fail.</p></div>
<div class="card flat edge-purple"><p><strong>The environment-emulation gap.</strong> Designing &amp; building the right target environment for a campaign is the second thing the AI agent automates — via the same <code>origin</code> seam.</p></div>
</div>

<!--
Stage five is Environment Provisioning, and it is the environment-emulation half of my agent's job — the second of the three stages the agent will take over.

The input is the two artifacts stage four produced: the environment parameters, which specify what the battlefield must look like, and the ordered Abilities plus Adversary, which are what will actually run on it.

The processing is where the provisioning discipline from the building-blocks section comes to life. First we provision the target containers, and we do it with the two-sub-phase dance I described earlier: bring them up with a controlled online build window to install prerequisites, then re-isolate them and positively verify that internet egress is gone. Second, we load the Abilities into Caldera, in the DAG order, so the engine knows the campaign and its sequence. Third — and this is the gate — we run a read-only isolation check on every target container.

That isolation gate, on the bottom-left card, is the completion condition for the stage, and this is a deliberate and strong design choice. Stage five does not count as complete until every target container is verified isolated. If any container is not isolated, the stage does not complete, the offending container is reported, and stage six — the actual execution — stays locked. The check is fail-closed: if the container list is empty, or the Docker daemon is unreachable, or the result is in any way inconclusive, it fails rather than optimistically passing. In other words, we would rather refuse to run than risk running against something that might not be contained. This is the structural guarantee that an attack can never execute outside a verified-isolated environment.

The output is a ready, isolated environment with the Abilities loaded and the isolation confirmed — and that unlocks stage six.

The bottom-right card names the research significance. Designing and building the right target environment for a given campaign — figuring out which hosts, which services, which vulnerabilities, wired into which networks — is genuinely hard expert work, and it is the second major thing my AI agent automates. And it plugs in through the very same origin seam as the DAG and the operationalization: today a human-curation provisioner uses the existing lab compose file; tomorrow an AI provisioner, with origin AI-generated, could design a bespoke environment. Same interface, no redesign. So across stages three, four, and five you have now seen all three places the agent will take over — the structure, the commands, and the environment. Stage six is where we find out whether all of that actually works.
-->

---
layout: default
---

# Stage 6 — Execution, Measurement &amp; Report

<div class="grad-rule"></div>

<div class="ipo">
<div class="box in">
<div class="lbl">Input</div>
<ul>
<li>The <strong>isolated environment</strong> + loaded Abilities (Stage 5)</li>
</ul>
</div>
<div class="box pr">
<div class="lbl">Processing — 4 gates, then run</div>
<ul>
<li>① explicit confirm · ② <strong>containment</strong> (no external target) · ③ <strong>isolation</strong> · ④ Caldera up</li>
<li>Run in DAG order; track live over WebSocket; <strong>1:1 audit</strong> per command</li>
<li><strong>Measure</strong> procedural sufficiency; then <strong>deprovision</strong></li>
</ul>
</div>
<div class="box out">
<div class="lbl">Output</div>
<ul>
<li>Consolidated <strong>report</strong>: success rate, per-technique telemetry, failed nodes, <strong>verdict</strong></li>
<li>Environment torn down</li>
</ul>
</div>
</div>

<div class="cards c2" style="margin-top:1rem;gap:1rem;">
<div class="card flat edge-blue"><p><strong>Procedural sufficiency</strong> = did the curated procedure actually reproduce the campaign? Success rate over DAG, per-technique status/telemetry, failed nodes, overall verdict.</p></div>
<div class="card flat edge-red"><p><strong>Always tears down.</strong> After measuring, the environment is deprovisioned; remaining resources are reported. This is the measurable signal the AI agent will optimize against.</p></div>
</div>

<!--
Stage six is Execution, Measurement, and Report — the payoff stage, where the whole pipeline either proves itself or shows us exactly where it falls short.

The input is the isolated, verified environment with the Abilities loaded from stage five. The processing is the most safety-critical in the system, so it is built as a series of gates that must all pass before a single command runs. Gate one: explicit human confirmation — the researcher sees the exact commands and their targets in a preview and must confirm. Gate two: containment — every command's target is parsed and validated, and if any points outside the internal subnets the entire run is refused. Gate three: isolation — the read-only check that the targets are still isolated. Gate four: Caldera must be up and healthy. Only when all four gates pass does execution begin.

Then the campaign runs, in DAG order, through Caldera. Progress is tracked live and streamed to the interface over a WebSocket, so you watch each technique execute in real time. Every executed command writes exactly one audit record — that one-to-one trail again. When execution finishes, the system measures procedural sufficiency, and then — importantly — it deprovisions, tearing the environment down.

The bottom-left card defines the central metric, procedural sufficiency, and this is the scientific core of the whole platform. The question it answers is: did the curated procedure actually succeed in reproducing the campaign? It is computed as a success rate over the DAG — how many of the campaign's techniques executed successfully — together with per-technique status and telemetry, a list of which nodes failed, and an overall verdict. This is how we turn the abstract notion of "the procedural gap" into a hard number. A high sufficiency means the curation captured enough of the real procedure to reproduce the behavior; a low one, or specific failed nodes, tells us precisely where the procedural knowledge was missing.

The bottom-right card notes two things. First, the stage always tears down the environment after measuring, and if any resource cannot be removed it is reported rather than silently left running — clean state is part of safety. Second, and this is the hook into the next section: that verdict, that success rate, is the measurable signal my AI agent will optimize against. When the agent generates a curation and it only reaches, say, seventy percent sufficiency, the agent can read which nodes failed and why, and iterate. So stage six is not just the end of the pipeline; it is the feedback source that makes an automated, self-improving agent possible. And that is the perfect moment to turn to my proposal.
-->

---
layout: default
class: section-divider
---

<div class="section" style="position:absolute;inset:0;"></div>

<div class="absolute top-8 left-14">
  <img src="/sticks-logomarca-modo-claro.png" class="h-10 opacity-90" alt="STICKS" />
</div>

<div class="absolute left-14 right-14" style="top:40%;">
  <div class="kicker">Section 03</div>
  <h1 style="font-size:3rem;">The Gap &amp; <span class="grad-text">My Proposal</span></h1>
  <div class="grad-rule" style="width:140px;height:6px;"></div>
  <div style="font-size:1.1rem;color:var(--ink-soft);max-width:48rem;">
    From manual curation to an AI agent that generates the procedure <em>and</em> the environment
  </div>
</div>

<!--
This is section three, and it is the reason we are all here tonight: the gap, and my proposal to close it with an AI agent.

Everything up to now has been setup. You understand what ATT&CK gives us and what it withholds. You understand the lab and the containment rules. You understand Atomic Red Team as a catalog of per-technique commands. And you have walked the full six-stage pipeline, stage by stage, with its clean input-processing-output contract. Most importantly, you have seen that stages three, four, and five share a common "origin" extension point that today reads "human curation."

In this section I am going to do four things. First, I will make brutally explicit what a human has to do by hand today — the manual gap — so the problem is concrete, not abstract. Second, I will state my proposal: an AI agent that fills that gap automatically. Third, I will show you the architecture of the agentic environment that drives the whole pipeline. And fourth, I will break down each component of the agent so you can see how the pieces map onto the stages you already know.

This is where my individual contribution lives, as distinct from the foundation my colleagues and I built together. So let us start by being honest and specific about exactly how much human effort the current approach demands.
-->

---
layout: default
---

# The manual gap today

<div class="grad-rule"></div>

<div class="card edge-red" style="margin-bottom:.9rem;">
<p>For the 8 curated campaigns, stages <strong>3, 4 and 5</strong> run on artifacts a human analyst produced <strong>by hand</strong>. That expert labor — done once per campaign, per environment — is the bottleneck this research removes.</p>
</div>

<div class="cards c3" style="gap:.9rem;">
<div class="card edge-purple">
<h3>Stage 3 — DAG, by hand</h3>
<p>The analyst reasons out the <strong>order &amp; dependencies</strong> of the campaign's techniques and writes the DAG.</p>
</div>
<div class="card edge-purple">
<h3>Stage 4 — Commands, by hand</h3>
<p>For each technique, the analyst <strong>selects, parametrizes &amp; sequences</strong> a concrete command (e.g. from Atomic Red Team) and derives the environment needs.</p>
</div>
<div class="card edge-purple">
<h3>Stage 5 — Environment, by hand</h3>
<p>The analyst <strong>designs the target hosts, services &amp; vulnerabilities</strong> and wires them into the isolated lab.</p>
</div>
</div>

<div class="cards c2" style="margin-top:.9rem;gap:1rem;">
<div class="card flat edge-blue"><p><strong>Slow &amp; expert-bound</strong> — deep knowledge of ATT&amp;CK, the campaign, tooling and networking. Does not scale to many campaigns.</p></div>
<div class="card flat edge-red"><p><strong>The two faces of the gap</strong> — command semantics (Stage 4) <em>and</em> environment emulation (Stage 5), with structure (Stage 3) on top.</p></div>
</div>

<!--
Let me make the manual gap concrete, because it is easy to say "a human curates it" and gloss over just how much skilled work that sentence hides.

For each of the eight curated campaigns, stages three, four, and five run entirely on artifacts that a human analyst produced by hand. My colleagues — Roth and Sidnei — did this work. Let me spell out what "by hand" actually means for each stage.

In stage three, the analyst has to reason out the campaign's structure. Given a set of techniques, in what order did this specific APT actually use them, and which steps depend on which? Getting that wrong means the emulation runs steps in an impossible order. The analyst encodes that reasoning as the DAG, by hand.

In stage four, for every technique in that DAG, the analyst has to select a concrete command that performs it — often drawing on Atomic Red Team — then parametrize it for our specific targets, our specific IP addresses and hosts, and sequence it. And while doing so, they have to work out what the environment must provide for each command to succeed. This is the densest, most expertise-heavy work in the whole process.

In stage five, the analyst designs the environment itself: which target hosts need to exist, what services they must run, what vulnerabilities they must expose for the campaign's techniques to land, and how to wire all of that into the isolated lab's networks. 

Step back and look at the bottom cards. First, this is slow and expert-bound. It demands deep, simultaneous knowledge of ATT&CK, of the particular campaign, of offensive tooling, and of networking. Very few people can do it well, and even they do it slowly. It simply does not scale — reproducing dozens or hundreds of campaigns by hand is not realistic.

Second — and this is the framing I want you to carry into the proposal — the gap has two faces, plus structure on top. There is the command-semantics face, which is stage four: turning techniques into runnable, parametrized, ordered commands. And there is the environment-emulation face, which is stage five: building the target environment those commands attack. Sitting over both is the structural reasoning of stage three, the DAG. My proposal, which is the next slide, is an AI agent that takes over all three.
-->

---
layout: default
---

# Proposal — the AI curation agent

<div class="grad-rule"></div>

<div class="two-col">
<div class="tight">

<div class="card edge-grad">
<h3>The idea</h3>
<p>Replace the <strong>human-curation</strong> implementation of stages 3–5 with <strong>AI-generated</strong> curation — through the <code>origin</code> extension point the pipeline already exposes.</p>
<div style="margin-top:.4rem;">
<code>origin: human_curation</code> &nbsp;→&nbsp; <code>origin: ai_generated</code>
</div>
</div>

<div class="card edge-blue">
<h3>What the agent produces</h3>
<ul>
<li><strong>Stage 3:</strong> the campaign DAG (structure)</li>
<li><strong>Stage 4:</strong> the executable, parametrized commands (<strong>command semantics</strong>)</li>
<li><strong>Stage 5:</strong> the target environment design (<strong>environment emulation</strong>)</li>
</ul>
</div>

<div class="card edge-red">
<h3>No pipeline redesign</h3>
<p>Stages 1, 2 and 6 are unchanged. The agent swaps in behind the same seams; <strong>safety gates stay exactly as they are</strong>.</p>
</div>

</div>

<div>
<div class="imgframe" style="height:200px;">
<img src="/images/code-screen.jpg" alt="code" />
</div>
<div class="caption">Image: “Computer coding on a screen”, rawpixel — CC0, via Openverse.</div>
<div class="card flat edge-purple" style="margin-top:.7rem;">
<h3>The evaluation payoff</h3>
<p>Run the agent on the same 8 campaigns and <strong>measure procedural sufficiency</strong> (Stage 6) — a direct, quantitative <strong>AI-vs-human</strong> comparison.</p>
</div>
</div>

</div>

<!--
Here is the proposal, stated as plainly as I can.

The idea, in the top-left card, is this: replace the human-curation implementation of stages three, four, and five with an AI-generated implementation. And the beautiful part — the part that makes this feasible rather than a rewrite — is that the pipeline already exposes the exact seam for it. Every one of those three stages carries an origin attribute. Today it reads "human curation." My agent provides the "AI generated" implementation behind the same interfaces. In code terms, it is flipping origin from human_curation to ai_generated and supplying the generator that produces the artifacts.

What does the agent actually produce? The middle card lists it, and it maps one-to-one onto the three faces of the gap we just discussed. At stage three, the agent produces the campaign DAG — the structure, the order and dependencies. At stage four, it produces the executable, parametrized commands — this is the command-semantics half. And at stage five, it produces the target environment design — this is the environment-emulation half. So the agent closes both faces of the gap, plus the structural reasoning on top. That is the complete scope of my contribution.

The bottom-left card makes an important promise: no pipeline redesign. Stages one, two, and six do not change at all. Stage one still selects and downloads the bundle; stage two still extracts campaign attributes; stage six still executes, measures, and tears down — and critically, all of its safety gates stay exactly as they are. The AI agent is not trusted to bypass containment. Everything it generates still passes through the same preview, the same confirmation, the same containment and isolation gates, the same audit. The agent proposes; the safety machinery disposes. That separation is deliberate: we get automation without weakening a single security guarantee.

And the right side is the scientific payoff, which is really why this is a research project and not just an engineering convenience. Because stage six gives us a quantitative measure of procedural sufficiency, we can run the agent on the very same eight campaigns that humans curated, and directly compare the numbers. Does AI-generated curation reach the same success rate as expert human curation? Where does it fall short, and on which kinds of techniques? That head-to-head, AI-versus-human, measured on an identical pipeline, is the result the research is after. Now let me show you how the agent is actually built — the architecture.
-->

---
layout: default
---

# AI environment architecture

<div class="grad-rule"></div>

<div class="imgframe" style="height:360px;">
<img src="/images/ai-architecture.svg" style="object-fit:contain;background:#fff;" alt="AI agent architecture" />
</div>

<div style="margin-top:.5rem;font-size:.82rem;color:var(--ink-soft);">
Knowledge &amp; tools feed an <strong>orchestrated agent</strong> whose per-stage sub-agents produce the DAG, the commands and the environment; Stage 6's verdict <strong>feeds back</strong> to refine them — all under fixed safety guardrails.
</div>

<!--
This is the architecture of the agentic environment that will drive the whole pipeline. Let me read it left to right, because it tells a story.

On the left are the knowledge and tool sources — the raw material the agent reasons over. There is the STIX and ATT&CK threat intelligence, which gives the campaign's techniques, relationships, and attributes. There is Atomic Red Team, the catalog of per-technique commands and their prerequisites. There is the lab catalog and compose definition, which describes the available hosts, services, and internal networks. And there are the eight human-curated cases, which serve as ground truth — both as examples the agent can learn from or be evaluated against.

In the center is the agent environment itself. At the top sits an orchestrator, an LLM-driven planner. Its job is to drive the stages and call tools, while respecting the same input-processing-output contract the pipeline defines. Below it are four specialized sub-agents, and notice how they line up exactly with the stages you already know. The DAG agent handles stage three — it orders the techniques into a dependency graph, emitting origin "AI generated." The operationalization agent handles stage four — it selects and parametrizes commands from Atomic Red Team and derives the environment parameters; this is the command-semantics gap. The environment agent handles stage five — it designs and provisions the target environment; this is the environment-emulation gap. And the measurement loop handles the feedback from stage six — it reads the verdict and telemetry, and feeds failures back to refine the DAG, the commands, or the environment.

Underneath those sub-agents is a shared layer of deterministic tools — the STIX parser, the Atomic Red Team lookup, the Docker and compose driver, the Caldera API, and the command parser. The agents reason and plan, but the actual actions go through these deterministic, injectable tools, which are read-only wherever they inspect. That keeps the risky operations predictable and testable, rather than letting the LLM free-form its way into the system.

And framing the entire center block, at the bottom, are the safety guardrails, which are non-negotiable and unchanged from the existing pipeline: every generated command and target is validated against the internal subnets, isolation is verified read-only and fail-closed before any execution, there is explicit preview and confirmation, a one-to-one audit, and the agent never, ever runs on the host.

On the right is execution and evaluation: the isolated Docker lab where the campaign runs, the procedural-sufficiency measurement, and the evaluation goal — comparing AI curation against the eight human cases on the same measurable pipeline. The dashed arrow curving back is the feedback loop: the verdict informs the next iteration. That loop is what makes this an agent rather than a one-shot generator. Let me now zoom into the individual components.
-->

---
layout: default
---

# Agent components — structure &amp; commands

<div class="grad-rule"></div>

<div class="cards c2" style="gap:1rem;">

<div class="card edge-purple">
<h3>DAG-generation agent · Stage 3</h3>
<p><strong>Input:</strong> campaign attributes + techniques (from Stage 2).</p>
<p><strong>Tools:</strong> STIX/ATT&amp;CK reader, technique knowledge.</p>
<p><strong>Reasoning:</strong> infer the <strong>ordering &amp; dependencies</strong> of the campaign's techniques — the structure Atomic Red Team lacks.</p>
<p><strong>Output:</strong> a campaign DAG with <code>origin = ai_generated</code>, drop-in behind the <code>DagGenerator</code> seam.</p>
</div>

<div class="card edge-purple">
<h3>Operationalization agent · Stage 4</h3>
<p><strong>Input:</strong> the DAG (from Stage 3).</p>
<p><strong>Tools:</strong> Atomic Red Team lookup, command parser, executor catalog.</p>
<p><strong>Reasoning:</strong> per node — <strong>select</strong> the command, <strong>parametrize</strong> it for the target, place it in <strong>DAG order</strong>; then <strong>derive the environment parameters</strong>.</p>
<p><strong>Output:</strong> ordered Abilities + Adversary <em>and</em> the environment-parameter spec — the <strong>command-semantics gap, automated</strong>.</p>
</div>

</div>

<div class="card flat edge-blue" style="margin-top:1rem;">
<p>Both components slot into the <strong>existing seams</strong> (<code>DagGenerator</code>, <code>OperationalizerByOrigin</code>) — the pipeline, UI and safety gates consume their output exactly as they consume human curation today.</p>
</div>

<!--
Let me zoom into the first two components of the agent — the ones that handle structure and commands, stages three and four.

Start with the DAG-generation agent, on the left, which owns stage three. Its input is the campaign attributes and techniques that stage two extracted. Its tools are a STIX and ATT&CK reader plus technique knowledge — essentially the ability to look up what each technique is and how techniques typically relate. Its reasoning task is to infer the ordering and dependencies among the campaign's techniques: given that this APT used these ten techniques, in what sequence did they realistically occur, and which ones are prerequisites for which others? This is genuine reasoning, because the raw threat intelligence rarely states the order explicitly — a human infers it, and now the agent must. Its output is a campaign DAG carrying origin "AI generated," and because it conforms to the existing DAG-generator interface, it is a true drop-in: the rest of the pipeline cannot tell, structurally, whether a human or the agent produced it.

Now the operationalization agent, on the right, which owns stage four — and this is the richest component, because it is the command-semantics gap made automatic. Its input is the DAG from stage three. Its tools are the Atomic Red Team lookup, a command parser, and the executor catalog. Its reasoning runs per node of the DAG and has three moves. First, select: choose the right command that implements this node's technique, typically from Atomic Red Team. Second, parametrize: fill in the specifics for our target — the actual IP address, the actual hostname, the actual file path. Third, order: place it correctly in the DAG sequence. And then, as a fourth move, it derives the environment parameters from those commands — recall that a command's dependencies, input arguments, and supported platforms essentially are the environment requirements. Its output is therefore the same pair of artifacts the human produces: the ordered Abilities and Adversary, plus the environment-parameter specification. This single component automates the densest expert labor in the entire process.

The card at the bottom is the reassurance that this is engineering-feasible, not hand-waving. Both of these agents slot into seams that already exist in the codebase — the DAG-generator interface and the operationalizer-by-origin interface. That means the pipeline, the user interface, and crucially the safety gates all consume the agent's output through exactly the same path they use for human curation today. Nothing downstream has to change or be re-validated. The next slide covers the remaining two components — the environment agent and the feedback loop — plus the guardrails they all operate under.
-->

---
layout: default
---

# Agent components — environment &amp; feedback

<div class="grad-rule"></div>

<div class="cards c2" style="gap:1rem;">

<div class="card edge-red">
<h3>Environment-provisioning agent · Stage 5</h3>
<p><strong>Input:</strong> the environment parameters (from Stage 4).</p>
<p><strong>Tools:</strong> Docker/compose driver, health &amp; isolation inspectors.</p>
<p><strong>Reasoning:</strong> design the target hosts, services &amp; vulnerabilities; choose the online-build vs. offline-isolated split; provision &amp; load Abilities.</p>
<p><strong>Output:</strong> an isolated environment with <code>origin = ai_generated</code> — the <strong>environment-emulation gap, automated</strong>.</p>
</div>

<div class="card edge-blue">
<h3>Measurement &amp; feedback loop · Stage 6</h3>
<p><strong>Input:</strong> the execution report — success rate, failed nodes, per-technique telemetry, verdict.</p>
<p><strong>Reasoning:</strong> diagnose <em>why</em> a node failed → refine the DAG, a command, or the environment, and re-run.</p>
<p><strong>Output:</strong> an improved curation — iterating toward <strong>procedural sufficiency</strong>.</p>
</div>

</div>

<div class="card flat edge-grad" style="margin-top:1rem;">
<h3>Shared guardrails — the agent proposes, the safety machinery disposes</h3>
<p>Every agent output still passes the <strong>containment</strong> check, the <strong>read-only fail-closed isolation</strong> gate, the <strong>explicit preview + confirmation</strong>, and the <strong>1:1 audit</strong>. The agent <strong>never runs on the host</strong> and never bypasses a gate. Automation is added <strong>without weakening any security guarantee</strong>.</p>
</div>

<!--
Here are the last two components of the agent — the environment agent and the measurement-and-feedback loop — and then the guardrails they all live under.

On the left is the environment-provisioning agent, which owns stage five and automates the environment-emulation half of the gap. Its input is the environment-parameter specification that the operationalization agent produced. Its tools are the Docker and compose driver and the health and isolation inspectors. Its reasoning task is to translate those parameters into an actual target environment: decide which hosts must exist, which services and vulnerabilities each must expose so the campaign's techniques can land, and then choose the provisioning strategy — in particular the online-build versus offline-isolated split we discussed, deciding what has to be installed during the egress window and what can be configured offline. It then provisions the environment and loads the Abilities. Its output is an isolated, ready environment tagged origin "AI generated." This is the component that frees a human from hand-designing the battlefield for every campaign.

On the right is the measurement-and-feedback loop, which closes the cycle using stage six. Its input is the execution report — the success rate, the list of failed nodes, the per-technique telemetry, and the overall verdict. Its reasoning is diagnostic: for a node that failed, why did it fail? Was the command wrong or mis-parametrized? Was a dependency missing, which is really a stage-four problem? Did the environment lack a service the technique needed, which is a stage-five problem? Based on that diagnosis, the agent refines the relevant artifact — the DAG, a command, or the environment — and re-runs. Its output is an improved curation, iterating toward higher procedural sufficiency. This loop is exactly what elevates the system from a one-shot generator to a genuine agent: it uses the measurable verdict as a reward signal and improves.

And now the frame around all four components, the shared guardrails, which I have stressed throughout and will state one final time because it is the ethical and technical heart of doing this safely. The agent proposes; the safety machinery disposes. Every single thing the agent generates — every DAG, every command, every environment — still passes through the unchanged safety gates: the containment check that rejects any external target, the read-only fail-closed isolation gate, the explicit preview and human confirmation, and the one-to-one audit. The agent never runs on the host and can never bypass a gate. The result is that we add powerful automation without weakening a single security guarantee. That is a deliberate design stance: an autonomous agent generating attacks is only acceptable if it is boxed inside a containment system it cannot override — and it is. With the proposal fully laid out, let me close by showing what is already proven and where this goes next.
-->

---
layout: default
class: section-divider
---

<div class="section" style="position:absolute;inset:0;"></div>

<div class="absolute top-8 left-14">
  <img src="/sticks-logomarca-modo-claro.png" class="h-10 opacity-90" alt="STICKS" />
</div>

<div class="absolute left-14 right-14" style="top:40%;">
  <div class="kicker">Section 04</div>
  <h1 style="font-size:3rem;">Evidence &amp; <span class="grad-text">Close</span></h1>
  <div class="grad-rule" style="width:140px;height:6px;"></div>
  <div style="font-size:1.1rem;color:var(--ink-soft);max-width:48rem;">
    What is already proven · the roadmap to the AI agent · questions
  </div>
</div>

<!--
We have reached the final section: evidence and close. I have told you about the problem, the building blocks, the pipeline, and my proposed AI agent. Now I want to ground all of that in what is actually built and proven, so this does not come across as a purely theoretical plan.

So in this short closing section I will do three things. First, I will show you the concrete evidence — the foundation that already works end to end today, including a real emulation we executed, with numbers. Second, I will lay out the roadmap: where we are now, and the path from this proven foundation to the fully realized AI agent. And third, we will open the floor for questions.

The reason the evidence matters is that my proposal is not a greenfield fantasy. It rests on a working platform. The pipeline exists, the containment works, and we have already driven a real APT campaign through it to completion inside the isolated lab. That foundation is precisely what makes the AI agent an incremental, well-scoped next step rather than a leap into the unknown. Let me show you the proof.
-->

---
layout: default
---

# What is proven today

<div class="grad-rule"></div>

<div class="stats" style="margin-bottom:1rem;">
<div class="stat"><div class="n grad-text">6</div><div class="l">pipeline stages, working end to end</div></div>
<div class="stat"><div class="n grad-text">8</div><div class="l">curated campaigns integrated</div></div>
<div class="stat"><div class="n grad-text">11</div><div class="l">ShadowRay techniques executed — real lab</div></div>
<div class="stat"><div class="n grad-text">PASSED</div><div class="l">real end-to-end emulation verdict</div></div>
</div>

<div class="cards c2" style="gap:1rem;">
<div class="card edge-blue">
<h3>The milestone: real ShadowRay E2E</h3>
<p>The full ShadowRay campaign ran <strong>for real</strong> in the contained lab — 11 Abilities executed against the live targets, <strong>0 failures</strong>, 1:1 audit trail persisted. The pipeline produced a measured result, not a mock.</p>
</div>
<div class="card edge-red">
<h3>Engineering &amp; correctness</h3>
<ul>
<li><strong>188</strong> backend tests · <strong>17</strong> frontend tests</li>
<li><strong>12</strong> property-based correctness properties (incl. the security invariants, hundreds of iterations each)</li>
<li>Isolated lab: <strong>4</strong> containers, 3 internal /24 networks, preflight conflict check</li>
</ul>
</div>
</div>

<!--
This is the evidence, and I want to walk through the numbers because they are the proof that the foundation is real.

Look at the four figures across the top. Six: the pipeline has all six stages working end to end — not on paper, not mocked, but actually running. Eight: all eight human-curated campaigns are integrated and selectable. Eleven: the number of techniques from the ShadowRay campaign that executed in the real lab. And PASSED: the verdict of the real end-to-end emulation.

Let me expand on the milestone, on the bottom-left, because it is the single most important result so far. ShadowRay is one of the eight curated campaigns. We brought up the full isolated lab and ran the entire ShadowRay campaign for real — not against mocks, but against live target containers, driven by Caldera. All eleven Abilities executed in the correct DAG order, with zero failures, and the one-to-one audit trail was persisted to the database. This was the project's last remaining foundational milestone, and clearing it means the pipeline genuinely produces a measured procedural-sufficiency result from a real contained execution. That is the capability my AI agent will build on — the agent generates the curation, and this same proven machinery executes and measures it.

On the bottom-right is the engineering and correctness evidence, because a security platform has to be trustworthy, not just functional. There are one hundred eighty-eight automated backend tests and seventeen frontend tests. More interesting for a safety-critical system, there are twelve property-based correctness properties, verified with hundreds of randomized iterations each. Those properties include the security invariants themselves — for example: no emulation ever starts against an external target; execution proceeds only when all targets are confirmed isolated; execution requires explicit confirmation; the audit is strictly one-to-one; and an audit failure aborts the operation. We did not just test these on a few examples — we proved them to hold across hundreds of generated scenarios. And the isolated lab is the four-container topology on three internal class-C networks, with a preflight check that refuses to bring the lab up if any of its subnets is already in use on the machine, so an attack packet can never be routed somewhere unexpected.

Taken together: the pipeline works, a real APT campaign has been reproduced and measured inside containment, and the safety guarantees are verified, not assumed. That is the solid ground my proposal stands on.
-->

---
layout: default
---

# Roadmap &amp; next steps

<div class="grad-rule"></div>

<div class="cards c3" style="gap:.9rem;">
<div class="card edge-blue">
<h3>✓ Phase 1 — Foundation <span class="pill blue">done</span></h3>
<ul>
<li>Pipeline UI + backend + containment</li>
<li>6 stages working end to end</li>
<li>Real ShadowRay emulation proven</li>
</ul>
</div>
<div class="card edge-purple">
<h3>▸ Phase 2 — AI curation agent <span class="pill purple">my work</span></h3>
<ul>
<li>DAG agent (Stage 3)</li>
<li>Operationalization agent (Stage 4)</li>
<li>Environment agent (Stage 5)</li>
<li>Measurement feedback loop</li>
</ul>
</div>
<div class="card edge-red">
<h3>◦ Phase 3 — Evaluation</h3>
<ul>
<li>Run AI curation on all 8 campaigns</li>
<li>Measure procedural sufficiency</li>
<li><strong>AI vs. human</strong> comparison</li>
</ul>
</div>
</div>

<div class="cards c2" style="margin-top:1rem;gap:1rem;">
<div class="card flat edge-blue"><p><strong>Immediate next steps:</strong> reproduce 1–2 more campaigns in the real lab; begin Phase 2 behind the <code>ai_generated</code> origin seam.</p></div>
<div class="card flat edge-grad"><p><strong>Research contribution:</strong> an empirical, repeatable way to <strong>measure the procedural semantics gap</strong> — and the first automated attempt to close it.</p></div>
</div>

<!--
Here is the roadmap, which places my proposal in the arc of the whole project and shows what is next.

Phase one, on the left, is the foundation, and it is done. That is the Pipeline UI, the backend, the containment core, the six stages working end to end, and the real ShadowRay emulation we just looked at. This is the shared groundwork, and it is complete and proven. Everything on the previous slide belongs to phase one.

Phase two, in the middle, is the AI curation agent — and this is my work, the individual contribution this talk has been about. It breaks into the four components I detailed: the DAG agent for stage three, the operationalization agent for stage four, the environment agent for stage five, and the measurement feedback loop over stage six. Each slots into the origin seam the foundation already prepared. The deliberate sequencing here matters: we built and proved the foundation first, specifically so that when the agent arrives, it plugs into a stable, well-understood, measurable pipeline rather than a moving target. That is also why we spent the effort understanding each stage's input, processing, and output so carefully — that understanding is the specification the agent must meet.

Phase three, on the right, is evaluation, which is what turns this from an engineering exercise into research. We run the AI-generated curation on all eight campaigns, measure procedural sufficiency on each using the existing stage-six machinery, and compare directly against the human-curated baseline. That AI-versus-human comparison, on an identical measurable pipeline, is the headline result the project is driving toward.

The bottom cards give the practical and the scientific framing. On the left, the immediate next steps: reproduce one or two more campaigns in the real lab to broaden the proof beyond ShadowRay, and then begin phase two, implementing the agents behind the ai_generated origin seam. On the right, the research contribution stated cleanly: STICKS provides an empirical, repeatable way to measure the procedural semantics gap in ATT&CK-in-STIX, and my agent is the first automated attempt to close it — with the measurement itself as the arbiter of success.

So to summarize the whole talk in one breath: structured threat intelligence tells us which techniques a campaign used but not how to reproduce it; STICKS makes that gap measurable through a contained six-stage pipeline; and my contribution is an AI agent that automatically generates both the executable commands and the emulation environment, evaluated head-to-head against human experts. With that, I would love to take your questions.
-->

---
layout: default
class: section-divider
---

<div class="cover" style="position:absolute;inset:0;"></div>

<div class="absolute left-14 right-14" style="top:30%;">
  <img src="/sticks-logomarca-modo-claro.png" class="h-24" alt="STICKS" />
  <h1 style="font-size:2.6rem;margin-top:1.2rem;">Thank you — <span class="grad-text">questions?</span></h1>
  <div class="grad-rule" style="width:130px;height:6px;"></div>
</div>

<div class="absolute bottom-16 left-14 right-14 flex justify-between items-end">
  <div>
    <div style="font-size:1.25rem;font-weight:800;">Nilson Sangy</div>
    <div style="font-size:.9rem;color:var(--muted);">CE-299 · Artificial Intelligence for Cybersecurity · Nov 20, 2026</div>
  </div>
  <div style="text-align:right;">
    <div style="font-weight:700;color:var(--sticks-purple);">github.com/c2dc/sticks</div>
    <div style="font-size:.82rem;color:var(--muted);">The Procedural Semantics Gap in ATT&amp;CK-in-STIX</div>
  </div>
</div>

<!--
Thank you very much for your attention. This is where I will stop and open the floor.

Let me leave you with the one-sentence version to anchor the discussion. ATT&CK and STIX tell us which techniques an adversary used, but not the concrete procedure needed to reproduce the campaign — the commands, their order, and the environment. STICKS turns that procedural semantics gap into something we can measure, inside a fully contained laboratory, through a six-stage pipeline that runs a real APT campaign end to end. And my contribution is an AI agent that automates the expert curation at the heart of that pipeline — generating both the executable command semantics and the emulated attack environment — so that we can, for the first time, measure automated curation against human experts on identical ground.

I am happy to go deeper on any part. A few directions I expect you might be curious about, and that I am glad to discuss: how the containment guarantees are actually verified and why we trust the fail-closed isolation gate; how procedural sufficiency is computed and what a "failed node" tells us diagnostically; why the DAG has to precede command selection; how the AI agent will use Atomic Red Team as a command source and what happens when no atomic test cleanly matches a technique; and how the feedback loop decides whether a failure is a command problem or an environment problem. I can also talk through the real ShadowRay run in more detail, or the engineering problems we hit bringing the lab up offline.

Please, go ahead with your questions.
-->
