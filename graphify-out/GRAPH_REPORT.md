# Graph Report - MY-AI (1788169616.7398198)

## Corpus Check
- 10083 nodes · 29624 edges · 7 community sectors detected.
- Verdict: Corpus mapped cleanly into decoupled domain sectors.

## Community Sectors (Navigation Hubs)
- **[[_COMMUNITY_Utilities & System|Utilities & System]]**: 426 nodes
- **[[_COMMUNITY_Frontend UI & Navigation|Frontend UI & Navigation]]**: 6141 nodes
- **[[_COMMUNITY_FastAPI Endpoints|FastAPI Endpoints]]**: 2187 nodes
- **[[_COMMUNITY_Database Models|Database Models]]**: 253 nodes
- **[[_COMMUNITY_Desktop & Browser Automation|Desktop & Browser Automation]]**: 33 nodes
- **[[_COMMUNITY_Hermes Cognitive OS|Hermes Cognitive OS]]**: 618 nodes
- **[[_COMMUNITY_Agent Modules & Routers|Agent Modules & Routers]]**: 425 nodes

## God Nodes (Most Connected Core Abstractions)
1. `get()` - 590 connections (Agent Modules & Routers)
2. `__init__()` - 351 connections (Utilities & System)
3. `main.py` - 351 connections (FastAPI Endpoints)
4. `ai_engine.py` - 237 connections (FastAPI Endpoints)
5. `search()` - 228 connections (Database Models)
6. `execute()` - 206 connections (Frontend UI & Navigation)
7. `schemas.ts` - 190 connections (Frontend UI & Navigation)
8. `connect()` - 165 connections (FastAPI Endpoints)
9. `routes.py` - 154 connections (Hermes Cognitive OS)
10. `run()` - 123 connections (Hermes Cognitive OS)
11. `add()` - 102 connections (FastAPI Endpoints)
12. `dumps()` - 102 connections (Hermes Cognitive OS)

## Architectural Insights
- **Decoupled Fast Routing**: Router layer directly dispatches requests to domain executors (`DesktopDomainExecutor`, `BrowserDomainExecutor`, `SchedulerDomainExecutor`).
- **Targeted Memory Retrieval**: Query-relevant memory loading minimizes prompt token bloat.
- **Selective Verification**: High-risk actions undergo verification while safe routine commands run with zero overhead.

## Suggested Architecture Questions
- *How does `FastDomainRouter` isolate `DesktopDomainExecutor` from browser dependency overhead?*
- *Why does `RelevantMemoryRetriever` filter full memory tables into targeted keyword subsets?*
- *How does `SelectiveRiskVerifier` evaluate action risk scores prior to execution?*