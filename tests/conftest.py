from pathlib import Path

import pytest

from connectorgen import connector_from_openapi

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def petstore_path() -> Path:
    return FIXTURES / "petstore.yaml"


@pytest.fixture
def petstore(petstore_path):
    return connector_from_openapi(petstore_path)


def plain(text: str) -> str:
    """CLI output as a person reads it: no colors, no box drawing, wrapped lines rejoined.
    (On CI runners Typer renders errors in colored boxes that wrap at the terminal width.)"""
    import re

    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)
