"""Export only the reviewed public source into a new directory; never copy Git/work."""
import argparse
import json
import os
import re
from pathlib import Path

from build_za_release import no_links, public_payload


def export(root, output):
    output = no_links(Path(os.path.abspath(output)))
    if os.path.lexists(output):
        raise FileExistsError("fresh source destination required")
    version, files = public_payload(root)
    # Refuse accidental reintroduction of this developer's private paths.
    root_path = os.fsencode(Path(root).absolute())
    private_path = re.compile(rb"(?<![A-Za-z0-9_/])" + re.escape(root_path)
                              + rb"(?=$|[/\\'\"\s])")
    for name, data in files.items():
        if private_path.search(data) or b"/canonical-" + b"tests" in data:
            raise ValueError("host-specific path in public file: " + name)
    output.mkdir(parents=True)
    for name, data in sorted(files.items()):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o755 if name.endswith(".sh") else 0o644)
    return {"version": version, "output": str(output), "files": len(files),
            "copied_git_or_work": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.root, args.output), indent=2))
