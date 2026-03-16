# Tech Stack — Python Web Framework Prototype 2026

## Implementation Stack (as of 2026-03-16)

**Framework:** FastAPI 0.115+
**Template Engine:** Jinja2
**ASGI Server:** Uvicorn
**Python Version:** 3.10+ (minimum requirement for FastAPI 0.115+)

**Dependencies:**
```
fastapi>=0.115
jinja2
uvicorn[standard]
```

**Rationale:** See ADL-2026-03-16-1 for framework selection decision. FastAPI chosen for balance of minimal setup and modern async-first architecture.

---

## Research Methodology (Historical — Research Phase Complete)

**Research Tools:**
- WebSearch: Primary tool for discovering current state of ecosystem
- WebFetch: For retrieving specific documentation, release notes, blog posts
- Multiple source verification: Each claim requires 2+ independent sources

**Source Quality Standards:**
- Official documentation (framework homepages, release notes)
- PyPI stats (downloads, release history)
- GitHub metrics (stars, commits, issues, last release date)
- Stack Overflow trends (question volume, tag activity)
- Job posting aggregators (Indeed, LinkedIn)
- Developer surveys (Python Developer Survey, Stack Overflow Survey)
- Practitioner blogs and conference talks (PyCon, DjangoCon, FastAPI meetups)
- Production case studies (company engineering blogs)

**Recency Requirement:**
- All data must be from March 2026 or explicitly timestamped
- Trends must cover last 12 months (March 2025 - March 2026)
- Historical context allowed but must be clearly dated

**Verification Standard:**
- Each claim requires source citation with URL and access date
- Metrics require exact numbers with source and timestamp
- Qualitative assessments require 2+ practitioner sources

## Research Architecture

**Phase 1: Reality Anchor (blocking)**
- Validate that conventional wisdom from 2024-2025 still applies
- Identify any major ecosystem shifts that invalidate prior assumptions

**Phase 2: Landscape Assessment (blocking)**
- Establish current framework status (active/deprecated/stagnant)
- Document significant changes in last 12 months

**Phase 3: Multi-dimensional Research (parallel, non-blocking)**
- Adoption trends (quantitative)
- Use case categorization (qualitative)
- FastAPI maturity investigation (mixed methods)

**Phase 4: Synthesis (Thinker)**
- Integration of all research findings
- Recommendation matrix: [use case] × [framework]
