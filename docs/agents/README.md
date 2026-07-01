# Ateles agents

The canonical, harness-neutral file for every `agent_definition` entity in Neotoma. Each `<name>.md` carries the full definition (frontmatter + metadata table + operational prompt) and is the source any harness can read. Neotoma is canonical; these files are generated — see `execution/scripts/render_agent_docs.py`. The Claude Code harness loads a generated mirror at `.claude/skills/<name>/SKILL.md` (same entity, minimal frontmatter). Per agent_policy `ent_c3c5e4a9350250cbf69e08bf`, prompts are public and PII-free.

| Agent | Tier | Genus | Status | Prompt | File |
| --- | --- | --- | --- | --- | --- |
| accipiter | T4 | Accipiter | planned | ✅ | [accipiter.md](accipiter.md) |
| anthus | T3 | Anthus | active | ✅ | [anthus.md](anthus.md) |
| apis | T3 | Apis | active | ✅ | [apis.md](apis.md) |
| apus | T3 |  | active | ✅ | [apus.md](apus.md) |
| aquila | T3 | Aquila | active | ✅ | [aquila.md](aquila.md) |
| ateles | T2 | Ateles | active | ✅ | [ateles.md](ateles.md) |
| aythya | T4 | Aythya | planned | ✅ | [aythya.md](aythya.md) |
| buteo | T4 | Buteo | planned | ✅ | [buteo.md](buteo.md) |
| cicada | T4 | Cicada | active | ✅ | [cicada.md](cicada.md) |
| ciconia | T4 | Ciconia | planned | ✅ | [ciconia.md](ciconia.md) |
| columba | T4 | Columba | planned | ✅ | [columba.md](columba.md) |
| corvus | T4 | Corvus | active | ✅ | [corvus.md](corvus.md) |
| cotinga | T3 | Cotinga cotinga | active | ✅ | [cotinga.md](cotinga.md) |
| formica | T3 | Formica | active | ✅ | [formica.md](formica.md) |
| fringilla | T4 | Fringilla | active | ✅ | [fringilla.md](fringilla.md) |
| gorilla | T4 | Gorilla | active | ✅ | [gorilla.md](gorilla.md) |
| hirundo | T4 | Hirundo | planned | ✅ | [hirundo.md](hirundo.md) |
| lanius | T3 | Lanius | planned | ✅ | [lanius.md](lanius.md) |
| manucode | T4 | Manucodia | planned | ✅ | [manucode.md](manucode.md) |
| menura | T2 | Menura | planned | ✅ | [menura.md](menura.md) |
| monedula | T3 | Corvus monedula | active | ✅ | [monedula.md](monedula.md) |
| neotoma-agent | T3 | Castor | planned | ✅ | [neotoma-agent.md](neotoma-agent.md) |
| pavo | T4 | Pavo | active | ✅ | [pavo.md](pavo.md) |
| phoenicurus | T4 | Phoenicurus | planned | ✅ | [phoenicurus.md](phoenicurus.md) |
| picus | T4 | Picus | active | ✅ | [picus.md](picus.md) |
| regulus | T4 | Regulus | planned | ✅ | [regulus.md](regulus.md) |
| robin | T4 | Erithacus | planned | ✅ | [robin.md](robin.md) |
| sitta | T3 | Sitta | proposed | ✅ | [sitta.md](sitta.md) |
| struthio | T4 | Struthio | planned | ✅ | [struthio.md](struthio.md) |
| sturnus | T3 | Sturnus | active | ✅ | [sturnus.md](sturnus.md) |
| sylvia | T3 | Sylvia | active | ✅ | [sylvia.md](sylvia.md) |
| turdus | T3 | Turdus | active | ✅ | [turdus.md](turdus.md) |
| tyto | T3 | Tyto | active | ✅ | [tyto.md](tyto.md) |
| vanellus | T4 | Vanellus | active | ✅ | [vanellus.md](vanellus.md) |
| waxwing | T4 | Bombycilla | active | ✅ | [waxwing.md](waxwing.md) |

*35 agents. Generated from Neotoma — do not edit directly.*
