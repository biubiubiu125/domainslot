from app.worker.events import redact


def test_redact_secrets_and_txt_token():
    text = redact("token=abc Bearer eyJabc.def AccessKey LTAI123456 yyds-xiaolajiao-secret password=hello")
    assert "eyJabc" not in text
    assert "yyds-xiaolajiao-secret" not in text
    assert "hello" not in text
    assert "[redacted]" in text
