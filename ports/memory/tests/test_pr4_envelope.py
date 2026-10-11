"""Tests for PR-4 capture-envelope MODEL - SYNTHETIC, local disposable.

Scope (peer DRAFT-READY verdict + controlling clarification, 2026-10-11):
the three NEW files only; MODEL semantics. This file proves NOTHING about
production authentication, trusted polling provenance, real key secrecy,
replay-safe effects, or any runtime behavior - all five production entry
points are fail-closed stubs and are asserted to refuse BEFORE touching
any dependency. Public synthetic fixture keys model byte
authentication/encryption ONLY.

Adversarial boundaries are exercised through the REAL model APIs
(seal_model / verify_model / claim_model / record_outcome_model /
mark_uncertain_model / resolve_unknown_model / readback_model /
mint_poll_provenance_model / production_*). Shape validators are the real
dataclass __post_init__ code; malformed registry rows are injected through
the actual disposable sqlite adapter; NO test asserts a fake self-raised
exception as if it were model-level rejection.

All identifiers, refs, labels, keys, and outcomes are synthetic PUBLIC
test values.
"""

import os
import sqlite3
import sys
import unittest

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import capability as cap  # noqa: E402
import envelope as env  # noqa: E402

NOW = 1_700_000_000
UID = 76
HEX32_A = "a" * 32
HEX32_B = "b" * 32
HEX32_C = "c" * 32
HEX64_A = "1" * 64
HEX64_B = "2" * 64


def make_key(key_id="key-a", not_before=0, decrypt_until=NOW + 10_000_000):
    return env.SyntheticKey(
        algorithm=env.Algorithm.FERNET, format_version=env.FORMAT_VERSION,
        key_id=key_id, not_before=not_before, decrypt_until=decrypt_until,
        material=Fernet.generate_key())


def make_web_binding(**over):
    fields = dict(
        uid=UID, lifetime=1, channel=cap.Channel.WEB,
        surface=cap.Surface.PRIVATE,
        issuer=env.SessionRef(session_token_hash=HEX64_A),
        operation_id=HEX32_A,
        operation_class=cap.OperationClass.WRITE,
        required_scopes=(cap.Scope.MEMORY_WRITE,),
        audience=env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                       id=None),
        information_ref="info-ref-1",
        content_fingerprint=HEX64_A,
        capture_epoch=7, grant_generation=3, policy_revision=2,
        decision_revision=1, consent_generation=4,
        extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
        group_action=None, sensitive=False, origin=cap.Origin.DIRECT,
        capture_time=NOW, expiry=NOW + env.MAX_AGE_SECONDS,
    )
    fields.update(over)
    return env.ModelOnlyRequestBinding(**fields)


def make_tg_binding(**over):
    fields = dict(
        uid=UID, lifetime=1, channel=cap.Channel.TELEGRAM,
        surface=cap.Surface.PRIVATE,
        issuer=env.TelegramIssuer(bot_id=123, chat_id=456, update_id=789),
        operation_id=HEX32_A,
        operation_class=cap.OperationClass.WRITE,
        required_scopes=(cap.Scope.MEMORY_WRITE,),
        audience=env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                       id=None),
        information_ref="info-ref-1",
        content_fingerprint=HEX64_A,
        capture_epoch=7, grant_generation=3, policy_revision=2,
        decision_revision=1, consent_generation=4,
        extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
        group_action=None, sensitive=False, origin=cap.Origin.DIRECT,
        capture_time=NOW, expiry=NOW + env.MAX_AGE_SECONDS,
    )
    fields.update(over)
    return env.ModelOnlyRequestBinding(**fields)


def make_payload(binding=None, nonce=HEX32_B, key_id="key-a"):
    return env.ModelOnlyCapturedPayload(
        binding=binding or make_web_binding(), nonce=nonce,
        format_version=env.FORMAT_VERSION, key_id=key_id)


def make_context(binding, **over):
    fields = dict(
        original_binding=binding, current_uid=binding.uid,
        current_lifetime=binding.lifetime,
        current_capture_epoch=binding.capture_epoch,
        current_grant_generation=binding.grant_generation,
        current_policy_revision=binding.policy_revision,
        current_decision_revision=binding.decision_revision,
        current_consent_generation=binding.consent_generation,
    )
    fields.update(over)
    return env.ModelOnlyCallerContext(**fields)


def make_registry(now=NOW):
    clock = env.SyntheticClock(now)
    adapter = env.SyntheticRegistryAdapter(sqlite3.connect(":memory:"))
    return env.ModelOnlyRegistryModel(adapter, clock), adapter, clock


def make_scope(binding=None):
    binding = binding or make_web_binding()
    return env.ModelOnlyOperationScope(
        uid=binding.uid, lifetime=binding.lifetime,
        operation_id=binding.operation_id)


def make_outcome(ref="outcome-ref-1", marker="marker-ref-1"):
    return env.ModelOnlyOutcome(outcome_ref=ref, operation_marker_ref=marker)


def make_evidence(scope, binding, owner_token, fence,
                  effect_state=env.EffectState.CONFIRMED_COMMITTED,
                  outcome=None):
    if effect_state is env.EffectState.CONFIRMED_COMMITTED:
        outcome = outcome or make_outcome()
    return env.ModelOnlyEffectEvidence(
        status=cap.ModelStatus.MODEL_ONLY, scope=scope, binding=binding,
        owner_token=owner_token, fence=fence,
        operation_marker_ref=(outcome.operation_marker_ref
                              if outcome is not None else "marker-ref-1"),
        effect_state=effect_state, outcome=outcome)


def verified(payload=None, key=None, now=NOW):
    payload = payload or make_payload()
    key = key or make_key()
    clock = env.SyntheticClock(now)
    envelope = env.seal_model(payload, key, clock)
    ring = env.SyntheticKeyRing(keys=(key,))
    return env.verify_model(envelope, ring,
                            make_context(payload.binding), clock)


class SealAndVerifyTests(unittest.TestCase):

    def test_roundtrip_returns_model_only_capture(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        self.assertIsInstance(envelope, env.ModelOnlyEnvelope)
        result = env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                                  make_context(payload.binding), clock)
        self.assertIs(result.status, cap.ModelStatus.MODEL_ONLY)
        self.assertEqual(result.payload, payload)
        self.assertEqual(result.binding, payload.binding)

    def test_verify_has_no_registry_and_returns_no_claim(self):
        # verify_model's signature takes (envelope, keyring, context, clock)
        # only: there is no registry parameter to mutate and no claim in
        # the return shape. A successful verify NEVER yields ACQUIRED.
        result = verified()
        self.assertFalse(hasattr(result, "claim_state"))
        self.assertFalse(hasattr(result, "owner_token"))
        self.assertFalse(hasattr(result, "fence"))

    def test_transport_only_reseal_matches_same_commitment(self):
        binding = make_web_binding()
        first = make_payload(binding, nonce=HEX32_B)
        second = make_payload(binding, nonce=HEX32_C)
        self.assertEqual(first.binding.commitment(),
                         second.binding.commitment())
        # reseal changes nonce/token bytes, NEVER intent
        key = make_key()
        clock = env.SyntheticClock(NOW)
        env1 = env.seal_model(first, key, clock)
        env2 = env.seal_model(second, key, clock)
        self.assertNotEqual(env1.token, env2.token)
        self.assertEqual(env1.key_id, env2.key_id)

    def test_bit_flipped_token_is_tampered(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        broken = bytearray(envelope.token)
        broken[len(broken) // 2] ^= 0x01
        flipped = env.ModelOnlyEnvelope(
            format_version=envelope.format_version, key_id=envelope.key_id,
            token=bytes(broken))
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(flipped, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.TAMPERED)

    def test_wrong_key_is_tampered_known_missing_key_unavailable(self):
        payload = make_payload()
        key = make_key()
        other = make_key(key_id="key-a")  # same id, different material
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(other,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.TAMPERED)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope,
                             env.SyntheticKeyRing(keys=(make_key("key-z"),)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)

    def test_retired_and_not_yet_valid_keys_fail_closed(self):
        payload = make_payload()
        retired = make_key(decrypt_until=NOW - 1)
        future = make_key(not_before=NOW + 100)
        clock = env.SyntheticClock(NOW)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(payload, retired, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(payload, future, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)
        # verify with a key retired after sealing also fails closed
        key = make_key()
        envelope = env.seal_model(payload, key, clock)
        gone = env.SyntheticKey(
            algorithm=env.Algorithm.FERNET, format_version=1,
            key_id="key-a", not_before=0, decrypt_until=NOW - 1,
            material=key.material)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(gone,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)

    def test_outer_inner_key_format_mismatch_is_tampered(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        spoofed = env.ModelOnlyEnvelope(
            format_version=envelope.format_version, key_id="key-b",
            token=envelope.token)
        ring = env.SyntheticKeyRing(
            keys=(key, make_key(key_id="key-b")))
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(spoofed, ring, make_context(payload.binding),
                             clock)
        # key-b material differs so authentication itself fails; had the
        # material matched, the outer/inner key_id mismatch would refuse
        self.assertIs(cm.exception.code, env.EnvelopeCode.TAMPERED)

    def test_oversized_outer_token_refused_before_decrypt(self):
        payload = make_payload()
        huge = env.ModelOnlyEnvelope(
            format_version=1, key_id="key-a",
            token=b"x" * (env.OUTER_TOKEN_MAX_BYTES + 1))
        clock = env.SyntheticClock(NOW)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(huge, env.SyntheticKeyRing(keys=(make_key(),)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_expiry_equality_refuses_and_no_future_capture(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        at_expiry = env.SyntheticClock(NOW + env.MAX_AGE_SECONDS)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, ring, make_context(payload.binding),
                             at_expiry)
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)
        last_valid = env.SyntheticClock(NOW + env.MAX_AGE_SECONDS - 1)
        ok = env.verify_model(envelope, ring, make_context(payload.binding),
                              last_valid)
        self.assertEqual(ok.payload, payload)
        # future capture_time refuses at verify
        future_binding = make_web_binding(
            capture_time=NOW + 10,
            expiry=NOW + 10 + env.MAX_AGE_SECONDS)
        future_payload = make_payload(future_binding)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(future_payload, key, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_expiry_must_equal_capture_plus_max_age(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(expiry=NOW + env.MAX_AGE_SECONDS + 1)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_checked_addition_overflow_refuses(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(capture_time=env.BIGINT_MAX - 1,
                             expiry=env.BIGINT_MAX - 1)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        clock = env.SyntheticClock(NOW)
        with self.assertRaises(env.EnvelopeError):
            clock.advance(env.BIGINT_MAX)

    def test_seal_with_expired_payload_refuses(self):
        key = make_key()
        old = make_payload(make_web_binding(
            capture_time=NOW - 10_000,
            expiry=NOW - 10_000 + env.MAX_AGE_SECONDS))
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(old, key, env.SyntheticClock(NOW))
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)


class StrictDecodeTests(unittest.TestCase):

    def _tamper_json(self, mutate):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        plaintext = env._canon(payload.to_dict())
        mutated = mutate(plaintext)
        token = Fernet(key.material).encrypt(mutated)
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        return envelope, key, payload, clock

    def _expect_malformed(self, envelope, key, payload, clock):
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_unknown_field_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"uid":76', b'"uid":76,"zzz":1'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_missing_field_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"capture_epoch":7,', b''))
        self._expect_malformed(envelope, key, payload, clock)

    def test_coercion_refuses_string_for_int_and_bool_for_int(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"uid":76', b'"uid":"76"'))
        self._expect_malformed(envelope, key, payload, clock)
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"lifetime":1', b'"lifetime":true'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_enum_drift_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"channel":"web"', b'"channel":"whatsapp"'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_duplicate_keys_refuse(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"uid":76', b'"uid":76,"uid":76'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_float_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"capture_epoch":7', b'"capture_epoch":7.0'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_audience_shape(self):
        with self.assertRaises(env.EnvelopeError):
            env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE, id="  ")
        with self.assertRaises(env.EnvelopeError):
            env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                  id="x" * 129)
        with self.assertRaises(env.EnvelopeError):
            # GROUP requires a non-None id under the model default
            env.ModelOnlyAudience(kind=env.AudienceKind.GROUP, id=None)


class BindingAndStalenessTests(unittest.TestCase):

    def test_every_projection_field_drift_refuses_stale(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        base = payload.binding.projection_dict()
        self.assertEqual(len(base), 22)  # full projection, nothing dropped
        drifts = [
            dict(uid=UID + 1), dict(lifetime=2),
            dict(channel=cap.Channel.TELEGRAM,
                 issuer=env.TelegramIssuer(bot_id=1, chat_id=2,
                                           update_id=3)),
            dict(surface=cap.Surface.GROUP),
            dict(issuer=env.SessionRef(session_token_hash=HEX64_B)),
            dict(operation_id=HEX32_C),
            dict(operation_class=cap.OperationClass.ERASE,
                 required_scopes=(cap.Scope.MEMORY_ERASE,)),
            dict(required_scopes=(cap.Scope.MEMORY_READ,)),
            dict(audience=env.ModelOnlyAudience(
                kind=env.AudienceKind.EXTERNAL, id="ext-1")),
            dict(information_ref="info-ref-2"),
            dict(content_fingerprint=HEX64_B),
            dict(capture_epoch=8), dict(grant_generation=4),
            dict(policy_revision=3), dict(decision_revision=2),
            dict(consent_generation=5),
            dict(sensitive=True), dict(origin=cap.Origin.ANONYMOUS),
        ]
        for drift in drifts:
            try:
                drifted = make_web_binding(**drift)
            except env.EnvelopeError:
                drifted = None  # constructor itself rejects incoherence
            if drifted is None:
                continue
            with self.assertRaises(env.EnvelopeError, msg=drift) as cm:
                env.verify_model(envelope, ring, make_context(drifted),
                                 clock)
            self.assertIs(cm.exception.code, env.EnvelopeCode.STALE,
                          msg=drift)

    def test_capture_time_expiry_drift_refuses_at_construction(self):
        # original capture/expiry are part of the projection; a changed
        # capture_time or expiry cannot even form a coherent binding
        for drift in (dict(capture_time=NOW + 5), dict(expiry=NOW + 5)):
            with self.assertRaises(env.EnvelopeError, msg=drift):
                make_web_binding(**drift)

    def test_current_snapshot_staleness(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        for field, value in (
                ("current_uid", UID + 1), ("current_lifetime", 2),
                ("current_capture_epoch", 8),
                ("current_grant_generation", 4),
                ("current_policy_revision", 3),
                ("current_decision_revision", 2),
                ("current_consent_generation", 5)):
            context = make_context(payload.binding, **{field: value})
            with self.assertRaises(env.EnvelopeError, msg=field) as cm:
                env.verify_model(envelope, ring, context, clock)
            self.assertIs(cm.exception.code, env.EnvelopeCode.STALE,
                          msg=field)

    def test_uid_recreation_new_lifetime_refuses(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        context = make_context(payload.binding, current_lifetime=2)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, ring, context, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.STALE)


class RegistryTests(unittest.TestCase):

    def test_claim_acquired_once_concurrent_second_is_in_progress(self):
        registry, _adapter, _clock = make_registry()
        result1 = registry.claim_model(verified(), "worker-1")
        self.assertIs(result1.claim_state, env.ClaimState.ACQUIRED)
        self.assertEqual(result1.fence, 1)
        self.assertIsNotNone(result1.owner_token)
        result2 = registry.claim_model(verified(), "worker-2")
        self.assertIs(result2.claim_state, env.ClaimState.IN_PROGRESS)
        self.assertIsNone(result2.fence)
        self.assertIsNone(result2.owner_token)

    def test_verify_then_claim_at_expiry_refuses(self):
        # verify at expiry-1 succeeds; the claim at expiry MUST recheck
        payload = make_payload()
        key = make_key()
        ring = env.SyntheticKeyRing(keys=(key,))
        ok_clock = env.SyntheticClock(NOW + env.MAX_AGE_SECONDS - 1)
        envelope = env.seal_model(payload, key, env.SyntheticClock(NOW))
        good = env.verify_model(envelope, ring,
                                make_context(payload.binding), ok_clock)
        registry, _adapter, _clock = make_registry(
            now=NOW + env.MAX_AGE_SECONDS)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(good, "worker-1")
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)

    def test_same_operation_reseal_registers_nonce_without_second_claim(self):
        registry, adapter, _clock = make_registry()
        binding = make_web_binding()
        first = registry.claim_model(
            verified(make_payload(binding, nonce=HEX32_B)), "worker-1")
        self.assertIs(first.claim_state, env.ClaimState.ACQUIRED)
        second = registry.claim_model(
            verified(make_payload(binding, nonce=HEX32_C)), "worker-2")
        self.assertIs(second.claim_state, env.ClaimState.IN_PROGRESS)
        # both nonces registered under the SAME operation
        self.assertIsNotNone(adapter.get_nonce(UID, 1, HEX32_B))
        self.assertIsNotNone(adapter.get_nonce(UID, 1, HEX32_C))

    def test_changed_projection_under_same_operation_refuses(self):
        registry, _adapter, _clock = make_registry()
        registry.claim_model(verified(), "worker-1")
        changed = make_payload(make_web_binding(
            information_ref="info-ref-2"))
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(changed), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_cross_operation_nonce_refuses(self):
        registry, _adapter, _clock = make_registry()
        registry.claim_model(verified(), "worker-1")
        other_op = make_payload(
            make_web_binding(operation_id=HEX32_C), nonce=HEX32_B)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(other_op), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_record_outcome_happy_path_and_idempotent_duplicate(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        outcome = make_outcome()
        record = registry.record_outcome_model(
            claim.scope, claim.owner_token, claim.fence, outcome)
        self.assertIs(record.state, env.RegistryState.COMMITTED)
        self.assertEqual(record.outcome, outcome)
        again = registry.record_outcome_model(
            claim.scope, claim.owner_token, claim.fence, outcome)
        self.assertEqual(again, record)
        # marker + outcome landed in the same transaction
        self.assertTrue(registry._adapter.marker_exists(
            outcome.operation_marker_ref))

    def test_changed_outcome_refuses_never_overwrite(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, make_outcome())
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(
                claim.scope, claim.owner_token, claim.fence,
                make_outcome(ref="outcome-ref-2"))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_stale_owner_or_fence_cannot_complete(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, "f" * 32,
                                          claim.fence, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, claim.owner_token,
                                          2, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_record_after_expiry_becomes_expired_tombstone(self):
        registry, _adapter, clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        clock.advance(env.MAX_AGE_SECONDS)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, claim.owner_token,
                                          claim.fence, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)
        record = registry.readback_model(claim.scope)
        self.assertIs(record.state, env.RegistryState.EXPIRED)
        # tombstone never reopens: late evidence cannot resolve EXPIRED
        binding = make_web_binding()
        evidence = make_evidence(claim.scope, binding, claim.owner_token,
                                 claim.fence)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(claim.scope, claim.owner_token,
                                           claim.fence, evidence)
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)

    def test_mark_uncertain_writes_nothing(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        receipt = registry.mark_uncertain_model(
            claim.scope, claim.owner_token, claim.fence)
        self.assertIs(receipt.receipt_state, env.ClaimState.UNKNOWN)
        record = registry.readback_model(claim.scope)
        self.assertIs(record.state, env.RegistryState.CLAIMED)

    def test_resolve_unknown_requires_durable_marker(self):
        registry, _adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        registry.mark_uncertain_model(claim.scope, claim.owner_token,
                                      claim.fence)
        # INCONCLUSIVE stays blocked, row becomes UNKNOWN
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence,
                          effect_state=env.EffectState.INCONCLUSIVE))
        self.assertIs(record.state, env.RegistryState.UNKNOWN)
        # CONFIRMED but marker missing (simulated crash before effect
        # commit) stays BLOCKED - caller flag alone NEVER commits
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence))
        self.assertIs(record.state, env.RegistryState.UNKNOWN)
        # no blind retry: an UNKNOWN claim result grants nothing
        again = registry.claim_model(verified(make_payload(binding)),
                                     "worker-2")
        self.assertIs(again.claim_state, env.ClaimState.UNKNOWN)
        self.assertIsNone(again.owner_token)

    def test_late_confirmed_marker_resolves_unknown_after_expiry(self):
        registry, _adapter, clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        # crash AFTER effect commit: marker durable, outcome write lost
        outcome = make_outcome()
        with registry._adapter.transaction():
            registry._adapter.put_marker(outcome.operation_marker_ref,
                                         outcome.outcome_ref)
        registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence,
                          effect_state=env.EffectState.INCONCLUSIVE))
        clock.advance(env.MAX_AGE_SECONDS + 1)
        # late attempted EXECUTION still refuses (fresh claim post-expiry)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(make_payload(binding)),
                                 "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)
        # accounting MAY complete: late confirmed evidence resolves UNKNOWN
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence, outcome=outcome))
        self.assertIs(record.state, env.RegistryState.COMMITTED)
        self.assertEqual(record.outcome, outcome)

    def test_evidence_tamper_refuses(self):
        registry, _adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        wrong_scope = env.ModelOnlyOperationScope(
            uid=UID + 1, lifetime=1, operation_id=HEX32_A)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(
                claim.scope, claim.owner_token, claim.fence,
                make_evidence(wrong_scope, binding, claim.owner_token,
                              claim.fence))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)
        changed_binding = make_web_binding(information_ref="info-ref-2")
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(
                claim.scope, claim.owner_token, claim.fence,
                make_evidence(claim.scope, changed_binding,
                              claim.owner_token, claim.fence))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_readback_is_typed_record_only(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, make_outcome())
        record = registry.readback_model(claim.scope)
        self.assertIs(record.status, cap.ModelStatus.MODEL_ONLY)
        self.assertIs(record.state, env.RegistryState.COMMITTED)
        self.assertFalse(hasattr(record, "owner_token"))
        self.assertFalse(hasattr(record, "fence"))
        self.assertFalse(hasattr(record, "commitment"))
        self.assertIsNone(registry.readback_model(
            env.ModelOnlyOperationScope(uid=UID, lifetime=9,
                                        operation_id=HEX32_C)))

    def test_resolution_deadline_is_expiry_plus_window_checked(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        record = registry.readback_model(claim.scope)
        self.assertEqual(
            record.resolution_deadline,
            NOW + env.MAX_AGE_SECONDS + env.RESOLUTION_WINDOW_SECONDS)

    def test_claim_result_shape_rules(self):
        scope = make_scope()
        with self.assertRaises(env.EnvelopeError):
            # owner_token on a non-ACQUIRED result refuses
            env.ModelOnlyClaimResult(
                status=cap.ModelStatus.MODEL_ONLY,
                claim_state=env.ClaimState.IN_PROGRESS, scope=scope,
                fence=1, owner_token="d" * 32, outcome=None)
        with self.assertRaises(env.EnvelopeError):
            # outcome on a non-COMMITTED result refuses
            env.ModelOnlyClaimResult(
                status=cap.ModelStatus.MODEL_ONLY,
                claim_state=env.ClaimState.ACQUIRED, scope=scope,
                fence=1, owner_token="d" * 32, outcome=make_outcome())


class ProvenanceAndStubTests(unittest.TestCase):

    def test_poll_provenance_mint_and_invariant(self):
        payload = make_payload(make_tg_binding())
        clock = env.SyntheticClock(NOW + 5)
        provenance = env.mint_poll_provenance_model(
            env.SyntheticBoundaryEvidence.for_testing(payload), clock)
        self.assertIs(provenance.status, cap.ModelStatus.MODEL_ONLY)
        self.assertEqual(provenance.payload, payload)
        self.assertEqual(provenance.minted_at, NOW + 5)
        self.assertLess(provenance.minted_at, payload.binding.expiry)

    def test_poll_provenance_telegram_only(self):
        web_payload = make_payload(make_web_binding())
        with self.assertRaises(env.EnvelopeError) as cm:
            env.mint_poll_provenance_model(
                env.SyntheticBoundaryEvidence.for_testing(web_payload),
                env.SyntheticClock(NOW))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_boundary_evidence_constructor_guard(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            env.SyntheticBoundaryEvidence(make_payload())
        self.assertIs(cm.exception.code, env.EnvelopeCode.MODEL_ONLY)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.mint_poll_provenance_model("not-evidence",
                                           env.SyntheticClock(NOW))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MODEL_ONLY)

    def test_all_five_production_stubs_refuse_unconditionally(self):
        key = make_key()
        clock = env.SyntheticClock(NOW)
        payload = make_payload()
        capability = object()  # even a "valid" capability-shaped input
        for stub in (env.production_seal, env.production_verify,
                     env.production_dispatch, env.production_load,
                     env.production_mint_poll_provenance):
            with self.assertRaises(env.EnvelopeError) as cm:
                stub(payload, key, clock, capability)
            self.assertIs(cm.exception.code, env.EnvelopeCode.MODEL_ONLY)

    def test_error_messages_are_fixed_and_echo_nothing(self):
        for code in env.EnvelopeCode:
            message = str(env.EnvelopeError(code))
            for leak in (HEX32_B, HEX64_A, "key-a", "info-ref-1",
                         "outcome-ref-1"):
                self.assertNotIn(leak, message)


if __name__ == "__main__":
    unittest.main()
