"""Compiler constant attestation regressions on the HA Python runtime."""


def test_slice_constants_are_attested_recursively():
    from custom_components.petsafe_freshfeed.tuya_compat import _constant_signature

    assert _constant_signature(slice(1, None, None)) == _constant_signature(slice(1, None, None))
    assert _constant_signature(slice(1, None, None)) != _constant_signature(slice(True, None, None))
    assert _constant_signature(slice(1, None, None)) != _constant_signature(slice(2, None, None))
