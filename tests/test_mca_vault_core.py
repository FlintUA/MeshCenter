"""MCA-VAULT PR 1 (ADR-0013): the vault core library. Pure library tests, no service, no wiring."""
import io
import json
import os
import random

import pytest

from meshsrv.attachments.vault import fields, header, kdf, memory, passphrase, recovery, streamcrypt
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.keys import subkey

FLOOR = kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT)      # the real floor: fast enough to run for real
PASS = passphrase.normalize("correct horse battery staple")


@pytest.fixture(scope="module")
def vault():
    return header.create(PASS, FLOOR)


# ---- KDF: floor, bounds, calibration ----------------------------------------------------------------------------

def test_floor_and_bounds_are_enforced():
    kdf.validate_params(FLOOR)
    for ops, mem, code in [
        (1, kdf.MIN_MEMLIMIT, "kdf_params_too_weak"),
        (kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT - 1024, "kdf_params_too_weak"),
        (kdf.MAX_OPSLIMIT + 1, kdf.MIN_MEMLIMIT, "kdf_params_unreasonable"),
        (kdf.MIN_OPSLIMIT, kdf.MAX_MEMLIMIT + 1024, "kdf_params_unreasonable"),
        (kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT + 1, "kdf_params_unreasonable"),      # not a multiple of 1 KiB
        ("2", kdf.MIN_MEMLIMIT, "kdf_params_invalid"),
        (True, kdf.MIN_MEMLIMIT, "kdf_params_invalid"),
    ]:
        with pytest.raises(VaultError) as info:
            kdf.validate_params(kdf.KdfParams(ops, mem))
        assert info.value.code == code, (ops, mem)


def test_real_derivation_is_deterministic_and_salted():
    salt = kdf.new_salt()
    a = kdf.derive_key(PASS, salt, FLOOR)
    assert bytes(a) == bytes(kdf.derive_key(PASS, salt, FLOOR)) and len(a) == 32
    assert bytes(a) != bytes(kdf.derive_key(PASS, kdf.new_salt(), FLOOR)), "another salt, another key"
    assert bytes(a) != bytes(kdf.derive_key(passphrase.normalize("another passphrase"), salt, FLOOR))
    with pytest.raises(VaultError):
        kdf.derive_key(b"", salt, FLOOR)


class FakeHost:
    """A host where one derivation takes `unit` seconds per (pass x MiB): deterministic calibration."""

    def __init__(self, unit):
        self.unit = unit
        self.calls = []

    def __call__(self, ops, mem):
        self.calls.append((ops, mem))
        return self.unit * ops * (mem / (1024 * 1024))


def test_calibration_reaches_about_one_second_by_raising_memory_first():
    host = FakeHost(unit=0.004)                      # 2 passes x 32 MiB = 0.256 s at the floor
    params = kdf.calibrate(ceiling_bytes=256 * 1024 * 1024, measure=host)
    assert params.memlimit > kdf.MIN_MEMLIMIT, "memory is raised first"
    assert 0.8 <= params.measured_ms / 1000 <= 1.3, params
    assert params.memlimit <= 256 * 1024 * 1024
    kdf.validate_params(params)


def test_calibration_uses_passes_when_the_memory_ceiling_is_reached():
    host = FakeHost(unit=0.001)                      # fast host
    params = kdf.calibrate(ceiling_bytes=64 * 1024 * 1024, measure=host)
    assert params.memlimit == 64 * 1024 * 1024
    assert params.opslimit > kdf.MIN_OPSLIMIT, "passes fill the remaining time"
    assert 0.8 <= params.measured_ms / 1000 <= 1.4


def test_calibration_lands_within_ten_percent_below_to_a_quarter_above_the_target_on_a_floor_bound_host():
    host = FakeHost(unit=0.0059)                     # a Zero 2 W class host: the floor takes 0.38 s, the ceiling is the floor
    params = kdf.calibrate(ceiling_bytes=kdf.MIN_MEMLIMIT, measure=host)
    assert params.memlimit == kdf.MIN_MEMLIMIT
    assert 0.9 <= params.measured_ms / 1000 <= 1.25, params
    assert params.opslimit > kdf.MIN_OPSLIMIT


def test_calibration_takes_the_fastest_of_two_runs_so_a_cold_first_run_does_not_weaken_the_result():
    calls = {"n": 0}

    def cold_then_warm(ops, mem):
        calls["n"] += 1
        base = 0.004 * ops * (mem / (1024 * 1024))
        return base * 3 if calls["n"] % 2 == 1 else base          # every first run of a pair is 3x slow

    params = kdf.calibrate(ceiling_bytes=kdf.MIN_MEMLIMIT, measure=cold_then_warm)
    assert 0.9 <= params.measured_ms / 1000 <= 1.25


def test_calibration_never_goes_below_the_floor_even_on_a_slow_host_or_tiny_ceiling():
    slow = FakeHost(unit=0.05)                       # already 3 s at the floor
    params = kdf.calibrate(ceiling_bytes=1, measure=slow)
    assert (params.opslimit, params.memlimit) == (kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT), "the floor wins over the ceiling"


def test_calibration_with_the_real_measure_returns_valid_parameters():
    params = kdf.calibrate(target_seconds=0.05, ceiling_bytes=kdf.MIN_MEMLIMIT)      # real Argon2id, tiny target
    kdf.validate_params(params)
    assert params.memlimit == kdf.MIN_MEMLIMIT and params.measured_ms >= 0


def test_memory_ceiling_is_a_quarter_of_available_ram(tmp_path):
    info = tmp_path / "meminfo"
    info.write_text("MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
    assert kdf.memory_ceiling_bytes(str(info)) == 4000000 * 1024 // 4
    info.write_text("MemAvailable: 1000 kB\n")
    assert kdf.memory_ceiling_bytes(str(info)) == kdf.MIN_MEMLIMIT, "never below the floor"
    assert kdf.memory_ceiling_bytes(str(tmp_path / "missing")) == kdf.DEFAULT_CEILING_BYTES


# ---- header: wrap, unlock, recovery, tampering ------------------------------------------------------------------

def test_unlock_with_the_passphrase_and_with_the_recovery_key_gives_the_same_storage_key(vault):
    parsed = header.VaultHeader.from_json(vault.header.to_json())
    assert bytes(header.unlock(parsed, PASS)) == bytes(vault.storage_key)
    assert bytes(header.unlock_with_recovery(parsed, vault.recovery_key)) == bytes(vault.storage_key)


def test_wrong_passphrase_wrong_recovery_key_and_damage_are_one_answer(vault):
    for attempt in (passphrase.normalize("not the passphrase"), PASS + b"x"):
        with pytest.raises(VaultError) as info:
            header.unlock(vault.header, attempt)
        assert info.value.code == "unlock_failed"
    with pytest.raises(VaultError) as info:
        header.unlock_with_recovery(vault.header, os.urandom(32))
    assert info.value.code == "unlock_failed"
    with pytest.raises(VaultError) as info:
        header.unlock_with_recovery(vault.header, b"short")
    assert info.value.code == "invalid_recovery_key"


def test_the_header_json_has_the_documented_shape_and_no_secret(vault):
    doc = json.loads(vault.header.to_json())
    assert doc["v"] == 1 and doc["kdf"]["alg"] == "argon2id13"
    assert set(doc) == {"v", "kdf", "wrap_pass", "wrap_recovery", "check"}
    text = vault.header.to_json()
    assert bytes(vault.storage_key).hex() not in text and vault.recovery_key.hex() not in text


def _edited(vault, mutate):
    doc = json.loads(vault.header.to_json())
    mutate(doc)
    return json.dumps(doc)


def test_tampered_kdf_parameters_do_not_open_even_when_above_the_floor(vault):
    stronger = _edited(vault, lambda d: d["kdf"].update(opslimit=d["kdf"]["opslimit"] + 1))
    parsed = header.VaultHeader.from_json(stronger)                      # valid and above the floor ...
    with pytest.raises(VaultError) as info:
        header.unlock(parsed, PASS)                                      # ... but bound into the wrap's AD
    assert info.value.code == "unlock_failed"
    other_salt = _edited(vault, lambda d: d["kdf"].update(salt=header._b64(os.urandom(16))))
    with pytest.raises(VaultError):
        header.unlock(header.VaultHeader.from_json(other_salt), PASS)


def test_weakened_or_absurd_parameters_are_refused_on_read(vault):
    for mutate, code in [
        (lambda d: d["kdf"].update(memlimit=1024 * 1024), "kdf_params_too_weak"),
        (lambda d: d["kdf"].update(opslimit=1), "kdf_params_too_weak"),
        (lambda d: d["kdf"].update(memlimit=kdf.MAX_MEMLIMIT * 2), "kdf_params_unreasonable"),
    ]:
        with pytest.raises(VaultError) as info:
            header.VaultHeader.from_json(_edited(vault, mutate))
        assert info.value.code == code


def test_malformed_headers_are_refused(vault):
    for text in ["", "not json", "[]", "{}", json.dumps({"v": 2}), json.dumps({"v": 1})]:
        with pytest.raises(VaultError):
            header.VaultHeader.from_json(text)
    assert pytest.raises(VaultError, header.VaultHeader.from_json, json.dumps({"v": 2})).value.code == "unsupported_header_version"
    for mutate in [
        lambda d: d["wrap_pass"].update(ct="AAAA"),
        lambda d: d["wrap_pass"].update(nonce=5),
        lambda d: d["kdf"].update(alg="scrypt"),
        lambda d: d.pop("check"),
        lambda d: d["kdf"].update(salt="!!!"),
    ]:
        with pytest.raises(VaultError):
            header.VaultHeader.from_json(_edited(vault, mutate))


def test_a_flipped_bit_in_a_wrap_or_the_check_fails_to_unlock(vault):
    def flip(section, field):
        def mutate(doc):
            raw = bytearray(header._unb64(doc[section][field] if section != "check" else doc["check"]))
            raw[-1] ^= 1
            if section == "check":
                doc["check"] = header._b64(raw)
            else:
                doc[section][field] = header._b64(raw)
        return mutate
    for section, field in [("wrap_pass", "ct"), ("wrap_recovery", "ct"), ("check", None)]:
        parsed = header.VaultHeader.from_json(_edited(vault, flip(section, field)))
        with pytest.raises(VaultError) as info:
            if section == "wrap_recovery":
                header.unlock_with_recovery(parsed, vault.recovery_key)
            else:
                header.unlock(parsed, PASS)
        assert info.value.code == "unlock_failed", section


def test_change_passphrase_rewraps_the_same_storage_key_with_a_fresh_salt(vault):
    new = passphrase.normalize("a brand new passphrase 2026")
    changed = header.change_passphrase(vault.header, bytes(vault.storage_key), new, FLOOR)
    assert changed.salt != vault.header.salt
    assert bytes(header.unlock(changed, new)) == bytes(vault.storage_key)
    with pytest.raises(VaultError):
        header.unlock(changed, PASS), "the old passphrase stops working"
    assert bytes(header.unlock_with_recovery(changed, vault.recovery_key)) == bytes(vault.storage_key), "the recovery key stays valid"
    with pytest.raises(VaultError):
        header.change_passphrase(vault.header, os.urandom(32), new, FLOOR)       # not the vault's key


def test_recalibrating_keeps_the_key_and_stores_the_new_parameters(vault):
    raised = kdf.KdfParams(kdf.MIN_OPSLIMIT + 1, kdf.MIN_MEMLIMIT * 2, measured_ms=1234)
    rewrapped = header.change_passphrase(vault.header, bytes(vault.storage_key), PASS, raised)
    parsed = header.VaultHeader.from_json(rewrapped.to_json())
    assert parsed.kdf_params == raised
    assert bytes(header.unlock(parsed, PASS)) == bytes(vault.storage_key)


def test_a_new_recovery_key_replaces_the_old_one(vault):
    updated, fresh = header.new_recovery_key(vault.header, bytes(vault.storage_key))
    assert fresh != vault.recovery_key
    assert bytes(header.unlock_with_recovery(updated, fresh)) == bytes(vault.storage_key)
    with pytest.raises(VaultError):
        header.unlock_with_recovery(updated, vault.recovery_key)
    assert bytes(header.unlock(updated, PASS)) == bytes(vault.storage_key), "the passphrase still works"


def test_subkeys_are_separate_and_stable(vault):
    sk = bytes(vault.storage_key)
    keys = {purpose: subkey(sk, purpose) for purpose in ("files", "fields", "keys", "check")}
    assert len(set(keys.values())) == 4 and all(len(k) == 32 for k in keys.values())
    assert keys["files"] == subkey(sk, "files") and keys["files"] != sk
    with pytest.raises(VaultError):
        subkey(sk, "unknown")
    with pytest.raises(VaultError):
        subkey(b"short", "files")


# ---- recovery key text -------------------------------------------------------------------------------------------

def test_recovery_key_text_form_round_trips_and_is_tolerant():
    raw = os.urandom(32)
    text = recovery.encode(raw)
    groups = text.split("-")
    assert len(groups) == 13 and all(len(g) == 4 for g in groups) and recovery.groups(text) == groups
    assert recovery.decode(text) == raw
    assert recovery.decode(text.lower().replace("-", " ")) == raw, "case, spaces and dashes do not matter"
    for bad in ["", "ABCD", text[:-1], text + "A", text.replace(text[0], "1", 1), text[:-1] + ("B" if text[-1] != "B" else "C")]:
        with pytest.raises(VaultError):
            recovery.decode(bad)


# ---- passphrase rule ---------------------------------------------------------------------------------------------

def test_passphrase_rule_minimum_twelve_and_a_hint():
    assert passphrase.assess("short")["level"] == "too_short" and not passphrase.assess("short")["ok"]
    assert not passphrase.assess("12345678901")["ok"], "11 characters"
    assert passphrase.assess("abcdefghijkl") == {"ok": False, "level": "common", "length": 12}, "on the common list"
    assert passphrase.assess("blue-tulip-77")["ok"] is True, "13 characters, not common"
    assert passphrase.assess("passwordpassword") == {"ok": False, "level": "common", "length": 16}
    assert passphrase.assess("aaaaaaaaaaaaaaaa")["level"] == "common", "one repeated character"
    assert passphrase.assess("correct horse battery staple")["level"] == "good"
    weak = passphrase.assess("mellowyellow1")
    assert weak["ok"] and weak["level"] in ("weak", "fair")
    assert passphrase.assess("é" * 12 + "xyz")["length"] == 15, "length counts characters, not bytes"


def test_passphrase_is_normalized_so_it_matches_across_platforms():
    composed, decomposed = "café au lait 2026", "café au lait 2026"
    assert passphrase.normalize(composed) == passphrase.normalize(decomposed)


# ---- encrypted files ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def file_key(vault):
    return subkey(bytes(vault.storage_key), "files")


CHUNK = streamcrypt.CHUNK


@pytest.mark.parametrize("size", [0, 1, 17, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK, 3 * CHUNK + 5, 5 * 1024 * 1024])
def test_round_trip_at_every_chunk_boundary_and_at_five_mib(file_key, size):
    data = os.urandom(size)
    blob = streamcrypt.encrypt_bytes(data, file_key, "att-1")
    assert streamcrypt.decrypt_bytes(blob, file_key, "att-1") == data
    assert len(blob) == streamcrypt.encrypted_size(size)
    assert streamcrypt.plain_size(len(blob)) == size
    assert blob[:4] == b"MCV1" and blob[4] == 1


def test_two_encryptions_of_the_same_data_differ(file_key):
    data = b"same plaintext" * 10
    assert streamcrypt.encrypt_bytes(data, file_key, "a") != streamcrypt.encrypt_bytes(data, file_key, "a")


def test_a_file_cannot_be_read_as_another_attachment_or_with_another_key(file_key):
    blob = streamcrypt.encrypt_bytes(b"secret" * 100, file_key, "att-1")
    for key, attachment in [(file_key, "att-2"), (os.urandom(32), "att-1")]:
        with pytest.raises(VaultError) as info:
            streamcrypt.decrypt_bytes(blob, key, attachment)
        assert info.value.code == "stream_corrupt"


def test_truncation_reordering_appending_and_bit_flips_never_decrypt(file_key):
    data = os.urandom(3 * CHUNK + 100)
    blob = streamcrypt.encrypt_bytes(data, file_key, "att-1")
    prefix, ct = blob[:29], blob[29:]
    block = CHUNK + 17
    # cut at every interesting place: inside the prefix, at chunk edges, inside the last chunk, drop the final chunk
    for cut in [0, 5, 28, 29, 30, 29 + block, 29 + 2 * block, 29 + 3 * block, len(blob) - 1, len(blob) - 17]:
        with pytest.raises(VaultError):
            streamcrypt.decrypt_bytes(blob[:cut], file_key, "att-1")
    with pytest.raises(VaultError):
        streamcrypt.decrypt_bytes(blob + b"\x00", file_key, "att-1")                     # data after the final chunk
    chunks = [ct[i:i + block] for i in range(0, len(ct), block)]
    swapped = prefix + chunks[1] + chunks[0] + b"".join(chunks[2:])
    with pytest.raises(VaultError):
        streamcrypt.decrypt_bytes(swapped, file_key, "att-1")                            # reordered
    with pytest.raises(VaultError):
        streamcrypt.decrypt_bytes(prefix + b"".join(chunks[:2] + chunks[3:]), file_key, "att-1")   # a chunk dropped
    for offset in [5, 28, 29, 100, len(blob) // 2, len(blob) - 1]:
        flipped = bytearray(blob)
        flipped[offset] ^= 0x01
        with pytest.raises(VaultError):
            streamcrypt.decrypt_bytes(bytes(flipped), file_key, "att-1")
    with pytest.raises(VaultError):
        streamcrypt.decrypt_bytes(b"XXXX" + blob[4:], file_key, "att-1")                 # wrong magic


def test_random_truncations_and_corruptions_never_return_wrong_data(file_key):
    rng = random.Random(7)
    data = os.urandom(CHUNK * 2 + 321)
    blob = streamcrypt.encrypt_bytes(data, file_key, "att-9")
    for _ in range(60):
        mutated = bytearray(blob)
        if rng.random() < 0.5:
            mutated = mutated[:rng.randrange(0, len(blob))]
        else:
            mutated[rng.randrange(0, len(blob))] ^= 1 << rng.randrange(8)
        try:
            out = streamcrypt.decrypt_bytes(bytes(mutated), file_key, "att-9")
        except VaultError:
            continue
        assert out == data and bytes(mutated) == blob, "only the untouched file may decrypt"


def test_decrypt_streams_chunk_by_chunk_and_handles_short_reads(file_key):
    data = os.urandom(CHUNK * 3 + 7)
    blob = streamcrypt.encrypt_bytes(data, file_key, "att-1")

    class Trickle(io.RawIOBase):
        """A source that returns at most 1000 bytes per read, like a socket."""
        def __init__(self, payload):
            self._buf = io.BytesIO(payload)

        def read(self, n=-1):
            return self._buf.read(min(n, 1000) if n and n > 0 else 1000)

    pieces = list(streamcrypt.decrypt_iter(Trickle(blob), file_key, "att-1"))
    assert [len(p) for p in pieces] == [CHUNK, CHUNK, CHUNK, 7]
    assert b"".join(pieces) == data


def test_plain_size_rejects_impossible_encrypted_sizes():
    assert streamcrypt.plain_size(0) is None and streamcrypt.plain_size(10) is None
    assert streamcrypt.plain_size(streamcrypt.encrypted_size(0)) == 0
    assert streamcrypt.plain_size(streamcrypt.encrypted_size(CHUNK) + 1) is None, "one byte more is not a valid .mcv size"
    assert streamcrypt.plain_size(streamcrypt.encrypted_size(CHUNK) - 1) == CHUNK - 1, "that is the size of a file one byte shorter"
    assert streamcrypt.plain_size(streamcrypt.encrypted_size(CHUNK) + 5) is None, "a trailing chunk shorter than its own tag"
    assert streamcrypt.plain_size(streamcrypt.encrypted_size(2 * CHUNK) + 16) is None


def test_file_helpers_encrypt_atomically_and_verify_before_a_plain_file_may_go(tmp_path, file_key):
    import hashlib
    plain = tmp_path / "plain.bin"
    data = os.urandom(CHUNK + 12345)
    plain.write_bytes(data)
    enc = tmp_path / "att-1.mcv"
    assert streamcrypt.encrypt_file(str(plain), str(enc), file_key, "att-1") == len(data)
    assert not (tmp_path / "att-1.mcv.part").exists() and plain.exists(), "the plain source is not removed here"
    digest = hashlib.sha256(data).hexdigest()
    assert streamcrypt.verify_file(str(enc), file_key, "att-1", digest, len(data)) is True
    assert streamcrypt.verify_file(str(enc), file_key, "att-1", "0" * 64, len(data)) is False
    assert streamcrypt.verify_file(str(enc), file_key, "att-1", digest, len(data) + 1) is False
    assert streamcrypt.verify_file(str(enc), file_key, "other", digest, len(data)) is False
    raw = bytearray(enc.read_bytes())
    raw[len(raw) // 2] ^= 1
    enc.write_bytes(bytes(raw))
    assert streamcrypt.verify_file(str(enc), file_key, "att-1", digest, len(data)) is False


def test_a_failed_encryption_leaves_no_part_file(tmp_path, file_key):
    enc = tmp_path / "x.mcv"
    with pytest.raises(OSError):
        streamcrypt.encrypt_file(str(tmp_path / "missing"), str(enc), file_key, "x")
    assert list(tmp_path.iterdir()) == []


# ---- encrypted database fields -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def field_key(vault):
    return subkey(bytes(vault.storage_key), "fields")


def test_field_round_trip_including_unicode_and_empty(field_key):
    for text in ["report.pdf", "", "Отчёт за октябрь.pdf", "日本語 \U0001F4CE", "a" * 5000]:
        value = fields.encrypt_field(field_key, "attachments", "file_name", "row-1", text)
        assert value.startswith("v1:") and fields.is_encrypted(value) and text not in value or text == ""
        assert fields.decrypt_field(field_key, "attachments", "file_name", "row-1", value) == text


def test_a_field_is_bound_to_its_table_column_and_row(field_key):
    value = fields.encrypt_field(field_key, "attachments", "file_name", "row-1", "secret name")
    for args in [("attachments", "file_name", "row-2"), ("attachments", "comment", "row-1"), ("other", "file_name", "row-1")]:
        with pytest.raises(VaultError) as info:
            fields.decrypt_field(field_key, *args, value)
        assert info.value.code == "field_corrupt"
    with pytest.raises(VaultError):
        fields.decrypt_field(os.urandom(32), "attachments", "file_name", "row-1", value)


def test_tampered_or_malformed_fields_are_refused(field_key):
    value = fields.encrypt_field(field_key, "t", "c", "r", "x")
    for bad in [value[:-2], value + "A", "v1:", "v1:AAAA", "v1:AAAA:BBBB", "plain text", "", "v2:" + value[3:]]:
        with pytest.raises(VaultError):
            fields.decrypt_field(field_key, "t", "c", "r", bad)


def test_read_shows_plain_text_decrypts_and_locks(field_key):
    value = fields.encrypt_field(field_key, "t", "c", "r", "name")
    assert fields.read(field_key, "t", "c", "r", "not migrated yet") == "not migrated yet", "a plain value is shown as it is"
    assert fields.read(None, "t", "c", "r", "not migrated yet") == "not migrated yet"
    assert fields.read(field_key, "t", "c", "r", value) == "name"
    assert fields.read(None, "t", "c", "r", value) is None, "locked: the placeholder, never the ciphertext"
    assert fields.read(field_key, "t", "c", "r", None) is None
    assert fields.encrypt_field(field_key, "t", "c", "r", "name") != value, "a fresh nonce every time"


# ---- memory buffer ------------------------------------------------------------------------------------------------

def test_secret_buffer_zeroes_in_place_and_is_idempotent():
    buf = memory.SecretBuffer(b"\x07" * 32)
    view = buf.view()
    assert bytes(buf) == b"\x07" * 32 and len(buf) == 32
    buf.zero()
    assert bytes(view) == b"\x00" * 32, "overwritten in place, not just dereferenced"
    buf.zero()
    assert isinstance(buf.locked, bool)


def test_secret_buffer_context_manager_zeroes_and_lock_failure_is_not_an_error():
    with memory.SecretBuffer(b"k" * 16, lock=False) as buf:
        assert buf.locked is False and bytes(buf) == b"k" * 16
        raw = buf.view()
    assert bytes(raw) == b"\x00" * 16
    empty = memory.SecretBuffer(b"")
    empty.zero()
    plain = bytearray(b"abc")
    memory.zero_bytearray(plain)
    assert bytes(plain) == b"\x00\x00\x00"


def test_no_secret_reaches_an_error_message(vault):
    for call in [
        lambda: header.unlock(vault.header, passphrase.normalize("hunter2 hunter2 hunter2")),
        lambda: fields.decrypt_field(os.urandom(32), "t", "c", "r", "v1:AAAA:BBBB"),
        lambda: streamcrypt.decrypt_bytes(b"MCV1\x01" + os.urandom(100), os.urandom(32), "att-secret-name"),
    ]:
        with pytest.raises(VaultError) as info:
            call()
        text = info.value.message + str(info.value)
        assert "hunter2" not in text and "att-secret-name" not in text
