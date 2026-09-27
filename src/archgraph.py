"""Generate, prepare, and serve the ArchGraph visualization of a codebase in one go."""

import argparse
import logging
import sys
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import generate
import prepare

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent


class ArchGraphHandler(SimpleHTTPRequestHandler):
    """Serves the frontend and maps /result.json to the given file, which may live outside the frontend folder."""

    def __init__(self, *args, result_path: Path, **kwargs):
        # Set before super().__init__, which already handles the request
        self.result_path = result_path
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def translate_path(self, path: str) -> str:
        if urlsplit(path).path == "/result.json":
            return str(self.result_path)
        return super().translate_path(path)


def create_server(result_path: Path, port: int) -> ThreadingHTTPServer:
    """Create a localhost server for the frontend, falling back to a free port if `port` is taken."""
    handler = partial(ArchGraphHandler, result_path=result_path.resolve())
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)


def serve(result_path: Path, port: int) -> None:
    server = create_server(result_path, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    logger.info(f"Serving the visualization at {url} (press Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Root directory of the codebase to analyze")
    parser.add_argument(
        "--output", required=True, type=Path, help="Folder for units.md, layers.json and result.json (created if needed)"
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for the local server (default: 8000, or a free one)")
    generate.add_options(parser)
    prepare.add_options(parser)
    args = parser.parse_args()

    generate.generate_folder(
        args.input,
        args.output,
        include_private=args.include_private,
        exclude_patterns=args.exclude,
        full_docstrings=args.full_docstrings,
        max_row_width=args.max_row_width,
    )
    try:
        result_path = prepare.prepare_folder(
            args.output, args.output, high_level_units_first=args.high_level_units_first, strict=args.strict
        )
    except ValueError as e:
        logger.critical(str(e))
        sys.exit(1)
    serve(result_path, args.port)
