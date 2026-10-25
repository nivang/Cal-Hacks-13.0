"""Tests for escrow.py's pure half. No network.

The expected hash and selectors come from the deployed AuthCaptureEscrow v1.1 on Base Sepolia: getHash returned
VECTOR_HASH for VECTOR, and the selectors are the ones in its bytecode (capture changed shape in v1.1).
"""
from dataclasses import replace

import pytest

from sentinel import escrow

VECTOR = escrow.PaymentInfo(
    operator="0x1111111111111111111111111111111111111111", payer="0xE9497e03fC13E38A8dBA969470A832f30749Da4A",
    receiver="0x2222222222222222222222222222222222222222", token=escrow.USDC, max_amount=100000,
    pre_approval_expiry=2000000000, authorization_expiry=2000003600, refund_expiry=2000007200, salt=12345,
)
VECTOR_HASH = "0x85a1b83c6a1b42ac7a1addb9d75597534d090c7e2ce978e8edd52410e56c883a"


def test_info_hash_matches_the_contract():
    assert escrow.info_hash(VECTOR) == VECTOR_HASH


def test_hash_ignores_address_case():
    assert escrow.info_hash(replace(VECTOR, payer=VECTOR.payer.lower())) == VECTOR_HASH


def test_nonce_is_the_hash_without_the_payer():
    nonce = escrow.signature_nonce(VECTOR)
    assert nonce == escrow.info_hash(replace(VECTOR, payer=escrow.ZERO))
    assert nonce == escrow.signature_nonce(replace(VECTOR, payer="0x" + "33" * 20))
    assert nonce != VECTOR_HASH


def test_salt_changes_the_hash():
    assert escrow.info_hash(replace(VECTOR, salt=12346)) != VECTOR_HASH


def test_selectors_match_the_deployed_contract():
    assert escrow.authorize_data(VECTOR, 100000, "0x" + "00" * 65)[:10] == "0x41d66202"
    assert escrow.capture_data(VECTOR, 100000)[:10] == "0x3efa46e1"
    assert escrow.void_data(VECTOR)[:10] == "0xfa1cad17"


def test_buyer_signs_receive_with_authorization_to_the_collector():
    domain, types, primary, message = escrow.receive_typed_data(VECTOR)
    assert (domain.name, domain.version, domain.chain_id) == ("USDC", "2", 84532)
    assert domain.verifying_contract.lower() == escrow.USDC.lower()
    assert primary == "ReceiveWithAuthorization" and primary in types
    assert message["to"].lower() == escrow.EIP3009_COLLECTOR.lower()
    assert message["value"] == 100000 and message["validAfter"] == 0 and message["validBefore"] == 2000000000
    assert "0x" + message["nonce"].hex() == escrow.signature_nonce(VECTOR)


def test_json_round_trip_keeps_a_256_bit_salt():
    info = replace(VECTOR, salt=2**256 - 1)
    stored = info.to_json()
    assert isinstance(stored["salt"], str)
    assert escrow.PaymentInfo.from_json(stored) == info


OPERATOR = "0x" + "44" * 20
NOW = 1_900_000_000
POLICY = {"escrow_min_hold_s": 600, "escrow_max_hold_s": 3600}


def offer(**extra_changes):
    from types import SimpleNamespace

    extra = {"name": "USDC", "version": "2", "authCaptureEscrow": escrow.ESCROW, "captureAuthorizer": OPERATOR,
             "operatorType": "delegated", "paymentFlow": "escrow", "captureMode": "deferred",
             "captureDeadline": NOW + 1800, "refundDeadline": NOW + 3600, "minFeeBps": 0, "maxFeeBps": 0,
             "feeRecipient": escrow.ZERO, "assetTransferMethod": "eip3009"}
    extra.update(extra_changes)
    return SimpleNamespace(scheme="auth-capture", network="eip155:84532", asset=escrow.USDC, amount="250000",
                           pay_to="0x" + "22" * 20, max_timeout_seconds=300, extra=extra)


def test_check_terms_accepts_a_clean_offer():
    assert escrow.check_terms(offer(), OPERATOR.upper().replace("0X", "0x"), NOW, POLICY) is None


@pytest.mark.parametrize(("change", "says"), [
    ({"captureAuthorizer": "0x" + "55" * 20}, "not its operator"),
    ({"authCaptureEscrow": "0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff"}, "v1.1"),
    ({"maxFeeBps": 100}, "fee"),
    ({"feeRecipient": "0x" + "66" * 20}, "fee"),
    ({"assetTransferMethod": "permit2"}, "eip3009"),
    ({"receiverAuthorizer": "0x" + "77" * 20}, "receiver authorizer"),
    ({"operatorType": "custom"}, "operator type"),
    ({"refundDeadline": NOW + 1000}, "out of order"),
    ({"captureDeadline": NOW + 60}, "between 10 and 60 minutes"),
    ({"captureDeadline": NOW + 86400, "refundDeadline": NOW + 90000}, "between 10 and 60 minutes"),
    ({"captureDeadline": "soon"}, "missing"),
])
def test_check_terms_refuses_unsafe_offers(change, says):
    assert says in (escrow.check_terms(offer(**change), OPERATOR, NOW, POLICY) or "")


def test_check_terms_refuses_another_scheme():
    exact = offer()
    exact.scheme = "exact"
    assert "not an auth-capture offer" in (escrow.check_terms(exact, OPERATOR, NOW, POLICY) or "")


def test_payment_info_follows_the_offer():
    info = escrow.payment_info(offer(), "0x" + "33" * 20, NOW, 9)
    assert (info.operator, info.receiver, info.max_amount) == (OPERATOR, "0x" + "22" * 20, 250000)
    assert (info.pre_approval_expiry, info.authorization_expiry, info.refund_expiry) == (NOW + 300, NOW + 1800,
                                                                                         NOW + 3600)
    assert info.salt == 9 and info.max_fee_bps == 0
