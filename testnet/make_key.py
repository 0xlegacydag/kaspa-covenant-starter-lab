"""Create a throwaway key for testnet experiments and print its testnet address.

The private key is written to the file you name and is never printed.
Keep that file OUTSIDE this repository. The key is for a test network only:
never send anything of value to its address and never reuse it elsewhere.

    python testnet/make_key.py C:\\path\\outside\\the\\repo\\tn10-test-key.txt
"""

import sys
from pathlib import Path

from kaspa import Keypair, PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1]).resolve()
    if REPO_ROOT in path.parents:
        print("Refusing: the key file must live outside the repository.")
        return 1
    if path.exists():
        print(f"Refusing: {path} already exists. Nothing was changed.")
        return 1

    keypair = Keypair.random()
    secret = keypair.private_key
    # Round-trip through the SDK before saving, so a file that loads is guaranteed.
    address = PrivateKey(secret).to_keypair().to_address("testnet").to_string()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="ascii")
    print(f"Key file written: {path}")
    print(f"Testnet address : {address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
