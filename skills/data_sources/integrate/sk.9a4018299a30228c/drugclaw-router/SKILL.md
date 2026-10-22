---
name: drugclaw-router
description: On-demand router to the DrugClaw drug-intelligence skills repository (QSong-github/DrugClaw). Use when a user needs to query any drug-related database (DrugBank, ChEMBL, BindingDB, PharmGKB, SIDER, FAERS, Open Targets, KEGG DRUG, etc.), drug-target interaction analysis, adverse event analysis, drug repurposing, pharmacogenomics, or drug combination queries. Fetches the matching SKILL.md from GitHub raw on demand — does NOT bundle 57 local skills.
tools: []
tags:
- drugclaw
- drug-discovery
- pharmacology
- drug-database
- drug-target
- adverse-event
- pharmacogenomics
- drug-repurposing
- cheinformatics
---

# DrugClaw Router

On-demand router for the **DrugClaw** skills repository ([QSong-github/DrugClaw](https://github.com/QSong-github/DrugClaw)),
covering **57 drug-intelligence database query skills** across drug discovery, pharmacology, toxicology, and pharmacogenomics.

> This skill does **not** duplicate the 57 skills. It tells the agent how to (1) **search**
> the DrugClaw folder listing, (2) **fetch** the matching `SKILL.md` from GitHub raw on
> demand, and (3) **follow** it.

---

## Quick domain map

### Core drug databases

| Folder | Database | What it covers |
|--------|----------|---------------|
| `drugbank` | DrugBank | Comprehensive drug/compound info |
| `chembl` | ChEMBL | Bioactive molecules, SAR |
| `chebi` | ChEBI | Small molecule chemical entities |
| `bindingdb` | BindingDB | Protein-ligand binding affinity |
| `stitch` | STITCH | Compound-protein interactions |
| `drugcentral` | DrugCentral | Drug information hub |
| `ttd` | TTD | Therapeutic target database |
| `iuphar` | IUPHAR/BPS | Receptors & ion channels |
| `unid3` | UNID3 | Drug 3D structures |
| `drugmechdb` | DrugMechDB | Drug mechanism of action |

### Pharmacogenomics

| Folder | Database | What it covers |
|--------|----------|---------------|
| `pharmgkb` | PharmGKB | Pharmacogenomics knowledge base |
| `cpic` | CPIC | Clinical PGx implementation guidelines |
| `dgidb` | DGIDB | Drug-gene interaction database |
| `drugprot` | DrugProt | Drug-protein relations |
| `molecular_targets` | Molecular Targets | Drug target database |
| `molecular_targets_data` | Molecular Targets | Drug target dataset |

### Adverse events & toxicity

| Folder | Database | What it covers |
|--------|----------|---------------|
| `sider` | SIDER | Drug side effects |
| `faers` | FAERS | FDA adverse event reports |
| `ade_corpus` | ADE Corpus | Adverse event relation extraction |
| `dili` | DILI | Drug-induced liver injury |
| `dilirank` | DILIRank | Hepatotoxicity ranking |
| `livertox` | LiverTox | Drug hepatotoxicity |
| `unitox` | UniTox | Drug toxicity |
| `nsides` | NSIDES | Drug side effects |
| `psytar` | PsyTAR | Psychiatric drug ADRs |
| `tarkg` | TARkg | Target-adverse reaction KG |
| `adrecs` | ADReCS | ADR classification system |
| `cadec` | CADEC | ADR clinical text dataset |
| `tac2017` | TAC 2017 | ADR relation extraction data |

### Drug-drug interactions

| Folder | Database | What it covers |
|--------|----------|---------------|
| `ddinter` | DDInter | Drug-drug interactions |
| `ddi_corpus` | DDI Corpus | DDI relation extraction |
| `mecddi` | MeCDDI | DDI mechanism dataset |

### Drug combinations

| Folder | Database | What it covers |
|--------|----------|---------------|
| `drugcomb` | DrugComb | Drug combination effects |
| `drugcombdb` | DrugCombDB | Drug combination database |

### Drug repurposing

| Folder | Database | What it covers |
|--------|----------|---------------|
| `repodb` | repoDB | Drug repositioning database |
| `repurposedrugs` | RepurposedDrugs | Drug new uses |
| `repurposing_hub` | Drug Repurposing Hub | Repurposing candidates |
| `drugrepobank` | DrugRepoBank | Drug repositioning |
| `drkg` | DRKG | Drug repurposing KG |

### Regulatory & clinical

| Folder | Database | What it covers |
|--------|----------|---------------|
| `openfda` | OpenFDA | Open FDA drug data API |
| `fda_orange_book` | FDA Orange Book | Therapeutic equivalence |
| `dailymed` | DailyMed | FDA drug labels |
| `medlineplus` | MedLinePlus | Drug & supplement info |
| `webmd` | WebMD | Drug & supplement info |
| `rxnorm` | RxNorm | US clinical drug terminology |
| `ndfrt` | NDF-RT | National drug file reference |
| `atc` | ATC | Anatomical therapeutic classification |
| `who_eml` | WHO EML | Essential medicines list |
| `drugs_com_reviews` | Drugs.com | User drug reviews |

### Cancer & special databases

| Folder | Database | What it covers |
|--------|----------|---------------|
| `gdsc` | GDSC | Cancer drug sensitivity genomics |
| `phee` | PHEE | Drug phenotype effects |
| `open_targets` | Open Targets | Drug target-disease associations |
| `kegg_drug` | KEGG DRUG | KEGG drug database |
| `oregano` | OREGANO | Natural products |
| `sematyp` | SemaTyp | Drug semantic types |
| `gdkd` | GDKD | Drug knowledge graph |
| `pharmkg` | PharmKG | Drug knowledge graph |

---

## How to use (router workflow)

### Step 1 — Find the right skill folder

From the tables above, locate the database you need.  The folder names follow the
database short name (e.g. `drugbank`, `chembl`, `sider`).

If unsure, list all folders via the GitHub API:

```python
import urllib.request, json

url = "https://api.github.com/repos/QSong-github/DrugClaw/contents/skills"
req = urllib.request.Request(url, headers={"User-Agent": "bio-agent", "Accept": "application/vnd.github+json"})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
for item in data:
    if item["type"] == "dir":
        print(item["name"])
```

Or search by keyword:

```python
keyword = "adverse"
url = f"https://api.github.com/search/code?q={keyword}+repo:QSong-github/DrugClaw+path:skills+filename:SKILL.md"
req = urllib.request.Request(url, headers={"User-Agent": "bio-agent", "Accept": "application/vnd.github+json"})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
for item in data.get("items", []):
    print(item["path"])  # e.g. skills/sider/SKILL.md
```

### Step 2 — Fetch the SKILL.md on demand

```python
folder = "drugbank"   # from Step 1 or the tables above
url = f"https://raw.githubusercontent.com/QSong-github/DrugClaw/main/skills/{folder}/SKILL.md"
skill_md = urllib.request.urlopen(url, timeout=30).read().decode()
print(skill_md)   # read it, then follow its instructions
```

### Step 3 — Clone fallback (if raw fetch unavailable)

```bash
git clone https://github.com/QSong-github/DrugClaw.git
# Skills are at DrugClaw/skills/<folder_name>/SKILL.md
```

For China-accelerated access:
```bash
git clone https://ghfast.top/https://github.com/QSong-github/DrugClaw.git
```

---

## Example interactions

**User:** "Query DrugBank for aspirin information."
**Agent:** → Folder `drugbank`. Fetch
`https://raw.githubusercontent.com/QSong-github/DrugClaw/main/skills/drugbank/SKILL.md` and follow it.

**User:** "Check if there are known adverse events for metformin."
**Agent:** → Folder `sider` and `faers`. Fetch both for comprehensive AE coverage.

**User:** "What drugs target EGFR?"
**Agent:** → Folder `dgidb` or `molecular_targets`. Fetch and query for EGFR targets.

**User:** "Are there any drug repurposing candidates for Alzheimer's?"
**Agent:** → Folders `repodb`, `repurposing_hub`, `repurposedrugs`. Fetch all three and cross-reference.

**User:** "Look up the CPIC guideline for clopidogrel and CYP2C19."
**Agent:** → Folder `cpic`. Fetch and query by gene-drug pair.

**User:** "Find ChEMBL bioactivity data for a kinase inhibitor."
**Agent:** → Folder `chembl`. Fetch and follow.

---

## Notes

- DrugClaw is an actively maintained repository with **57 skills** covering the full drug intelligence stack: databases, adverse events, drug-drug interactions, repurposing, pharmacogenomics, and regulatory data.
- All skills are database query skills — each SKILL.md provides Python code to query its specific database API.
- Always resolve the skill folder name from the tables above or the API listing rather than guessing.
- Some DrugClaw skills were previously included locally in `skills/drugclaw/`; those have been removed in favor of this on-demand router.
