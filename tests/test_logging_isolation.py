"""Guards the autouse structlog reset in tests/conftest.py.

The tests run in file order: the first configures structlog globally (as
navigator.__main__._configure_logging does), the second checks that the
configuration did not leak into it.
"""
import io

import structlog


def test_a_configures_structlog_globally():
    stream = io.StringIO()
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=stream))
    assert structlog.is_configured()
    structlog.get_logger("t").info("isolation.probe")
    assert "isolation.probe" in stream.getvalue()
    stream.close()  # mimic pytest closing its capture stream after the test


def test_b_structlog_config_is_reset_between_tests():
    assert not structlog.is_configured()
    # logging must not hit the stream closed by the previous test
    structlog.get_logger("t").info("isolation.after_reset")


def test_c_configure_logging_does_not_leak_into_later_tests(capsys):
    import navigator.__main__ as m
    m._configure_logging()
    structlog.get_logger("t").info("isolation.configured")
    assert "isolation.configured" in capsys.readouterr().err


def test_d_logging_after_configure_logging_test_does_not_crash():
    assert not structlog.is_configured()
    structlog.get_logger("t").info("isolation.after_main_configure")
