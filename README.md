# Pensieve

Self-hosted RSS reader with the Google Reader / Reeder experience, plus a local-LLM layer (categorization,
story clustering, reader memory, daily insights) served by the homelab LiteLLM gateway. See CLAUDE.md for
the engineering contract and the PRD link.

```bash
cp .env.example .env
make dev-db && make migrate && make dev   # http://localhost:8000
make worker                                # in a second shell
```
