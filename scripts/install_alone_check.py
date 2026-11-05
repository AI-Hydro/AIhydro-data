"""Install-alone task for aihydro-data.

Run by ``.github/workflows/install-alone.yml`` with the *venv's* interpreter,
from a directory outside the checkout, after installing ONLY the built wheel
and its PyPI dependencies (aihydro-core among them). No other AI-Hydro package
is present and every socket connection is refused.

Task: discover data products offline through the public CLI and Python API.
  1. ``aihydro-data list-products --json --variable streamflow`` lists the
     registered streamflow products, each with a citation and a licence;
  2. ``aihydro-data describe NWIS_STREAMFLOW`` returns that product's full spec;
  3. the Python ``list_products``/``get_product`` agree with the CLI.

Socket block: an IN-PROCESS patch of the Python ``socket`` module (connect, create_connection,
getaddrinfo). It does not cover child processes or C libraries that open sockets themselves, so
it is evidence that this Python code made no network call, not an OS-level sandbox.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile


def _block_network() -> None:
    def refuse(*_a, **_k):
        raise RuntimeError("network access attempted during an install-alone check")

    socket.socket.connect = refuse  # type: ignore[assignment]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


def _cli(*args: str) -> str:
    exe = os.path.join(os.path.dirname(sys.executable), "aihydro-data")
    # The child inherits no network guard, but it only reads the registry; a
    # scratch HOME keeps the check from writing under the real ~/.aihydro.
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, "USERPROFILE": home}
        out = subprocess.run([exe, *args], check=True, capture_output=True, text=True, env=env)
    return out.stdout


def main() -> int:
    _block_network()
    import aihydro_data

    where = aihydro_data.__file__
    assert "site-packages" in where, f"imported from a source tree, not the wheel: {where}"

    listed = json.loads(_cli("list-products", "--json", "--variable", "streamflow"))
    assert listed, "no streamflow products registered"
    ids = {p["id"] for p in listed}
    assert "NWIS_STREAMFLOW" in ids, ids
    for p in listed:
        assert p["variable"] == "streamflow", p["id"]
        assert p["citation"] and p["license"], f"{p['id']} lacks citation/licence"

    spec = json.loads(_cli("describe", "NWIS_STREAMFLOW"))
    assert spec["id"] == "NWIS_STREAMFLOW" and spec["units"] == "m3/s", spec["id"]

    from aihydro_data import get_product, list_products

    assert {p.id for p in list_products(variable="streamflow")} == ids
    assert get_product("NWIS_STREAMFLOW").citation == spec["citation"]

    print(f"aihydro-data {aihydro_data.__version__}: install-alone task OK "
          f"({len(listed)} streamflow products, {where})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
