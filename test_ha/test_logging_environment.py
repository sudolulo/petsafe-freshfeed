"""Synthetic transport fixtures must not inherit pytest's DEBUG capture level."""
import logging


def test_vendor_logging_defaults_do_not_enable_payload_debug(caplog):
    # TinyTuya creates these NOTSET loggers on import. Their admission must not
    # depend on collection order or whether pytest's live logging is enabled.
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("tinytuya.synthetic_capture_regression")
    assert not logger.isEnabledFor(logging.DEBUG)
