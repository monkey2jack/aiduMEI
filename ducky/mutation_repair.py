"""Local administrator's evidence-only journal repair CLI (also wheel-installable).

Stop the API first: this command acquires its data-directory process lock.
Never calls the SDK and never replays input. Inspect payload only in a private
terminal. Evidence itself is not printed or stored, only its SHA-256 digest.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys


def _nonempty(value):
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner", "--user-id", dest="owner", type=_nonempty, required=True)
    parser.add_argument("--bank-id", type=_nonempty, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List repair_required records in this exact scope")
    inspect = commands.add_parser("inspect", help="Inspect one exact-scope mutation")
    inspect.add_argument("--mutation-id", type=_nonempty, required=True)
    inspect.add_argument("--include-payload", action="store_true",
                         help="Show memory input/result; known credential fields are redacted")
    resolve = commands.add_parser("resolve", help="Record an independently verified outcome; no replay")
    resolve.add_argument("--mutation-id", type=_nonempty, required=True)
    resolve.add_argument("--resolution", required=True,
                         choices=("confirmed_applied", "confirmed_not_applied"))
    evidence = resolve.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--evidence", type=_nonempty)
    evidence.add_argument("--evidence-file", type=Path)
    resolve.add_argument("--result-file", type=Path,
                         help="Optional verified JSON receipt, only for confirmed_applied")
    return parser


def _redact(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if any(part in str(key).lower() for part in
                      ("password", "secret", "token", "api_key", "apikey", "authorization", "credential"))
                      else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _run(args):
    directory = args.data_dir.expanduser().resolve(strict=True)
    if not directory.is_dir() or not (directory / "mutation_journal.sqlite3").is_file():
        raise ValueError("existing journal directory required")
    # Configure before importing ducky storage modules. Explicit path avoids
    # accidentally repairing the repository's default data/ installation.
    os.environ["AIDUMEM_DATA_DIR"] = str(directory)
    from ducky.process_lock import acquire_api_process_lock
    acquire_api_process_lock(directory)
    from ducky import mutation_journal as journal
    if journal.journal_path().parent.resolve() != directory:
        raise ValueError("storage was already initialized for a different directory")
    with journal.scope_lock(args.owner, args.bank_id):
        if args.command == "list":
            return {"mutations": journal.list_repairs(args.owner, args.bank_id), "automatic_replay": False}
        if args.command == "inspect":
            record = journal.inspect_mutation(args.mutation_id, args.owner, args.bank_id,
                                              include_payload=args.include_payload)
            if record is None:
                raise KeyError("not found in scope")
            # Correlation key may itself contain caller data; IDs suffice here.
            record.pop("request_key", None)
            return _redact(record)
        evidence = args.evidence
        if args.evidence_file:
            evidence = args.evidence_file.read_text(encoding="utf-8")
        result = None
        if args.result_file:
            if args.resolution != "confirmed_applied":
                raise ValueError("receipt requires confirmed_applied")
            result = json.loads(args.result_file.read_text(encoding="utf-8"))
        return journal.resolve_mutation(args.mutation_id, args.owner, args.bank_id,
                                        resolution=args.resolution, evidence=evidence, result=result)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except KeyError:
        print(json.dumps({"status": "not_found", "automatic_replay": False}), file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Never echo provider errors, paths, evidence, credentials or payloads.
        print(json.dumps({"status": "refused", "automatic_replay": False,
                          "message": "Stop the API; verify directory, scope, state and evidence."}),
              file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
