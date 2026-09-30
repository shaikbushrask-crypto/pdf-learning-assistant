
import os
import re
import uuid
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Generator

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    UploadFile,
    File,
    status,
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine,
    String,
    Text,
    ForeignKey,
    Integer,
    DateTime,
    or_,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
    Session,
)
from pwdlib import PasswordHash
import jwt
from jwt.exceptions import InvalidTokenError
from pypdf import PdfReader

load_dotenv()

# --------------------------------------------------
# 1. CONFIGURATION
# --------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./learning_assistant.db",
)

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "").strip()
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
)

MAX_PDF_BYTES = int(os.getenv("MAX_PDF_MB", "10")) * 1024 * 1024

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

if not JWT_SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is missing. Create a .env file beside main.py "
        "and add a randomly generated secret key."
    )

connect_args = (
    {"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

password_hasher = PasswordHash.recommended()

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login"
)

app = FastAPI(
    title="PDF Learning Assistant",
    description=(
        "User registration, JWT authentication, PDF upload, "
        "text extraction, summaries, and quizzes."
    ),
    version="1.1.0",
)


# --------------------------------------------------
# 2. DATABASE MODELS
# --------------------------------------------------

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    username: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    materials: Mapped[list["Material"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    stored_filename: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )

    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    extracted_text: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    owner: Mapped["User"] = relationship(
        back_populates="materials",
    )


Base.metadata.create_all(bind=engine)


# --------------------------------------------------
# 3. DATABASE SESSION
# --------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------
# 4. USER SCHEMA
# --------------------------------------------------

class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)


# --------------------------------------------------
# 5. JWT TOKEN GENERATION
# --------------------------------------------------

def create_access_token(user_id: int) -> str:
    expiration = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": str(user_id),
        "exp": expiration,
    }

    return jwt.encode(
        payload,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


# --------------------------------------------------
# 6. JWT AUTHENTICATION
# --------------------------------------------------

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )

        subject = payload.get("sub")

        if not subject:
            raise credentials_error

        user_id = int(subject)

    except (InvalidTokenError, ValueError, TypeError):
        raise credentials_error

    user = db.get(User, user_id)

    if user is None:
        raise credentials_error

    return user


# --------------------------------------------------
# 7. MATERIAL OWNERSHIP
# --------------------------------------------------

def get_owned_material(
    material_id: int,
    user: User,
    db: Session,
) -> Material:
    material = db.get(Material, material_id)

    if material is None or material.owner_id != user.id:
        raise HTTPException(
            status_code=404,
            detail="Material not found.",
        )

    return material


# --------------------------------------------------
# 8. PDF TEXT EXTRACTION AND OPTIONAL OCR
# --------------------------------------------------

def extract_pdf_text(pdf_path: Path) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        page_text = [
            page.extract_text() or ""
            for page in reader.pages
        ]
        text = "\n\n".join(page_text).strip()

    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="Unable to read this PDF file.",
        ) from exc

    if len(text) >= 40:
        return text

    # Optional OCR for scanned PDFs.
    try:
        import fitz
        import pytesseract
        from PIL import Image
        from io import BytesIO

        ocr_pages = []

        with fitz.open(str(pdf_path)) as document:
            for page in document:
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(2, 2),
                    alpha=False,
                )
                image = Image.open(
                    BytesIO(pixmap.tobytes("png"))
                )
                ocr_pages.append(
                    pytesseract.image_to_string(image)
                )

        ocr_text = "\n\n".join(ocr_pages).strip()

        if len(ocr_text) > len(text):
            text = ocr_text

    except ImportError:
        pass
    except Exception:
        pass

    if not text:
        raise HTTPException(
            status_code=422,
            detail=(
                "No readable text was found. For scanned PDFs, "
                "install the optional OCR packages and Tesseract OCR."
            ),
        )

    return text


# --------------------------------------------------
# 9. SUMMARY GENERATION
# --------------------------------------------------

def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()

    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
        if len(sentence.strip()) > 25
    ]


def generate_summary(text: str) -> tuple[str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize the supplied study material "
                            "with a title and concise bullet points. "
                            "Do not invent facts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": text[:30000],
                    },
                ],
                temperature=0.2,
            )

            result = response.choices[0].message.content

            if result:
                return result.strip(), "llm"

        except Exception:
            pass

    sentences = split_sentences(text)

    if not sentences:
        excerpt = re.sub(r"\s+", " ", text).strip()[:1200]
        return (
            "Summary (extractive fallback)\n\n" + excerpt,
            "extractive_fallback",
        )

    summary = "\n".join(
        f"- {sentence}" for sentence in sentences[:6]
    )

    return (
        "Summary (extractive fallback)\n\n" + summary,
        "extractive_fallback",
    )


# --------------------------------------------------
# 10. QUIZ GENERATION
# --------------------------------------------------

def generate_quiz(
    text: str,
    count: int = 5,
) -> tuple[list[dict], str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Create quiz questions using only the "
                            "supplied material. Return valid JSON "
                            "with a questions array. Each question "
                            "must contain question and answer fields."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Create {count} quiz questions and answers "
                            f"from this material:\n{text[:30000]}"
                        ),
                    },
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)

            items = (
                data if isinstance(data, list)
                else data.get("questions", [])
                if isinstance(data, dict)
                else []
            )

            questions = []

            if isinstance(items, list):
                for item in items[:count]:
                    if (
                        isinstance(item, dict)
                        and item.get("question")
                        and item.get("answer")
                    ):
                        questions.append({
                            "question": str(item["question"]),
                            "answer": str(item["answer"]),
                        })

            if questions:
                return questions, "llm"

        except Exception:
            pass

    sentences = split_sentences(text)
    questions = []

    for index, sentence in enumerate(
        sentences[:count],
        start=1,
    ):
        questions.append({
            "question": (
                f"Review question {index}: Explain the main idea "
                f"in this statement: \"{sentence[:220]}\""
            ),
            "answer": sentence,
        })

    if not questions:
        excerpt = re.sub(r"\s+", " ", text).strip()[:300]
        questions.append({
            "question": "What is the main idea of the uploaded material?",
            "answer": excerpt or "No readable text was extracted.",
        })

    return questions, "source_based_fallback"


# --------------------------------------------------
# 11. HOME AND HEALTH
# --------------------------------------------------

@app.get("/")
def home():
    return {
        "message": "PDF Learning Assistant API is running.",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


# --------------------------------------------------
# 12. REGISTER USER
# --------------------------------------------------

@app.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
):
    username = user_data.username.strip()
    email = user_data.email.strip().lower()

    if len(username) < 2:
        raise HTTPException(
            status_code=422,
            detail="Username must contain at least 2 characters.",
        )

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(
            status_code=422,
            detail="Enter a valid email address.",
        )

    existing_user = db.query(User).filter(
        or_(
            User.email == email,
            User.username == username,
        )
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="That email or username is already registered.",
        )

    user = User(
        username=username,
        email=email,
        password_hash=password_hasher.hash(user_data.password),
    )

    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not register the user. Check the server logs.",
        )

    return {
        "message": "User registered successfully.",
        "id": user.id,
        "username": user.username,
        "email": user.email,
    }


# --------------------------------------------------
# 13. LOGIN - ACCEPT EMAIL OR USERNAME
# --------------------------------------------------

@app.post("/auth/login")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    # Swagger's username field can contain either the email
    # address or the registered username.
    identifier = form_data.username.strip()

    user = db.query(User).filter(
        or_(
            User.email == identifier.lower(),
            User.username == identifier,
        )
    ).first()

    valid_password = False

    if user is not None:
        try:
            valid_password = password_hasher.verify(
                form_data.password,
                user.password_hash,
            )
        except Exception:
            valid_password = False

    if user is None or not valid_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username/email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(user.id)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


# --------------------------------------------------
# 14. CURRENT USER
# --------------------------------------------------

@app.get("/auth/me")
def read_current_user(
    current_user: User = Depends(get_current_user),
):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
    }


# --------------------------------------------------
# 15. UPLOAD PDF
# --------------------------------------------------

@app.post(
    "/materials/upload",
    status_code=status.HTTP_201_CREATED,
)
def upload_material(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    original_name = Path(
        file.filename or "upload.pdf"
    ).name

    if Path(original_name).suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=415,
            detail="Only PDF files are accepted.",
        )

    stored_name = f"{uuid.uuid4().hex}.pdf"
    destination = UPLOAD_DIR / stored_name

    size = 0
    header = b""

    try:
        with destination.open("wb") as output:
            while True:
                chunk = file.file.read(1024 * 1024)

                if not chunk:
                    break

                if not header:
                    header = chunk[:8]

                size += len(chunk)

                if size > MAX_PDF_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="PDF exceeds the configured size limit.",
                    )

                output.write(chunk)

        if not header.startswith(b"%PDF-"):
            raise HTTPException(
                status_code=415,
                detail="The uploaded file is not a valid PDF.",
            )

        extracted_text = extract_pdf_text(destination)

        material = Material(
            original_filename=original_name[:255],
            stored_filename=stored_name,
            owner_id=current_user.id,
            extracted_text=extracted_text,
        )

        db.add(material)
        db.commit()
        db.refresh(material)

        return {
            "message": "PDF uploaded and text extracted successfully.",
            "id": material.id,
            "filename": material.original_filename,
            "characters_extracted": len(extracted_text),
        }

    except Exception:
        db.rollback()

        if destination.exists():
            destination.unlink(missing_ok=True)

        raise

    finally:
        file.file.close()


# --------------------------------------------------
# 16. SUMMARIZE PDF
# --------------------------------------------------

@app.post("/materials/{material_id}/summary")
def summarize_material(
    material_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    material = get_owned_material(
        material_id,
        current_user,
        db,
    )

    if not material.extracted_text.strip():
        pdf_path = UPLOAD_DIR / material.stored_filename

        if not pdf_path.exists():
            raise HTTPException(
                status_code=404,
                detail="The stored PDF file could not be found.",
            )

        material.extracted_text = extract_pdf_text(pdf_path)

    summary, method = generate_summary(material.extracted_text)

    material.summary = summary
    db.commit()

    return {
        "material_id": material.id,
        "filename": material.original_filename,
        "summary": summary,
        "generation_method": method,
    }


# --------------------------------------------------
# 17. GENERATE QUIZ
# --------------------------------------------------

@app.post("/materials/{material_id}/quiz")
def quiz_material(
    material_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    material = get_owned_material(
        material_id,
        current_user,
        db,
    )

    if not material.extracted_text.strip():
        pdf_path = UPLOAD_DIR / material.stored_filename

        if not pdf_path.exists():
            raise HTTPException(
                status_code=404,
                detail="The stored PDF file could not be found.",
            )

        material.extracted_text = extract_pdf_text(pdf_path)
        db.commit()

    questions, method = generate_quiz(
        material.extracted_text,
        count=5,
    )

    return {
        "material_id": material.id,
        "filename": material.original_filename,
        "count": len(questions),
        "generation_method": method,
        "questions": questions,
    }