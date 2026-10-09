import factory_cli


def test_cli_package_imports() -> None:
    assert factory_cli.__version__ == "0.1.0"
