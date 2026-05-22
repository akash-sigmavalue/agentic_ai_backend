# Portfolio Management Integration

Portfolio management backend integrated into the agentic AI backend.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## PostgreSQL

Create a PostgreSQL database and user:

```sql
CREATE USER avinash WITH PASSWORD 'avinash';
CREATE DATABASE portfolio_db OWNER avinash;
```

Then set `DATABASE_URL` in `.env`:

```env
DATABASE_URL="postgresql+psycopg://avinash:avinash@localhost:5432/portfolio_db"
```

The integrated app creates the portfolio tables on startup through `database.portfolio.db.init_db()`. Replace the sample username, password, host, port, and database name as needed for your PostgreSQL instance.

## Main APIs

- `GET /portfolio/health`
- `GET /portfolio/sections`
- `GET /portfolio/records/{section_key}`
- `POST /portfolio/uploads/{section_key}/preview`
- `PATCH /portfolio/uploads/{upload_id}/mapping`
- `POST /portfolio/uploads/{upload_id}/confirm`
- `POST /portfolio/uploads/global/preview`
- `PATCH /portfolio/uploads/global/{upload_id}/mapping`
- `POST /portfolio/uploads/global/{upload_id}/confirm`
- `GET /portfolio/dashboard`
- `POST /portfolio/dashboard/refresh`

## Design

The mapping agent is scoped. Section upload sends only the selected section fields. Global upload detects candidate tables first, then maps each table against its detected section, never all fields blindly.
