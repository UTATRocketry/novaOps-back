# NovaOps Backend

NovaOps Backend is a FastAPI service that bridges frontend clients, MQTT device traffic, and config-driven parsing/translation logic.

## Documentation Map

- **Backend Architecture**: service responsibilities, parsing flow, managers, runtime state.
- **OpenAPI Docs**:
  - Swagger UI: `/docs`
  - ReDoc: `/redoc`

## Quick Start

1. Run locally with scripts in `scripts/`.
2. Or run in Docker with `docker compose up --build`.
3. Open `http://localhost:8000/docs` for live API schema and examples.
