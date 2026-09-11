"""List voices or upload, convert, and download audio using Python's standard library."""
import argparse
import http.client
import json
from pathlib import Path
import shutil
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--voice", help="VoiceBox profile UUID")
    parser.add_argument("--sample", help="Reference sample UUID, required if the voice has several")
    parser.add_argument("--output", type=Path, default=Path("converted.wav"))
    parser.add_argument("--authorized", action="store_true", help="Confirm permission to use the source and voice")
    parser.add_argument("--timeout", type=float, default=3600, help="Maximum polling time in seconds")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    if args.input is None:
        with urlopen(base + "/api/voices", timeout=30) as response:
            print(json.dumps(json.load(response), indent=2))
        return
    if not args.voice or not args.authorized:
        parser.error("Conversion requires --voice and --authorized")
    if args.output.exists():
        parser.error("Output already exists; choose a new --output path")
    query = dict(profile_id=args.voice, filename=args.input.name, authorized="true")
    if args.sample:
        query["sample_id"] = args.sample
    connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=120)
    try:
        with args.input.open("rb") as source:
            connection.request("POST", "/api/speech?" + urlencode(query), body=source, headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(args.input.stat().st_size)
            })
        response = connection.getresponse()
        job = json.load(response)
        if response.status != 202:
            raise RuntimeError(f"Upload rejected ({response.status}): {job}")
    finally:
        connection.close()
    print(f"Job: {job['job_id']}; status: {base}{job['status_url']}")
    deadline = time.monotonic() + args.timeout
    while job["status"] in {"queued", "running"}:
        if time.monotonic() >= deadline:
            raise TimeoutError("Polling timed out; the server job continues. Use the status URL above.")
        time.sleep(2)
        with urlopen(base + job["status_url"], timeout=30) as response:
            job = json.load(response)
    if job["status"] != "completed":
        raise RuntimeError(job.get("error", job))
    with urlopen(base + job["download_url"], timeout=120) as response, args.output.open("xb") as destination:
        shutil.copyfileobj(response, destination)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
