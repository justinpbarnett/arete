import json
import subprocess
import sys


def main() -> int:
    result = subprocess.run([sys.executable, "-B", "-m", "arete.cli", "prompts", "verify", "--json"],
                            capture_output=True, text=True)
    print(f"arete prompts verify --json exited {result.returncode}")
    print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode:
        return 1
    try:
        row = json.loads(result.stdout)
    except ValueError:
        print("Expected a JSON record for the verification prompt.")
        return 1
    if (not isinstance(row, dict) or row.get("name") != "verify" or row.get("valid") is not True
            or not isinstance(row.get("inputs"), list)
            or any(name not in row["inputs"] for name in ("goal", "surface", "commands", "journeys"))):
        print("Expected a valid verify prompt with goal, surface, commands and journeys inputs.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
