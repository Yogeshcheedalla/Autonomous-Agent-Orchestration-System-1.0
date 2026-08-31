# Graph Report - MY-AI (1785341951.382655)

## Corpus Check
- 8314 nodes · 21481 edges · 7 community sectors detected.
- Verdict: Corpus mapped cleanly into decoupled domain sectors.

## Community Sectors (Navigation Hubs)
- **[[_COMMUNITY_Utilities & System|Utilities & System]]**: 426 nodes
- **[[_COMMUNITY_Frontend UI & Navigation|Frontend UI & Navigation]]**: 5982 nodes
- **[[_COMMUNITY_FastAPI Endpoints|FastAPI Endpoints]]**: 720 nodes
- **[[_COMMUNITY_Database Models|Database Models]]**: 165 nodes
- **[[_COMMUNITY_Desktop & Browser Automation|Desktop & Browser Automation]]**: 29 nodes
- **[[_COMMUNITY_Agent Modules & Routers|Agent Modules & Routers]]**: 405 nodes
- **[[_COMMUNITY_Hermes Cognitive OS|Hermes Cognitive OS]]**: 587 nodes

## God Nodes (Most Connected Core Abstractions)
1. `get()` - 390 connections (Agent Modules & Routers)
2. `__init__()` - 268 connections (Utilities & System)
3. `main.py` - 253 connections (FastAPI Endpoints)
4. `ai_engine.py` - 222 connections (FastAPI Endpoints)
5. `schemas.ts` - 190 connections (Frontend UI & Navigation)
6. `search()` - 183 connections (FastAPI Endpoints)
7. `execute()` - 168 connections (Agent Modules & Routers)
8. `routes.py` - 154 connections (Hermes Cognitive OS)
9. `connect()` - 121 connections (Hermes Cognitive OS)
10. `settings-route.tsx` - 78 connections (Frontend UI & Navigation)
11. `session-route.tsx` - 77 connections (Frontend UI & Navigation)
12. `dumps()` - 72 connections (Hermes Cognitive OS)

## Architectural Insights
- **Decoupled Fast Routing**: Router layer directly dispatches requests to domain executors (`DesktopDomainExecutor`, `BrowserDomainExecutor`, `SchedulerDomainExecutor`).
- **Targeted Memory Retrieval**: Query-relevant memory loading minimizes prompt token bloat.
- **Selective Verification**: High-risk actions undergo verification while safe routine commands run with zero overhead.

## Suggested Architecture Questions
- *How does `FastDomainRouter` isolate `DesktopDomainExecutor` from browser dependency overhead?*
- *Why does `RelevantMemoryRetriever` filter full memory tables into targeted keyword subsets?*
- *How does `SelectiveRiskVerifier` evaluate action risk scores prior to execution?*