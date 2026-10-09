import factory_engine


def test_engine_package_imports() -> None:
    assert factory_engine.__version__ == "0.1.0"
