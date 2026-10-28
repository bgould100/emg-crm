import asyncio
import pytest
from fastapi.testclient import TestClient
import app.api.main as api
from app.api.main import HomeownerRequestBodyLimitMiddleware


def run_asgi(middleware, scope, receive):
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def homeowner_scope(headers=None):
    return {
        "type": "http",
        "method": "POST",
        "path": "/homeowner/submit",
        "headers": headers or [],
    }


def response_status(messages):
    return next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )


def test_rejects_oversized_content_length_before_reading_body():
    async def app(scope, receive, send):
        raise AssertionError("Oversized request should not reach the app.")

    middleware = HomeownerRequestBodyLimitMiddleware(app)

    async def receive():
        raise AssertionError("Body should not be read.")

    scope = homeowner_scope([
        (
            b"content-length",
            str(middleware.MAX_BODY_SIZE + 1).encode("ascii"),
        )
    ])

    messages = run_asgi(middleware, scope, receive)

    assert response_status(messages) == 413


def test_rejects_oversized_stream_without_content_length():
    async def app(scope, receive, send):
        raise AssertionError("Oversized request should not reach the app.")

    middleware = HomeownerRequestBodyLimitMiddleware(app)
    body = b"x" * (middleware.MAX_BODY_SIZE + 1)
    delivered = False

    async def receive():
        nonlocal delivered

        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        return {"type": "http.disconnect"}

    messages = run_asgi(
        middleware,
        homeowner_scope(),
        receive,
    )

    assert response_status(messages) == 413

def test_passes_normal_body_to_application_unchanged():
    body = b"normal homeowner form body"
    received_by_app = []

    async def app(scope, receive, send):
        message = await receive()
        received_by_app.append(message["body"])
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b"ok",
        })

    middleware = HomeownerRequestBodyLimitMiddleware(app)
    delivered = False

    async def receive():
        nonlocal delivered

        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        return {"type": "http.disconnect"}

    messages = run_asgi(
        middleware,
        homeowner_scope(),
        receive,
    )

    assert response_status(messages) == 200
    assert received_by_app == [body]

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("customer_name", "N" * 151),
        ("customer_phone", "2" * 31),
        ("service_type", "S" * 101),
        ("description", "D" * 5001),
        ("customer_email", "a" * 256 + "@example.com"),
        ("additional_info", "I" * 5001),
    ],
)
def test_homeowner_submission_rejects_oversized_text_fields(
    monkeypatch, field, value
):
    monkeypatch.setenv("EMG_ALLOWED_ORIGIN", "http://testserver")

    def unexpected_create_lead(**kwargs):
        raise AssertionError("Invalid input must not create a lead.")

    monkeypatch.setattr(api, "create_lead", unexpected_create_lead)

    data = {
        "customer_name": "Test Homeowner",
        "customer_phone": "2085550100",
        "zip_code": "83814",
        "service_type": "Plumbing",
        "description": "Please inspect a leaking pipe.",
    }
    data[field] = value

    response = TestClient(api.app).post(
        "/homeowner/submit",
        data=data,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 422