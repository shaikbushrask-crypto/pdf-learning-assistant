
# PDF Learning Assistant

A FastAPI-based application that helps users learn from PDF documents by uploading learning materials, generating summaries, and creating quizzes.

## Features

- User registration
- User login with JWT authentication
- Protected API endpoints
- PDF document upload
- Summary generation
- Quiz generation
- Current authenticated user endpoint
- SQLite database

## Technology Stack

- Python
- FastAPI
- SQLAlchemy
- SQLite
- JWT Authentication
- PyPDF

## Project Setup

### 1. Clone the repository

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd pdf-learning-assistant
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file using `.env.example` as a template.

Set a secure `JWT_SECRET_KEY` and configure the database URL and other required settings.

Never upload your `.env` file or secret keys to GitHub.

### 5. Run the application

```bash
python -m uvicorn main:app --reload
```

### 6. Open API documentation

Visit:

http://127.0.0.1:8000/docs

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/users` | Register a user |
| POST | `/auth/login` | Log in and obtain a JWT |
| GET | `/auth/me` | Get the current authenticated user |
| POST | `/materials/upload` | Upload a PDF |
| POST | `/materials/{material_id}/summary` | Generate a summary |
| POST | `/materials/{material_id}/quiz` | Generate a quiz |

Check the interactive Swagger documentation for the exact request fields and response formats.

## Database

The application uses SQLite for local development.

## Security

- Passwords are hashed before storage.
- JWT authentication protects private endpoints.
- Environment secrets should remain private.

## Future Improvements

- OCR for scanned PDFs
- LLM-based summaries and quizzes
- Improved quiz customization
- Additional document formats

## Author

Thaiseen Nishath