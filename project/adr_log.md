# ADR Log — Python Web Framework Research 2026

## ADL-2026-03-15-1: Research Methodology and Sequential Gating

**Decision:** Use staged research approach with blocking reality anchor before detailed investigation

**Options Considered:**
- Option A: Parallel research across all dimensions immediately
  - Reason rejected: Risk of researching outdated landscape if major changes occurred in last 12 months
- Option B: Single comprehensive research pass without validation
  - Reason rejected: Could produce recommendations based on 2024 conventional wisdom rather than 2026 current state
- Option C: Reality anchor first, then parallel detailed research (CHOSEN)
  - Rationale: Validates assumptions before committing research effort; enables efficient parallel work after landscape confirmed

**Chosen:** Staged approach with blocking validation phase

**Tradeoff:** Adds ~30-60 minutes to research timeline for reality anchor, but prevents wasting hours researching an outdated landscape. Sacrifices immediate parallelization for research validity.

**Tech Debt Flag:** false

**Reversibility:** easy (can pivot research strategy mid-stream if reality anchor reveals need)

**Date:** 2026-03-15

---

## ADL-2026-03-15-2: Multi-Source Verification Standard

**Decision:** Require 2+ independent sources for all claims; official sources prioritized over anecdotal

**Options Considered:**
- Option A: Accept single-source claims if source is authoritative
  - Reason rejected: Even official sources can be outdated or biased (vendor marketing)
- Option B: Require 3+ sources for all claims
  - Reason rejected: Over-burdens research for widely-known facts; slows progress unnecessarily
- Option C: 2+ sources with source quality tiers (CHOSEN)
  - Rationale: Balances verification rigor with research efficiency

**Chosen:** 2+ source standard with quality tiers:
- Tier 1: Official docs, PyPI/GitHub metrics, developer surveys
- Tier 2: Practitioner blogs, conference talks, production case studies
- Tier 3: Social media, forum posts (requires 3+ sources or Tier 1/2 corroboration)

**Tradeoff:** More research time per claim, but higher confidence in recommendations. Sacrifices speed for validity.

**Tech Debt Flag:** false

**Reversibility:** easy (can relax standard for non-critical claims if time-constrained)

**Date:** 2026-03-15

---

## ADL-2026-03-15-3: FastAPI Maturity Investigation as Separate Task

**Decision:** Dedicated research task to investigate FastAPI production maturity separately from general adoption metrics

**Options Considered:**
- Option A: Treat FastAPI like all other frameworks in adoption metrics task
  - Reason rejected: Thinker identified specific contradiction (popularity vs. maturity) requiring deeper investigation
- Option B: Skip FastAPI-specific investigation, rely on general metrics
  - Reason rejected: Fails to address the lens contradiction; risks recommending popular but immature framework
- Option C: Separate deep-dive task on FastAPI production evidence (CHOSEN)
  - Rationale: Resolves contradiction by specifically seeking production use evidence beyond popularity metrics

**Chosen:** Dedicated Task 5 for FastAPI maturity investigation

**Tradeoff:** Additional research task adds complexity and time, but directly addresses known risk identified by Thinker.

**Tech Debt Flag:** false

**Reversibility:** easy (can merge with Task 3 if research reveals no maturity gap)

**Date:** 2026-03-15

---

## ADL-2026-03-16-1: Framework Selection for Minimal Navigation Prototype

**Decision:** Use FastAPI with Jinja2 templates for minimal website prototype with navigation routing

**Options Considered:**
- Option A: Django 5.2 LTS
  - Reason rejected: Over-provisioned for "minimal" requirement — requires settings.py, apps config, migrations, admin setup even for simple multi-page site. Setup overhead ~45-60 minutes for basic navigation.
- Option B: Flask 3.1.2
  - Reason rejected: Research synthesis (lines 65-78) shows Flask in maintenance mode with only 2 releases in 2025, no active feature development, overtaken by FastAPI in community mindshare. Not recommended for ANY new work, even prototypes.
- Option C: FastAPI with Jinja2 templates (CHOSEN)
  - Rationale: Balances minimal setup (~20-30 lines for basic navigation) with modern async-first architecture. Server-side rendering via Jinja2 matches "navigation routing" requirement. Future-proof if prototype expands to API endpoints or async workloads.

**Chosen:** FastAPI 0.115+ with Jinja2 template rendering

**Tradeoff:** Requires Python 3.10+ (FastAPI minimum per synthesis lines 51-55), which may not be available in all environments. Sacrifices Flask's slight setup simplicity for long-term maintainability and async capability. Requires explicit Jinja2 setup (not batteries-included like Django templates).

**Tech Debt Flag:** false

**Debt Note:** null

**Reversibility:** easy (switching to Django or another framework at prototype stage requires rewriting ~50-100 lines of code, minimal cost)

**Date:** 2026-03-16
