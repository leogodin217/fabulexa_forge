"""Tests for the ExporterError hierarchy in fabulexa_export.errors."""

from __future__ import annotations

from fabulexa_export.errors import (
    ConfigError,
    CorruptError,
    CorruptValidationError,
    ExporterError,
    ExportError,
    ExportRuntimeError,
    InitRequiresRecordRoles,
    MixerExtraUnavailable,
    RebaseDateNotNaive,
    RebaseDateUnresolvable,
    RebaseError,
    RebaseInvalidRuntimeAnchor,
    RebaseOriginUnresolvable,
    RebaseTimezoneUnresolvable,
    RebaseUnknownTimezone,
)
from fabulexa_export.reader.errors import ReaderError


def test_exporter_error_is_exception() -> None:
    """ExporterError is a direct subclass of Exception."""
    assert issubclass(ExporterError, Exception)


def test_config_error_is_exporter_error() -> None:
    """ConfigError subclasses ExporterError."""
    assert issubclass(ConfigError, ExporterError)


def test_export_error_is_exporter_error() -> None:
    """ExportError subclasses ExporterError."""
    assert issubclass(ExportError, ExporterError)


def test_export_runtime_error_is_exporter_error() -> None:
    """ExportRuntimeError subclasses ExporterError."""
    assert issubclass(ExportRuntimeError, ExporterError)


def test_none_subclass_reader_error() -> None:
    """No export error class subclasses ReaderError — separate failure domains."""
    for cls in (ExporterError, ConfigError, ExportError, ExportRuntimeError):
        assert not issubclass(cls, ReaderError), (
            f"{cls.__name__} must not subclass ReaderError"
        )


def test_four_classes_are_distinct() -> None:
    """All four error classes are distinct types."""
    classes = [ExporterError, ConfigError, ExportError, ExportRuntimeError]
    assert len(set(classes)) == 4


def test_rebase_error_is_exporter_error() -> None:
    """RebaseError subclasses ExporterError."""
    assert issubclass(RebaseError, ExporterError)


def test_rebase_subclasses_subclass_rebase_error() -> None:
    """All RebaseError subclasses subclass RebaseError."""
    for cls in (
        RebaseTimezoneUnresolvable,
        RebaseOriginUnresolvable,
        RebaseDateNotNaive,
        RebaseDateUnresolvable,
        RebaseUnknownTimezone,
        RebaseInvalidRuntimeAnchor,
    ):
        assert issubclass(cls, RebaseError), f"{cls.__name__} must subclass RebaseError"


def test_init_requires_record_roles_is_exporter_error() -> None:
    """InitRequiresRecordRoles subclasses ExporterError."""
    assert issubclass(InitRequiresRecordRoles, ExporterError)


def test_mixer_extra_unavailable_is_exporter_error() -> None:
    """MixerExtraUnavailable subclasses ExporterError (sibling of KafkaClientUnavailable)."""
    assert issubclass(MixerExtraUnavailable, ExporterError)


def test_mixer_extra_unavailable_not_exporter_error_subtype() -> None:
    """MixerExtraUnavailable is a direct child of ExporterError, not a sub-subclass."""
    assert MixerExtraUnavailable.__bases__ == (ExporterError,)


def test_mixer_extra_unavailable_message_names_extra() -> None:
    """MixerExtraUnavailable message names the install extra."""
    msg = "FastAPI / ASGI server not installed; install the [mixer] extra"
    err = MixerExtraUnavailable(msg)
    assert "mixer" in str(err)


def test_corrupt_error_is_exporter_error() -> None:
    """CorruptError subclasses ExporterError."""
    assert issubclass(CorruptError, ExporterError)


def test_corrupt_validation_error_is_corrupt_error() -> None:
    """CorruptValidationError subclasses CorruptError."""
    assert issubclass(CorruptValidationError, CorruptError)


def test_corrupt_error_is_catchable_as_exporter_error() -> None:
    """CorruptError instances are caught by except ExporterError."""
    try:
        raise CorruptError("bad corrupt config")
    except ExporterError:
        pass  # expected


def test_corrupt_validation_error_is_catchable_as_exporter_error() -> None:
    """CorruptValidationError instances are caught by except ExporterError."""
    try:
        raise CorruptValidationError("business rule failed")
    except ExporterError:
        pass  # expected
