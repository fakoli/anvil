"""Compatibility entry point for the shared isolated file-inspection worker."""

from anvil.state._file_inspection_worker import _inspect as _inspect_request
from anvil.state._file_inspection_worker import main


def _inspect(path: str) -> dict[str, object]:
    return _inspect_request({"path": path})


if __name__ == "__main__":
    raise SystemExit(main())
