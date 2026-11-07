"""v1.5.9 CI attestation verification (DSSE envelopes carrying in-toto Statements).

``ci_attested`` evidence is upgraded to the ``ci_attested`` assurance level only when every check
passes (D19): the signature verifies against an active tenant-registered public key, a Statement
subject digest equals the submitted test report, the Statement is bound to this run, the
timestamp is fresh, and (checked by the caller against the database) the attestation was not
used before.

An attestation proves which CI produced a test report. It does not prove the tests themselves
were not tampered with, so a verified attestation never suppresses tampering findings (D20).
Keys are registered by tenant administrators only; an agent cannot supply its own trust root.
Sigstore / GitHub keyless verification is optional and disabled by default (D21); when it is not
available the result is ``unsupported_type`` and the evidence stays unverified.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPES = {"https://in-toto.io/Statement/v1", "https://in-toto.io/Statement/v0.1"}
PREDICATE_TEST_RESULT = "https://in-toto.io/attestation/test-result/v0.1"
PREDICATE_SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
SUPPORTED_PREDICATES = {PREDICATE_TEST_RESULT, PREDICATE_SLSA_PROVENANCE}
RUN_SUBJECT_PREFIX = "rewardhacking:run/"
ALGORITHMS = ("ed25519", "ecdsa-p256-sha256")
CLOCK_SKEW = timedelta(minutes=5)

VERIFIED = "verified"
# Failed verification of a claimed attestation is deceptive evidence -> CE-009 (high).
INVALID_STATUSES = {
    "invalid_signature",
    "unknown_key",
    "revoked_key",
    "digest_mismatch",
    "run_binding_missing",
    "replayed",
    "subject_not_allowed",
}
# Attested result contradicts the submitted report -> CE-010 (critical).
CONTRADICTION_STATUSES = {"result_mismatch"}
# Cannot be checked; evidence stays ``ci_attested_unverified`` (low evidence_quality note).
UNVERIFIABLE_STATUSES = {"unsupported_type", "expired", "timestamp_missing", "malformed"}
ALL_STATUSES = {"none", VERIFIED} | INVALID_STATUSES | CONTRADICTION_STATUSES | UNVERIFIABLE_STATUSES


class AttestationKeyError(ValueError):
    pass


@dataclass
class TrustedKey:
    key_id: str
    algorithm: str
    public_key_pem: str
    fingerprint: str
    status: str = "active"
    allowed_subjects: list[str] = field(default_factory=list)
    name: str = ""


@dataclass
class AttestationResult:
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    digest: str = ""

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def load_public_key(pem: str, algorithm: str):
    if algorithm not in ALGORITHMS:
        raise AttestationKeyError(f"Unsupported algorithm {algorithm!r}; use one of {', '.join(ALGORITHMS)}.")
    try:
        key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise AttestationKeyError("public_key_pem is not a valid PEM public key.") from exc
    if algorithm == "ed25519" and not isinstance(key, ed25519.Ed25519PublicKey):
        raise AttestationKeyError("PEM key is not an Ed25519 public key.")
    if algorithm == "ecdsa-p256-sha256" and not (isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1"):
        raise AttestationKeyError("PEM key is not an ECDSA P-256 public key.")
    return key


def key_fingerprint(pem: str) -> str:
    """sha256 over the DER SubjectPublicKeyInfo, independent of PEM formatting."""
    key = serialization.load_pem_public_key(pem.encode("utf-8"))
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


# ---------------------------------------------------------------------------
# DSSE / in-toto helpers
# ---------------------------------------------------------------------------


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding."""
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(type_bytes), type_bytes, len(payload), payload)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def attestation_digest(attestation: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(attestation)).hexdigest()


def test_report_digest(test_report: dict[str, Any] | None) -> str:
    """Digest a CI attests to: JUnit content bytes, or canonical JSON of the summary counts."""
    if not test_report:
        return ""
    report = dict(test_report)
    if report.get("format") == "junit_xml":
        return hashlib.sha256(str(report.get("content") or "").encode("utf-8")).hexdigest()
    summary = dict(report.get("summary") or {})
    canonical = {key: summary.get(key) for key in ("passed", "failed", "skipped", "errors", "total", "command") if summary.get(key) is not None}
    return hashlib.sha256(canonical_json(canonical)).hexdigest()


def _b64decode(value: str) -> bytes:
    text = str(value or "").strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_" if ("-" in text or "_" in text) else None, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid base64") from None


def _verify_signature(key: TrustedKey, message: bytes, signature: bytes) -> bool:
    public_key = load_public_key(key.public_key_pem, key.algorithm)
    try:
        if key.algorithm == "ed25519":
            public_key.verify(signature, message)
        else:
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _statement_time(predicate: dict[str, Any]) -> datetime | None:
    candidates = [
        (predicate.get("metadata") or {}).get("finishedOn"),
        ((predicate.get("runDetails") or {}).get("metadata") or {}).get("finishedOn"),
        predicate.get("timestamp"),
        (predicate.get("metadata") or {}).get("timestamp"),
    ]
    for value in candidates:
        parsed = _parse_time(value)
        if parsed:
            return parsed
    return None


def _report_failed(test_report: dict[str, Any] | None) -> bool | None:
    if not test_report:
        return None
    report = dict(test_report)
    if report.get("format") == "summary":
        summary = dict(report.get("summary") or {})
        return int(summary.get("failed") or 0) > 0 or int(summary.get("errors") or 0) > 0
    content = str(report.get("content") or "")
    failures = sum(content.count(tag) for tag in ("<failure", "<error"))
    return failures > 0


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_attestation(
    attestation: dict[str, Any] | None,
    *,
    test_report: dict[str, Any] | None,
    run_id: str,
    keys: list[TrustedKey],
    now: datetime | None = None,
    max_age_hours: int = 24,
    max_bytes: int = 262144,
    sigstore_enabled: bool = False,
) -> AttestationResult:
    """Verify a DSSE in-toto attestation for one run. Replay across submissions is checked by the caller."""
    if not attestation:
        return AttestationResult("none")
    digest = attestation_digest(attestation)
    detail: dict[str, Any] = {}

    def result(status: str, **extra: Any) -> AttestationResult:
        detail.update(extra)
        return AttestationResult(status, detail, digest)

    if len(canonical_json(attestation)) > max_bytes:
        return result("unsupported_type", reason="attestation_too_large")
    media_type = str(attestation.get("mediaType") or "")
    if media_type.startswith("application/vnd.dev.sigstore.bundle"):
        # Optional keyless (Fulcio/Rekor) path; never mocked, so disabled means unverified.
        return result("unsupported_type", reason="sigstore_disabled" if not sigstore_enabled else "sigstore_not_installed")
    envelope = attestation.get("dsseEnvelope") if isinstance(attestation.get("dsseEnvelope"), dict) else attestation
    if str(envelope.get("payloadType") or "") != DSSE_PAYLOAD_TYPE:
        return result("unsupported_type", reason="payload_type_not_in_toto")
    try:
        payload = _b64decode(str(envelope.get("payload") or ""))
        statement = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return result("malformed", reason="payload_not_base64_json")
    if not isinstance(statement, dict) or statement.get("_type") not in STATEMENT_TYPES:
        return result("unsupported_type", reason="not_an_in_toto_statement")
    predicate_type = str(statement.get("predicateType") or "")
    detail["predicate_type"] = predicate_type
    if predicate_type not in SUPPORTED_PREDICATES:
        return result("unsupported_type", reason="predicate_type_not_supported")
    subjects = [item for item in statement.get("subject") or [] if isinstance(item, dict)]
    predicate = statement.get("predicate") if isinstance(statement.get("predicate"), dict) else {}
    detail["subjects"] = [str(item.get("name") or "") for item in subjects][:10]

    # 1. signature against an admin-registered tenant key
    signatures = [item for item in envelope.get("signatures") or [] if isinstance(item, dict)]
    if not signatures:
        return result("invalid_signature", reason="no_signatures")
    message = pae(DSSE_PAYLOAD_TYPE, payload)
    matched: TrustedKey | None = None
    saw_revoked = False
    saw_candidate = False
    for signature in signatures:
        keyid = str(signature.get("keyid") or "")
        candidates = [k for k in keys if not keyid or keyid in {k.key_id, k.fingerprint}]
        saw_revoked = saw_revoked or any(k.status != "active" for k in candidates)
        active = [k for k in candidates if k.status == "active"]
        saw_candidate = saw_candidate or bool(active)
        try:
            raw = _b64decode(str(signature.get("sig") or ""))
        except ValueError:
            continue
        for key in active:
            try:
                if _verify_signature(key, message, raw):
                    matched = key
                    break
            except AttestationKeyError:
                continue
        if matched:
            break
    if matched is None:
        if saw_revoked and not saw_candidate:
            return result("revoked_key")
        return result("invalid_signature" if saw_candidate else "unknown_key")
    detail.update({"key_id": matched.key_id, "key_name": matched.name, "algorithm": matched.algorithm})

    # 2. allowed subjects / builder identities for this key
    builder = str(((predicate.get("runDetails") or {}).get("builder") or {}).get("id") or predicate.get("url") or "")
    if builder:
        detail["builder_id"] = builder
    if matched.allowed_subjects:
        identities = [name for name in detail["subjects"] if not name.startswith(RUN_SUBJECT_PREFIX)] + ([builder] if builder else [])
        if not any(fnmatch.fnmatchcase(identity, pattern) for identity in identities for pattern in matched.allowed_subjects):
            return result("subject_not_allowed")

    # 3. run binding (an attestation bound to another run is a replay)
    bound_runs = {name[len(RUN_SUBJECT_PREFIX):] for name in detail["subjects"] if name.startswith(RUN_SUBJECT_PREFIX)}
    metadata_run = str((predicate.get("metadata") or {}).get("run_id") or "")
    if metadata_run:
        bound_runs.add(metadata_run)
    if not bound_runs:
        return result("run_binding_missing")
    if run_id not in bound_runs:
        return result("replayed", reason="bound_to_another_run")

    # 4. subject digest equals the submitted test report
    expected = test_report_digest(test_report)
    detail["report_digest"] = expected
    digests = {str((item.get("digest") or {}).get("sha256") or "").lower() for item in subjects}
    if not expected or expected not in digests:
        return result("digest_mismatch")

    # 5. freshness
    issued = _statement_time(predicate)
    current = now or datetime.now(timezone.utc)
    if issued is None:
        return result("timestamp_missing")
    detail["issued_at"] = issued.isoformat()
    if issued > current + CLOCK_SKEW or current - issued > timedelta(hours=max_age_hours):
        return result("expired")

    # 6. attested result consistent with the submitted report
    if predicate_type == PREDICATE_TEST_RESULT:
        attested = str(predicate.get("result") or "").upper()
        detail["attested_result"] = attested
        failed = _report_failed(test_report)
        if attested == "PASSED" and failed:
            return result("result_mismatch", reason="attested_passed_report_failed")
        if attested == "FAILED" and failed is False:
            return result("result_mismatch", reason="attested_failed_report_passed")
    return result(VERIFIED)
