from memproof.types import NotSupported


def load_adapter(name):
    raise NotSupported(f"Adapter not implemented: {name}")
