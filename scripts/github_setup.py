"""Create missing workflow credential names without inspecting or replacing existing values."""

import json
import subprocess
import sys

REPOSITORY = "CBIIT/nci-sip-mcp"
PLACEHOLDER = "NCI_SIP_CONFIGURE_ME"
SECRETS = {
    "NCI_SIP_CADSR_CREDENTIAL": "Issued caDSR user:password for protected live checks",
    "NCI_SIP_EVS_LICENSE_KEY": "Issued EVS licence key for restricted live checks",
}
VARIABLES: dict[str, str] = {}


def instruction(name: str) -> str:
    return (
        f"Maintainer: run gh secret set {name} --repo {REPOSITORY} "
        "in your own terminal; it prompts for the value."
    )


def gh(arguments: list[str], action: str, value: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - fixed gh operations and inventory-owned names.
        ["gh", *arguments],  # noqa: S607 - gh is the documented administration boundary.
        input=value,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        print(
            f"GitHub setup failed while {action}; check gh authentication and repository access.",
            file=sys.stderr,
        )
        raise SystemExit(result.returncode)
    return result.stdout


def main() -> int:
    existing = {
        kind: {
            row["name"]
            for row in json.loads(
                gh([kind, "list", "--repo", REPOSITORY, "--json", "name"], f"listing {kind} names")
            )
        }
        for kind in ("secret", "variable")
    }
    for kind, inventory in (("secret", SECRETS), ("variable", VARIABLES)):
        for name in sorted(inventory):
            if name in existing[kind]:
                print(f"Existing {kind}: {name} (unchanged; value not inspected)")
            else:
                gh(
                    [kind, "set", name, "--repo", REPOSITORY],
                    f"creating {kind} {name}",
                    PLACEHOLDER,
                )
                print(f"Created {kind}: {name} (placeholder)")
    for name in sorted(SECRETS):
        print(instruction(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
