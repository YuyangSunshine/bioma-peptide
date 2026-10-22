from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PROTOCOLS_IO_KEYS = (
    "PROTOCOLS_IO_TOKEN",
    "PROTOCOLS_IO_CLIENT_ID",
    "PROTOCOLS_IO_CLIENT_SECRET",
    "PROTOCOLS_IO_REFRESH_TOKEN",
)


def test_protocols_io_example_contains_no_credentials() -> None:
    values = dotenv_values(ROOT / ".env.example")

    assert set(PROTECTED_PROTOCOLS_IO_KEYS) <= values.keys()
    assert all(not values[key] for key in PROTECTED_PROTOCOLS_IO_KEYS)


def test_private_environment_file_is_gitignored() -> None:
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in patterns
    assert "!.env.example" in patterns
