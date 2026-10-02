"""Generated resume and cover-letter documents, the answer library and mock interviews."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse

from ..overrides import shared_router
from ...document_artifacts import delete_document, delete_document_artifact, ensure_document_artifact
from ...preparation import (
    MAX_MOCK_AUDIO_BYTES,
    PreparationNotFoundError,
    answer_mock_question,
    approve_document,
    create_document,
    create_mock_interview,
    delete_answer,
    document_record,
    edit_document,
    interview_record,
    list_interviews as list_mock_interviews,
    list_answers,
    list_documents,
    recorded_mock_answer_path,
    save_answer,
    store_recorded_mock_answer,
)
from ...integrations.agent_providers import provider_catalog
from ...integrations.pdf import markdown_to_html
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.preparation import (
    AnswerSaveRequest,
    DocumentCreateRequest,
    DocumentEditRequest,
    InterviewCreateRequest,
    MockAnswerRequest,
)


router = shared_router()


@router.get("/api/v1/preparation/documents")
def preparation_documents(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    items = list_documents(conn, user_id=user_id)
    return {"items": items, "total": len(items), "pdf_available": ctx.services.pdf_renderer is not None}


@router.post("/api/v1/preparation/documents", status_code=status.HTTP_201_CREATED)
def generate_preparation_document(
    payload: DocumentCreateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        provider = None
        if payload.provider:
            metadata = next(item for item in provider_catalog() if item["id"] == payload.provider)
            if not metadata["configured"]:
                raise ValueError(metadata["setup_hint"])
            provider = ctx.services.agent_provider_factory(payload.provider, str(metadata["model"]))
        return create_document(
            conn,
            payload.opportunity_id,
            payload.document_type,
            provider=provider, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/preparation/documents/{document_id}")
def get_preparation_document(
    document_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return document_record(conn, document_id, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc


@router.put("/api/v1/preparation/documents/{document_id}")
def update_preparation_document(
    document_id: str,
    payload: DocumentEditRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        document = edit_document(conn, document_id, payload.content, payload.evidence_fields, user_id=user_id)
        delete_document_artifact(conn, document_id, ctx.config.resume_storage, user_id=user_id)
        return document
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/preparation/documents/{document_id}/approve")
def approve_preparation_document(
    document_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        document = approve_document(conn, document_id, user_id=user_id)
        artifact = ensure_document_artifact(conn, document_id, ctx.config.resume_storage, user_id=user_id)
        public_artifact = {
            "id": artifact["id"],
            "filename": artifact["filename"],
            "media_type": artifact["media_type"],
            "byte_size": artifact["byte_size"],
            "sha256": artifact["sha256"],
            "created_at": artifact["created_at"],
        }
        return {**document, "artifact": public_artifact}
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/preparation/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_preparation_document(
    document_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    try:
        delete_document(conn, document_id, ctx.config.resume_storage, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/v1/preparation/documents/{document_id}/download")
def download_preparation_document(
    document_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    try:
        document = document_record(conn, document_id, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    filename = f"{document['document_type']}-v{document['version']}.md"
    return Response(
        content=document["content"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/v1/preparation/documents/{document_id}/pdf")
def download_preparation_document_pdf(
    document_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    try:
        document = document_record(conn, document_id, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    if ctx.services.pdf_renderer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PDF export needs Playwright: pip install -r requirements-optional.txt, then python -m playwright install chromium",
        )
    label = "Cover letter" if document["document_type"] == "cover_letter" else "Resume"
    try:
        pdf = ctx.services.pdf_renderer(markdown_to_html(document["content"], title=f"{label} v{document['version']}"))
    except Exception as exc:  # noqa: BLE001 - a missing browser build is the usual cause
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The PDF could not be rendered ({type(exc).__name__}). If Chromium is missing, run: python -m playwright install chromium",
        ) from exc
    filename = f"{document['document_type']}-v{document['version']}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/v1/preparation/answers")
def answer_library(
    q: str = Query(default="", max_length=200),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_answers(conn, q, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/preparation/answers", status_code=status.HTTP_201_CREATED)
def create_library_answer(
    payload: AnswerSaveRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return save_answer(conn, **payload.model_dump(), user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/preparation/answers/all", status_code=status.HTTP_204_NO_CONTENT)
def delete_all_library_answers(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    delete_answer(conn, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/api/v1/preparation/answers/{answer_id}")
def update_library_answer(
    answer_id: str,
    payload: AnswerSaveRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return save_answer(conn, answer_id=answer_id, **payload.model_dump(), user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/preparation/answers/{answer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_library_answer(
    answer_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    if not delete_answer(conn, answer_id, user_id=user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/preparation/interviews", status_code=status.HTTP_201_CREATED)
def start_mock_interview(
    payload: InterviewCreateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return create_mock_interview(conn, payload.opportunity_id, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc


@router.get("/api/v1/preparation/interviews")
def mock_interviews(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_mock_interviews(conn, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.get("/api/v1/preparation/interviews/{interview_id}")
def get_mock_interview(
    interview_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return interview_record(conn, interview_id, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found") from exc


@router.post("/api/v1/preparation/questions/{question_id}/answers", status_code=status.HTTP_201_CREATED)
def submit_mock_answer(
    question_id: str,
    payload: MockAnswerRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return answer_mock_question(conn, question_id, payload.answer_text, payload.transcript, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/preparation/questions/{question_id}/recorded-answers", status_code=status.HTTP_201_CREATED)
async def submit_recorded_mock_answer(
    question_id: str,
    audio: Annotated[UploadFile, File()],
    answer_text: Annotated[str, Form()] = "",
    transcript: Annotated[str, Form()] = "",
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    if len(answer_text) > 100_000 or len(transcript) > 100_000:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Reviewed answer text is too large")
    audio_data = await audio.read(MAX_MOCK_AUDIO_BYTES + 1)
    try:
        return store_recorded_mock_answer(
            conn,
            question_id,
            answer_text,
            transcript,
            audio_data,
            audio.content_type or "",
            ctx.config.interview_storage,
            user_id=user_id,
        )
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/preparation/answers/{answer_id}/audio")
def download_recorded_mock_answer(
    answer_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> FileResponse:
    try:
        path, media_type = recorded_mock_answer_path(conn, answer_id, ctx.config.interview_storage, user_id=user_id)
    except PreparationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found") from exc
    return FileResponse(path, media_type=media_type, filename=f"mock-interview{path.suffix}")
