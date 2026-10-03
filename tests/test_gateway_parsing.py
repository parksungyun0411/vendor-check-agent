from vendor_check.clients.gateway import classify_gateway_code, parse_gateway_error
from vendor_check.errors import FailureKind

# 2026-10-03 apis.data.go.kr 에 임의 키로 호출해 받은 본문 (resultType 미지정 / json)
OBSERVED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse>
<cmmMsgHeader>
  <errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>
  <returnAuthMsg>등록되지 않은 서비스키</returnAuthMsg>
  <returnReasonCode>30</returnReasonCode>
</cmmMsgHeader>
</OpenAPI_ServiceResponse>""".encode()
OBSERVED_JSON = (
    '{"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE_KEY_IS_NULL", '
    '"returnAuthMsg": "서비스 접근거부", "returnReasonCode": "20"}}}'
).encode()


def test_parses_observed_xml_envelope():
    assert parse_gateway_error(OBSERVED_XML) == ("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR")


def test_parses_observed_json_envelope():
    assert parse_gateway_error(OBSERVED_JSON) == ("20", "SERVICE_KEY_IS_NULL")


def test_normal_body_is_not_an_error():
    assert parse_gateway_error(b'{"response": {"header": {"resultCode": "00"}}}') is None


def test_truncated_envelope_still_detected():
    code, _ = parse_gateway_error(OBSERVED_XML[:-60])
    assert code == "30"


def test_code_classification():
    assert classify_gateway_code("22") == FailureKind.QUOTA
    assert classify_gateway_code("23") == FailureKind.RATE_LIMIT
    assert classify_gateway_code("30") == FailureKind.AUTH
    assert classify_gateway_code("01") == FailureKind.GATEWAY
    assert classify_gateway_code("10") == FailureKind.INVALID_REQUEST
